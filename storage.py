from __future__ import annotations

import asyncio
import json
import random
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .constants import (
    DEFAULT_CATEGORY,
    DEFAULT_CATEGORY_DESCRIPTION,
    SUPPORTED_IMAGE_SUFFIXES,
)
from .models import (
    PluginPaths,
    StickerAsset,
    StickerAssetDraft,
    StickerGroup,
    StickerUsageEvent,
)
from .utils import safe_filename


class StickerStorage:
    def __init__(self, paths: PluginPaths):
        self.paths = paths
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self.paths.data_dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(
            self.paths.stickers_dir.mkdir, parents=True, exist_ok=True
        )
        async with self._lock:
            await asyncio.to_thread(self._init_database_sync)
            await asyncio.to_thread(self._ensure_default_groups_sync)

    async def close(self) -> None:
        return None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.paths.metadata_db))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_database_sync(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sticker_groups (
                    name TEXT PRIMARY KEY,
                    description TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sticker_assets (
                    asset_id TEXT PRIMARY KEY,
                    group_name TEXT NOT NULL,
                    storage_key TEXT NOT NULL UNIQUE,
                    original_name TEXT NOT NULL,
                    mime_hint TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    usage_count INTEGER NOT NULL DEFAULT 0,
                    last_used_at REAL,
                    labels_json TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sticker_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sticker_assets_group_name ON sticker_assets(group_name)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sticker_usage_scope_key ON sticker_usage(scope_key, created_at DESC)"
            )
            conn.commit()

    def _ensure_default_groups_sync(self) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM sticker_groups").fetchone()
            if int(row[0]) == 0:
                conn.execute(
                    "INSERT INTO sticker_groups(name, description) VALUES(?, ?)",
                    (DEFAULT_CATEGORY, DEFAULT_CATEGORY_DESCRIPTION),
                )
                conn.commit()

    def _build_asset_id(self, group_name: str) -> str:
        return f"{group_name}-{int(time.time() * 1000)}"

    def _labels_to_json(self, labels: tuple[str, ...]) -> str:
        return json.dumps([label for label in labels if label], ensure_ascii=False)

    def _json_to_labels(self, raw_json: str) -> tuple[str, ...]:
        try:
            data = json.loads(raw_json or "[]")
        except Exception as exc:
            logger.warning(f"此刻的心情: invalid labels_json ignored: {exc}")
            return ()
        if not isinstance(data, list):
            return ()
        return tuple(str(item).strip() for item in data if str(item).strip())

    def _row_to_asset(self, row: sqlite3.Row | tuple) -> StickerAsset:
        return StickerAsset(
            asset_id=row[0],
            group_name=row[1],
            storage_key=row[2],
            original_name=row[3],
            mime_hint=row[4],
            description=row[5],
            source=row[6],
            created_at=row[7],
            usage_count=row[8],
            last_used_at=row[9],
            labels=self._json_to_labels(row[10]),
        )

    async def upsert_group(self, group: StickerGroup) -> None:
        async with self._lock:
            await asyncio.to_thread(self._upsert_group_sync, group)

    def _upsert_group_sync(self, group: StickerGroup) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sticker_groups(name, description) VALUES(?, ?)
                ON CONFLICT(name) DO UPDATE SET description = excluded.description
                """,
                (group.name, group.description),
            )
            conn.commit()

    async def list_groups(self) -> list[StickerGroup]:
        return await asyncio.to_thread(self._list_groups_sync)

    def _list_groups_sync(self) -> list[StickerGroup]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, description FROM sticker_groups ORDER BY name"
            ).fetchall()
        return [StickerGroup(name=row[0], description=row[1]) for row in rows]

    async def get_group(self, group_name: str) -> StickerGroup | None:
        return await asyncio.to_thread(self._get_group_sync, group_name)

    def _get_group_sync(self, group_name: str) -> StickerGroup | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT name, description FROM sticker_groups WHERE name = ?",
                (group_name,),
            ).fetchone()
        return StickerGroup(name=row[0], description=row[1]) if row else None

    async def add_asset(self, draft: StickerAssetDraft) -> StickerAsset:
        async with self._lock:
            return await asyncio.to_thread(self._add_asset_sync, draft)

    def _add_asset_sync(self, draft: StickerAssetDraft) -> StickerAsset:
        asset_id = self._build_asset_id(draft.group_name)
        created_at = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sticker_assets(
                    asset_id, group_name, storage_key, original_name, mime_hint,
                    description, source, created_at, usage_count, last_used_at, labels_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)
                """,
                (
                    asset_id,
                    draft.group_name,
                    draft.storage_key,
                    draft.original_name,
                    draft.mime_hint,
                    draft.description,
                    draft.source,
                    created_at,
                    self._labels_to_json(draft.labels),
                ),
            )
            conn.commit()
        return StickerAsset(
            asset_id=asset_id,
            group_name=draft.group_name,
            storage_key=draft.storage_key,
            original_name=draft.original_name,
            mime_hint=draft.mime_hint,
            description=draft.description,
            source=draft.source,
            created_at=created_at,
            labels=draft.labels,
        )

    async def query_assets(
        self,
        group_name: str | None = None,
        labels: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> list[StickerAsset]:
        return await asyncio.to_thread(
            self._query_assets_sync, group_name, labels, limit
        )

    def _query_assets_sync(
        self,
        group_name: str | None = None,
        labels: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> list[StickerAsset]:
        sql = "SELECT asset_id, group_name, storage_key, original_name, mime_hint, description, source, created_at, usage_count, last_used_at, labels_json FROM sticker_assets"
        params: list[object] = []
        if group_name:
            sql += " WHERE group_name = ?"
            params.append(group_name)
        sql += " ORDER BY usage_count DESC, created_at ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        assets = [self._row_to_asset(row) for row in rows]
        if not labels:
            return assets
        expected = set(labels)
        return [asset for asset in assets if expected.issubset(set(asset.labels))]

    async def get_asset(self, asset_id: str) -> StickerAsset | None:
        return await asyncio.to_thread(self._get_asset_sync, asset_id)

    def _get_asset_sync(self, asset_id: str) -> StickerAsset | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT asset_id, group_name, storage_key, original_name, mime_hint, description, source, created_at, usage_count, last_used_at, labels_json FROM sticker_assets WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
        return self._row_to_asset(row) if row else None

    async def get_asset_by_storage_key(self, storage_key: str) -> StickerAsset | None:
        return await asyncio.to_thread(self._get_asset_by_storage_key_sync, storage_key)

    def _get_asset_by_storage_key_sync(self, storage_key: str) -> StickerAsset | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT asset_id, group_name, storage_key, original_name, mime_hint, description, source, created_at, usage_count, last_used_at, labels_json FROM sticker_assets WHERE storage_key = ?",
                (storage_key,),
            ).fetchone()
        return self._row_to_asset(row) if row else None

    async def delete_asset(self, asset_id: str) -> StickerAsset | None:
        async with self._lock:
            return await asyncio.to_thread(self._delete_asset_sync, asset_id)

    def _delete_asset_sync(self, asset_id: str) -> StickerAsset | None:
        asset = self._get_asset_sync(asset_id)
        if asset is None:
            return None
        with self._connect() as conn:
            conn.execute("DELETE FROM sticker_usage WHERE asset_id = ?", (asset_id,))
            conn.execute("DELETE FROM sticker_assets WHERE asset_id = ?", (asset_id,))
            conn.commit()
        return asset

    async def count_assets(self) -> int:
        return await asyncio.to_thread(self._count_assets_sync)

    def _count_assets_sync(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM sticker_assets").fetchone()
        return int(row[0]) if row else 0

    async def prune_missing_assets(self) -> list[str]:
        async with self._lock:
            return await asyncio.to_thread(self._prune_missing_assets_sync)

    def _prune_missing_assets_sync(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT asset_id, storage_key FROM sticker_assets"
            ).fetchall()
            stale_asset_ids: list[str] = []
            for row in rows:
                asset_id = str(row[0] or "").strip()
                storage_key = str(row[1] or "").strip()
                if not asset_id or not storage_key:
                    continue
                if not self._resolve_path_sync(storage_key).exists():
                    stale_asset_ids.append(asset_id)
            if not stale_asset_ids:
                return []
            conn.executemany(
                "DELETE FROM sticker_usage WHERE asset_id = ?",
                [(asset_id,) for asset_id in stale_asset_ids],
            )
            conn.executemany(
                "DELETE FROM sticker_assets WHERE asset_id = ?",
                [(asset_id,) for asset_id in stale_asset_ids],
            )
            conn.commit()
        return stale_asset_ids

    async def record_usage(self, event: StickerUsageEvent) -> None:
        async with self._lock:
            await asyncio.to_thread(self._record_usage_sync, event)

    def _record_usage_sync(self, event: StickerUsageEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sticker_usage(asset_id, scope_key, created_at) VALUES(?, ?, ?)",
                (event.asset_id, event.scope_key, event.created_at),
            )
            conn.execute(
                "UPDATE sticker_assets SET usage_count = usage_count + 1, last_used_at = ? WHERE asset_id = ?",
                (event.created_at, event.asset_id),
            )
            conn.commit()

    async def list_recent_usage(
        self, scope_key: str, limit: int
    ) -> list[StickerUsageEvent]:
        return await asyncio.to_thread(self._list_recent_usage_sync, scope_key, limit)

    def _list_recent_usage_sync(
        self, scope_key: str, limit: int
    ) -> list[StickerUsageEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT asset_id, scope_key, created_at FROM sticker_usage WHERE scope_key = ? ORDER BY created_at DESC LIMIT ?",
                (scope_key, limit),
            ).fetchall()
        return [
            StickerUsageEvent(asset_id=row[0], scope_key=row[1], created_at=row[2])
            for row in rows
        ]

    async def import_file(
        self,
        source_path: Path,
        group_name: str,
        preferred_name: str | None = None,
    ) -> tuple[str, str]:
        return await asyncio.to_thread(
            self._import_file_sync, source_path, group_name, preferred_name
        )

    def _import_file_sync(
        self,
        source_path: Path,
        group_name: str,
        preferred_name: str | None = None,
    ) -> tuple[str, str]:
        self.paths.stickers_dir.mkdir(parents=True, exist_ok=True)
        target_dir = self.paths.stickers_dir / group_name
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = safe_filename(
            preferred_name or source_path.name, source_path.suffix or ".jpg"
        )
        target_path = target_dir / safe_name
        if target_path.exists():
            target_path = (
                target_dir
                / f"{target_path.stem}_{int(time.time() * 1000)}{target_path.suffix}"
            )
        shutil.copy2(source_path, target_path)
        storage_key = str(target_path.relative_to(self.paths.stickers_dir)).replace(
            "\\", "/"
        )
        return storage_key, target_path.name

    async def resolve_path(self, storage_key: str) -> Path:
        return await asyncio.to_thread(self._resolve_path_sync, storage_key)

    def _resolve_path_sync(self, storage_key: str) -> Path:
        path = Path(storage_key)
        if path.is_absolute():
            return path
        return (self.paths.stickers_dir / storage_key).resolve()

    async def delete_file(self, storage_key: str) -> None:
        await asyncio.to_thread(self._delete_file_sync, storage_key)

    def _delete_file_sync(self, storage_key: str) -> None:
        target = self._resolve_path_sync(storage_key)
        target.unlink(missing_ok=True)
        parent = target.parent
        while parent != self.paths.stickers_dir and parent.exists():
            try:
                next(parent.iterdir())
                break
            except StopIteration:
                parent.rmdir()
                parent = parent.parent
            except OSError:
                break

    async def iter_all_assets(self) -> list[StickerAsset]:
        return await self.query_assets()

    async def get_all_tags(self) -> list[str]:
        assets = await self.query_assets()
        tags: set[str] = set()
        for asset in assets:
            tags.update(asset.labels or (asset.group_name,))
        return sorted(tags)

    async def get_tag_index(self) -> dict[str, list[str]]:
        assets = await self.query_assets()
        index: dict[str, list[str]] = {}
        for asset in assets:
            for tag in asset.labels or (asset.group_name,):
                index.setdefault(tag, []).append(asset.asset_id)
        return index

    async def get_memes_by_tags(self, tags: list[str]) -> list[dict[str, Any]]:
        normalized = tuple(tag for tag in tags if tag)
        assets = await self.query_assets(labels=normalized)
        result = []
        for asset in assets:
            resolved_path = await self.resolve_path(asset.storage_key)
            result.append(
                {
                    "meme_id": asset.asset_id,
                    "file_path": str(resolved_path),
                    "tags": list(asset.labels) or [asset.group_name],
                    "source": asset.source,
                    "usage_count": asset.usage_count,
                    "added_time": asset.created_at,
                }
            )
        return result

    async def increment_usage_count(
        self, asset_id: str, scope_key: str = "legacy-render"
    ) -> None:
        await self.record_usage(
            StickerUsageEvent(
                asset_id=asset_id, scope_key=scope_key, created_at=time.time()
            )
        )

    async def update_asset_source(self, asset_id: str, source: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._update_asset_source_sync, asset_id, source)

    def _update_asset_source_sync(self, asset_id: str, source: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sticker_assets SET source = ? WHERE asset_id = ?",
                (source, asset_id),
            )
            conn.commit()

    async def save_meme_with_tags(
        self,
        meme_id: str,
        file_path: str,
        tags: list[str],
        source: str = "",
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._save_meme_with_tags_sync,
                meme_id,
                file_path,
                tags,
                source,
            )

    def _save_meme_with_tags_sync(
        self,
        meme_id: str,
        file_path: str,
        tags: list[str],
        source: str = "",
    ) -> bool:
        try:
            normalized_storage_key = str(Path(file_path))
            if Path(file_path).is_absolute():
                normalized_storage_key = str(
                    Path(file_path)
                    .resolve()
                    .relative_to(self.paths.stickers_dir.resolve())
                ).replace("\\", "/")
            group_name = tags[0] if tags else DEFAULT_CATEGORY
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO sticker_groups(name, description) VALUES(?, ?)
                    ON CONFLICT(name) DO UPDATE SET description = excluded.description
                    """,
                    (
                        group_name,
                        DEFAULT_CATEGORY_DESCRIPTION
                        if group_name == DEFAULT_CATEGORY
                        else "",
                    ),
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO sticker_assets(
                        asset_id, group_name, storage_key, original_name, mime_hint,
                        description, source, created_at, usage_count, last_used_at, labels_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT usage_count FROM sticker_assets WHERE asset_id = ?), 0), COALESCE((SELECT last_used_at FROM sticker_assets WHERE asset_id = ?), NULL), ?)
                    """,
                    (
                        meme_id,
                        group_name,
                        normalized_storage_key,
                        Path(file_path).name,
                        Path(file_path).suffix.lower(),
                        ", ".join(tags),
                        source,
                        time.time(),
                        meme_id,
                        meme_id,
                        self._labels_to_json(tuple(tags)),
                    ),
                )
                conn.commit()
            return True
        except Exception as exc:
            logger.error(f"此刻的心情: 保存带标签表情包失败: {exc}", exc_info=True)
            return False

    async def get_sticker_count(self) -> int:
        return await self.count_assets()

    async def get_usage_stats(self) -> dict[str, Any]:
        assets = await self.query_assets()
        total_count = len(assets)
        total_usage = sum(asset.usage_count for asset in assets)
        avg_usage = total_usage / total_count if total_count else 0
        least_used = min(
            assets, key=lambda item: (item.usage_count, item.created_at), default=None
        )
        most_used = max(
            assets, key=lambda item: (item.usage_count, item.created_at), default=None
        )
        return {
            "total_count": total_count,
            "total_usage": total_usage,
            "avg_usage": avg_usage,
            "least_used": {
                "meme_id": least_used.asset_id,
                "usage_count": least_used.usage_count,
            }
            if least_used
            else None,
            "most_used": {
                "meme_id": most_used.asset_id,
                "usage_count": most_used.usage_count,
            }
            if most_used
            else None,
        }

    async def get_least_used_memes(self, count: int) -> list[dict[str, Any]]:
        assets = sorted(
            await self.query_assets(),
            key=lambda item: (item.usage_count, item.created_at),
        )[:count]
        result = []
        for asset in assets:
            resolved_path = await self.resolve_path(asset.storage_key)
            result.append(
                {
                    "meme_id": asset.asset_id,
                    "file_path": str(resolved_path),
                    "tags": list(asset.labels) or [asset.group_name],
                    "source": asset.source,
                    "usage_count": asset.usage_count,
                    "added_time": asset.created_at,
                }
            )
        return result

    async def get_random_sticker_path(self, category: str) -> str | None:
        assets = await self.query_assets(group_name=category)
        if not assets:
            return None
        asset = random.choice(assets)
        await self.record_usage(
            StickerUsageEvent(
                asset_id=asset.asset_id,
                scope_key="legacy-storage-random",
                created_at=time.time(),
            )
        )
        resolved_path = await self.resolve_path(asset.storage_key)
        return str(resolved_path)

    async def get_catalog_stickers_data(self) -> dict[str, str]:
        return {group.name: group.description for group in await self.list_groups()}

    async def get_available_stickers_data(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for group in await self.list_groups():
            if await self.query_assets(group_name=group.name, limit=1):
                result[group.name] = group.description
        return result

    async def get_catalog_description(self, category: str) -> str | None:
        group = await self.get_group(category)
        return group.description if group else None

    async def has_sticker_assets(self, category: str) -> bool:
        return bool(await self.query_assets(group_name=category, limit=1))

    async def get_all_memes(self) -> list[dict[str, Any]]:
        return await self.get_memes_by_tags([])

    async def get_meme_by_id(self, asset_id: str) -> dict[str, Any] | None:
        asset = await self.get_asset(asset_id)
        if asset is None:
            return None
        resolved_path = await self.resolve_path(asset.storage_key)
        return {
            "meme_id": asset.asset_id,
            "file_path": str(resolved_path),
            "tags": list(asset.labels) or [asset.group_name],
            "source": asset.source,
            "usage_count": asset.usage_count,
            "added_time": asset.created_at,
        }

    async def get_meme_by_file_path(
        self, file_path: str | Path
    ) -> dict[str, Any] | None:
        target = Path(file_path).resolve()
        for asset in await self.query_assets():
            resolved_path = await self.resolve_path(asset.storage_key)
            if resolved_path == target:
                return {
                    "meme_id": asset.asset_id,
                    "file_path": str(target),
                    "tags": list(asset.labels) or [asset.group_name],
                    "source": asset.source,
                    "usage_count": asset.usage_count,
                    "added_time": asset.created_at,
                }
        return None

    async def delete_meme(self, asset_id: str) -> bool:
        asset = await self.delete_asset(asset_id)
        if asset is None:
            return False
        await self.delete_file(asset.storage_key)
        return True

    async def iter_all_sticker_files(self) -> list[Path]:
        return [
            await self.resolve_path(asset.storage_key)
            for asset in await self.query_assets()
        ]
