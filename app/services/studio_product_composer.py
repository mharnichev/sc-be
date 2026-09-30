"""Small, explicit client for the local Studio Product Composer API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


class ComposerError(RuntimeError):
    pass


@dataclass(frozen=True)
class ComposerRender:
    image: bytes
    processor_version: str
    audit: dict[str, Any]


class StudioProductComposerClient:
    """Use only Composer's loopback HTTP API; never automate its browser UI."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8765",
        *,
        timeout_seconds: float = 60,
        poll_seconds: float = 1,
        max_wait_seconds: float = 900,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if urlparse(self.base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Studio Product Composer must be a loopback URL")
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self.max_wait_seconds = max_wait_seconds

    async def render_studio_light(
        self,
        source_path: Path,
        *,
        name: str,
        quality: str = "high",
        resolution: str = "4:5",
        output_format: str = "webp",
        output_quality: int = 90,
    ) -> ComposerRender:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_seconds) as client:
            health = await self._request(client, "GET", "/api/health")
            project = await self._request(client, "POST", "/api/projects", json={"name": name, "creation_intent": "IMAGES"})
            project_id = project["id"]
            await self._request(client, "PUT", f"/api/projects/{project_id}/background/preset/studio_light")
            composition = await self._request(client, "GET", f"/api/projects/{project_id}/composition")
            await self._request(
                client,
                "PUT",
                f"/api/projects/{project_id}/composition",
                json={"resolution": resolution, "remove_background": True, "expected_version": composition["version"]},
            )
            with source_path.open("rb") as source_file:
                imported = await self._request(
                    client,
                    "POST",
                    f"/api/projects/{project_id}/imports",
                    data={"source_type": "FILE_PICKER"},
                    files={"files": (source_path.name, source_file, self._media_type(source_path))},
                )
            imported_assets = [
                item for item in imported.get("assets", []) if str(item.get("state", "")).lower() == "imported"
            ]
            if len(imported_assets) != 1:
                raise ComposerError(f"Composer import did not produce exactly one asset: {imported}")
            asset_id = imported_assets[0]["id"]
            removal = await self._request(
                client, "POST", f"/api/projects/{project_id}/background-removal/start", json={"quality": quality}
            )
            removal = await self._wait_for_completion(
                client, f"/api/projects/{project_id}/background-removal/status", removal, "background removal"
            )
            if removal.get("failed") or removal.get("cancelled"):
                raise ComposerError(f"Composer background removal failed: {removal}")
            exported = await self._request(
                client,
                "POST",
                f"/api/projects/{project_id}/exports",
                json={"format": output_format, "resolution": resolution, "quality": output_quality, "confirm_ready_subset": False},
            )
            exported = await self._wait_for_completion(
                client, f"/api/projects/{project_id}/exports/{exported['id']}", exported, "export"
            )
            result = next((item for item in exported.get("results", []) if item.get("asset_id") == asset_id), None)
            if result is None or str(result.get("state", "")).lower() != "exported":
                raise ComposerError(f"Composer export did not complete for {asset_id}: {exported}")
            response = await client.get(
                f"/api/projects/{project_id}/exports/{exported['id']}/items/{asset_id}/download"
            )
            if response.is_error:
                raise ComposerError(f"Composer output download failed ({response.status_code}): {response.text[:500]}")
            return ComposerRender(
                image=response.content,
                processor_version=str(health.get("version") or "studio-product-composer"),
                audit={"project_id": project_id, "asset_id": asset_id, "export_id": exported["id"]},
            )

    async def _wait_for_completion(
        self, client: httpx.AsyncClient, path: str, payload: dict[str, Any], label: str
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.max_wait_seconds
        while payload.get("running", False):
            if asyncio.get_running_loop().time() >= deadline:
                raise ComposerError(f"Timed out waiting for Composer {label}")
            await asyncio.sleep(self.poll_seconds)
            payload = await self._request(client, "GET", path)
        return payload

    @staticmethod
    async def _request(client: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await client.request(method, path, **kwargs)
        if response.is_error:
            raise ComposerError(f"Composer {method} {path} failed ({response.status_code}): {response.text[:500]}")
        return response.json()

    @staticmethod
    def _media_type(path: Path) -> str:
        return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(
            path.suffix.lower(), "application/octet-stream"
        )
