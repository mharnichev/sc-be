"""Local source containment using synthetic storage and a stubbed remote transport."""
import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.utils import process_product_images as images


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def storage(tmp_path, monkeypatch):
    buffer = BytesIO()
    Image.new("RGB", (1, 1), (1, 2, 3)).save(buffer, format="PNG")
    content = buffer.getvalue()
    root = tmp_path / "uploads"
    inside = root / "products" / "7" / "original.png"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(content)
    outside = tmp_path / "outside.png"
    outside.write_bytes(content)
    sibling = tmp_path / "uploads-other" / "original.png"
    sibling.parent.mkdir()
    sibling.write_bytes(content)
    monkeypatch.setattr(images, "settings", SimpleNamespace(upload_dir=str(root), upload_url_prefix="/media"))
    return SimpleNamespace(root=root, inside=inside, outside=outside, sibling=sibling, content=content)


def source(url):
    return images.SourceImage(7, 9, "gallery:9", url)


@pytest.mark.anyio
@pytest.mark.parametrize("url", [
    "/media/products/7/original.png",
    "/media/products/7/./original.png",
    "/media/products/7/../7/original.png",
])
async def test_valid_local_source_preserves_bytes_and_fingerprint(storage, url):
    path, fingerprint, temporary = await images._materialize_source(source(url))
    assert path.resolve() == storage.inside.resolve()
    assert path.read_bytes() == storage.content
    assert fingerprint == hashlib.sha256(storage.content).hexdigest()
    assert temporary is None


@pytest.mark.anyio
async def test_relative_upload_root_is_resolved_from_working_directory(storage, monkeypatch):
    monkeypatch.chdir(storage.root.parent)
    images.settings.upload_dir = "uploads"
    path, fingerprint, temporary = await images._materialize_source(source("/media/products/7/original.png"))
    assert path.resolve() == storage.inside.resolve()
    assert fingerprint == hashlib.sha256(storage.content).hexdigest()
    assert temporary is None


@pytest.mark.anyio
async def test_configured_upload_prefix_with_trailing_slash_still_works(storage):
    images.settings.upload_url_prefix = "/assets/uploads/"
    path, fingerprint, temporary = await images._materialize_source(source("/assets/uploads/products/7/original.png"))
    assert path.resolve() == storage.inside.resolve()
    assert fingerprint == hashlib.sha256(storage.content).hexdigest()
    assert temporary is None


@pytest.mark.anyio
async def test_upload_root_can_itself_be_a_symlink(storage, monkeypatch):
    alias = storage.root.parent / "configured-uploads"
    alias.symlink_to(storage.root, target_is_directory=True)
    images.settings.upload_dir = str(alias)
    path, fingerprint, temporary = await images._materialize_source(source("/media/products/7/original.png"))
    assert path.resolve() == storage.inside.resolve()
    assert fingerprint == hashlib.sha256(storage.content).hexdigest()
    assert temporary is None


@pytest.mark.anyio
@pytest.mark.parametrize("directory_link", [False, True])
async def test_symlink_staying_inside_root_is_supported(storage, directory_link):
    link = storage.root / "linked"
    if directory_link:
        link.symlink_to(storage.inside.parent, target_is_directory=True)
        url = "/media/linked/original.png"
    else:
        link.symlink_to(storage.inside)
        url = "/media/linked"
    path, fingerprint, temporary = await images._materialize_source(source(url))
    assert path.resolve() == storage.inside.resolve()
    assert fingerprint == hashlib.sha256(storage.content).hexdigest()
    assert temporary is None


@pytest.mark.anyio
@pytest.mark.parametrize("escape", [
    "parent", "nested_parent", "absolute_suffix", "sibling_prefix", "file_symlink", "directory_symlink",
])
async def test_local_escape_is_rejected_before_hash_or_read(storage, monkeypatch, escape):
    if escape == "parent":
        url = "/media/../outside.png"
    elif escape == "nested_parent":
        url = "/media/products/7/../../../outside.png"
    elif escape == "absolute_suffix":
        url = f"/media/{storage.outside}"
    elif escape == "sibling_prefix":
        url = "/media/../uploads-other/original.png"
    elif escape == "file_symlink":
        (storage.root / "escape.png").symlink_to(storage.outside)
        url = "/media/escape.png"
    else:
        (storage.root / "escape-dir").symlink_to(storage.outside.parent, target_is_directory=True)
        url = "/media/escape-dir/outside.png"

    hash_calls = []
    read_calls = []
    original_hash = images._sha256_file
    original_open = Path.open

    def tracked_hash(path):
        hash_calls.append(path)
        return original_hash(path)

    def guarded_open(path, *args, **kwargs):
        if path.resolve() in {storage.outside.resolve(), storage.sibling.resolve()}:
            read_calls.append(path)
            raise AssertionError("Outside synthetic file must not be opened")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(images, "_sha256_file", tracked_hash)
    monkeypatch.setattr(Path, "open", guarded_open)
    with pytest.raises(ValueError):
        await images._materialize_source(source(url))
    assert hash_calls == []
    assert read_calls == []


@pytest.mark.anyio
async def test_missing_contained_source_keeps_file_not_found(storage):
    with pytest.raises(FileNotFoundError):
        await images._materialize_source(source("/media/products/7/missing.png"))


@pytest.mark.anyio
async def test_symlink_cycle_is_rejected_before_hash_with_controlled_error(storage, monkeypatch):
    first = storage.root / "loop-first"
    second = storage.root / "loop-second"
    first.symlink_to(second)
    second.symlink_to(first)
    hash_calls = []

    def unexpected_hash(path):
        hash_calls.append(path)
        raise AssertionError("Symlink cycle must not be hashed")

    monkeypatch.setattr(images, "_sha256_file", unexpected_hash)
    with pytest.raises(ValueError):
        await images._materialize_source(source("/media/loop-first"))
    assert hash_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("url_kind", ["prefix_confusion", "raw_absolute", "file_scheme"])
async def test_unsupported_local_forms_do_not_gain_new_path_support(storage, monkeypatch, url_kind):
    url = {
        "prefix_confusion": "/media-other/original.png",
        "raw_absolute": str(storage.inside),
        "file_scheme": storage.outside.as_uri(),
    }[url_kind]
    calls = []

    def unexpected_hash(path):
        calls.append(path)
        raise AssertionError("Unsupported original must not be hashed")

    monkeypatch.setattr(images, "_sha256_file", unexpected_hash)
    with pytest.raises(ValueError, match="Unsupported original image URL"):
        await images._materialize_source(source(url))
    assert calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("url", ["http://supplier.example.test/original.png", "https://supplier.example.test/original.png"])
async def test_remote_source_keeps_existing_materialization_contract(storage, monkeypatch, url):
    calls = []

    class StubClient:
        def __init__(self, **kwargs):
            calls.append(("options", kwargs))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, requested):
            calls.append(("get", requested))
            return SimpleNamespace(content=storage.content, headers={"content-type": "image/png"},
                                   raise_for_status=lambda: None)

    monkeypatch.setattr(images.httpx, "AsyncClient", StubClient)
    path, fingerprint, temporary = await images._materialize_source(source(url))
    try:
        assert path.name == "source-image.png"
        assert path.read_bytes() == storage.content
        assert fingerprint == hashlib.sha256(storage.content).hexdigest()
        assert temporary is not None
        assert calls == [("options", {"timeout": 60, "follow_redirects": True}), ("get", url)]
    finally:
        if temporary is not None:
            temporary.cleanup()
