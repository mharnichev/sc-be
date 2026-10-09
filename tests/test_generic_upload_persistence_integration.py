"""Real PostgreSQL upload persistence, using only synthetic temporary images."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.api.v1.routes.uploads import upload_file as upload_route
from app.core.config import settings
from app.models.upload import Upload
from test_product_image_storage import image_bytes, upload_file
from test_segments_integration import anyio_backend, database


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'upload_dir', str(tmp_path))
    monkeypatch.setattr(settings, 'upload_url_prefix', '/synthetic-media')
    return tmp_path


def synthetic_file():
    return upload_file(image_bytes('PNG'), content_type='image/png', filename='synthetic.png')


@pytest.mark.anyio
async def test_successful_upload_commits_record_and_canonical_bytes(database, storage):
    async with database() as session:
        response = await upload_route(file=synthetic_file(), session=session, _=object())
    async with database() as session:
        saved = await session.get(Upload, response.id)
        assert saved.file_path == response.file_path
        assert saved.content_type == 'image/png'
        assert Path(saved.file_path).read_bytes() == image_bytes('PNG')
        assert await session.scalar(select(func.count()).select_from(Upload)) == 1


@pytest.mark.anyio
async def test_real_flush_rejection_removes_only_new_file_and_recovers_session(database, storage, monkeypatch):
    existing = storage / 'preexisting.txt'
    existing.write_bytes(b'synthetic canary')
    monkeypatch.setattr(settings, 'upload_url_prefix', '/' + 'x' * 600)
    async with database() as session:
        with pytest.raises(DBAPIError) as error:
            await upload_route(file=synthetic_file(), session=session, _=object())
        assert error.value.orig.sqlstate == '22001'
        assert await session.scalar(text('SELECT 1')) == 1
        assert await session.scalar(select(func.count()).select_from(Upload)) == 0
    assert existing.read_bytes() == b'synthetic canary'
    assert not list((storage / 'images').iterdir())


@pytest.mark.anyio
async def test_real_deferred_commit_rejection_removes_new_file(database, storage, monkeypatch):
    existing = storage / 'preexisting.txt'
    existing.write_bytes(b'synthetic canary')
    fixed = 'a' * 32
    monkeypatch.setattr('app.services.uploads.uuid.uuid4', lambda: SimpleNamespace(hex=fixed))
    async with database() as session:
        await session.execute(text('ALTER TABLE uploads ADD CONSTRAINT synthetic_upload_url_unique UNIQUE (file_url) DEFERRABLE INITIALLY DEFERRED'))
        session.add(Upload(file_name='preexisting.txt', file_path=str(existing),
                           file_url=f'/synthetic-media/images/synthetic-{fixed}.png', content_type='text/plain', size=16))
        await session.commit()
        with pytest.raises(IntegrityError) as error:
            await upload_route(file=synthetic_file(), session=session, _=object())
        assert error.value.orig.sqlstate == '23505'
        assert await session.scalar(text('SELECT 1')) == 1
        assert await session.scalar(select(func.count()).select_from(Upload)) == 1
    assert existing.read_bytes() == b'synthetic canary'
    assert not list((storage / 'images').iterdir())


@pytest.mark.anyio
@pytest.mark.parametrize('failure_phase', ['refresh', 'after_commit'])
async def test_real_committed_upload_survives_subsequent_failure(database, storage, monkeypatch, failure_phase):
    failure = RuntimeError('synthetic postcommit error')
    async with database() as session:
        if failure_phase == 'refresh':
            async def fail_refresh(_instance):
                raise failure
            monkeypatch.setattr(session, 'refresh', fail_refresh)
        else:
            original_commit = session.commit
            async def fail_after_commit():
                await original_commit()
                raise failure
            monkeypatch.setattr(session, 'commit', fail_after_commit)
        with pytest.raises(RuntimeError) as error:
            await upload_route(file=synthetic_file(), session=session, _=object())
        assert error.value is failure
    async with database() as session:
        saved = (await session.scalars(select(Upload))).one()
        assert Path(saved.file_path).read_bytes() == image_bytes('PNG')
        assert saved.content_type == 'image/png'
