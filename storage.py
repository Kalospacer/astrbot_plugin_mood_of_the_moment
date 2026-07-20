from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .models import (
    PluginPaths,
    StickerAsset,
    StickerAssetDraft,
    StickerUsageEvent,
)
from .utils import normalize_tags, safe_filename


class StickerStorage:
    _SELECT_COLUMNS = (
        "asset_id, meme_def, storage_key, mime_hint, description, source, "
        "created_at, usage_count, last_used_at, tags_json"
    )

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

    async def close(self) -> None:
        return None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.paths.metadata_db))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_database_sync(self) -> None:
        self.paths.metadata_db.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            existing = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sticker_assets'"
            ).fetchone()
            if existing:
                columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(sticker_assets)")
                }
                required = {
                    "asset_id",
                    "meme_def",
                    "storage_key",
                    "mime_hint",
                    "description",
                    "source",
                    "created_at",
                    "usage_count",
                    "last_used_at",
                    "tags_json",
                }
                if not required.issubset(columns):
                    raise RuntimeError(
                        "检测到旧版表情包数据库。请先使用 WebUI 的“格式化旧库”功能，"
                        "不要直接让 v2 运行时读取旧数据库。"
                    )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sticker_assets (
                    asset_id TEXT PRIMARY KEY,
                    meme_def TEXT NOT NULL UNIQUE,
                    storage_key TEXT NOT NULL UNIQUE,
                    mime_hint TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    usage_count INTEGER NOT NULL DEFAULT 0,
                    last_used_at REAL,
                    tags_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sticker_assets_created_at ON sticker_assets(created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sticker_assets_usage_count ON sticker_assets(usage_count)"
            )
            conn.commit()

    @staticmethod
    def _build_asset_id(meme_def: str) -> str:
        return f"{meme_def}-{int(time.time() * 1000)}-{os.urandom(4).hex()}"

    @staticmethod
    def _tags_to_json(tags: tuple[str, ...]) -> str:
        return json.dumps(list(tags), ensure_ascii=False)

    @staticmethod
    def _json_to_tags(raw_json: str) -> tuple[str, ...]:
        try:
            value = json.loads(raw_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(f"此刻的心情: invalid tags_json ignored: {exc}")
            return ()
        return normalize_tags(value)

    def _row_to_asset(self, row: sqlite3.Row) -> StickerAsset:
        return StickerAsset(
            asset_id=str(row["asset_id"]),
            meme_def=str(row["meme_def"]),
            storage_key=str(row["storage_key"]),
            mime_hint=str(row["mime_hint"] or ""),
            description=str(row["description"] or ""),
            source=str(row["source"] or ""),
            created_at=float(row["created_at"] or 0.0),
            usage_count=int(row["usage_count"] or 0),
            last_used_at=(
                float(row["last_used_at"])
                if row["last_used_at"] is not None
                else None
            ),
            tags=self._json_to_tags(str(row["tags_json"] or "[]")),
        )

    async def add_asset(self, draft: StickerAssetDraft) -> StickerAsset:
        if not draft.meme_def.strip():
            raise ValueError("meme_def 不能为空")
        if not draft.description.strip():
            raise ValueError("description 不能为空")
        tags = normalize_tags(draft.tags)
        if not tags:
            raise ValueError("至少需要一个 tag")
        async with self._lock:
            return await asyncio.to_thread(
                self._add_asset_sync,
                StickerAssetDraft(
                    meme_def=draft.meme_def.strip(),
                    storage_key=draft.storage_key,
                    mime_hint=draft.mime_hint,
                    description=draft.description.strip(),
                    source=draft.source,
                    tags=tags,
                    usage_count=draft.usage_count,
                    last_used_at=draft.last_used_at,
                ),
            )

    def _add_asset_sync(self, draft: StickerAssetDraft) -> StickerAsset:
        asset_id = self._build_asset_id(draft.meme_def)
        created_at = time.time()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO sticker_assets(
                    asset_id, meme_def, storage_key, mime_hint, description, source,
                    created_at, usage_count, last_used_at, tags_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    draft.meme_def,
                    draft.storage_key,
                    draft.mime_hint,
                    draft.description,
                    draft.source,
                    created_at,
                    draft.usage_count,
                    draft.last_used_at,
                    self._tags_to_json(draft.tags),
                ),
            )
            conn.commit()
        return StickerAsset(
            asset_id=asset_id,
            meme_def=draft.meme_def,
            storage_key=draft.storage_key,
            mime_hint=draft.mime_hint,
            description=draft.description,
            source=draft.source,
            created_at=created_at,
            usage_count=draft.usage_count,
            last_used_at=draft.last_used_at,
            tags=draft.tags,
        )

    async def query_assets(
        self,
        tags: tuple[str, ...] = (),
        limit: int | None = None,
        match_all: bool = True,
    ) -> list[StickerAsset]:
        return await asyncio.to_thread(
            self._query_assets_sync,
            normalize_tags(tags),
            limit,
            match_all,
        )

    def _query_assets_sync(
        self,
        tags: tuple[str, ...],
        limit: int | None,
        match_all: bool,
    ) -> list[StickerAsset]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM sticker_assets "
                "ORDER BY created_at ASC, meme_def ASC"
            ).fetchall()
        assets = [self._row_to_asset(row) for row in rows]
        if tags:
            expected = {tag.casefold() for tag in tags}
            if match_all:
                assets = [
                    asset
                    for asset in assets
                    if expected.issubset({tag.casefold() for tag in asset.tags})
                ]
            else:
                assets = [
                    asset
                    for asset in assets
                    if expected & {tag.casefold() for tag in asset.tags}
                ]
        return assets[:limit] if limit is not None else assets

    async def get_asset(self, asset_id: str) -> StickerAsset | None:
        return await asyncio.to_thread(self._get_asset_sync, asset_id)

    def _get_asset_sync(self, asset_id: str) -> StickerAsset | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM sticker_assets WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
        return self._row_to_asset(row) if row else None

    async def get_asset_by_meme_def(self, meme_def: str) -> StickerAsset | None:
        return await asyncio.to_thread(self._get_asset_by_meme_def_sync, meme_def)

    def _get_asset_by_meme_def_sync(self, meme_def: str) -> StickerAsset | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM sticker_assets WHERE meme_def = ?",
                (meme_def,),
            ).fetchone()
        return self._row_to_asset(row) if row else None

    async def get_asset_by_storage_key(self, storage_key: str) -> StickerAsset | None:
        return await asyncio.to_thread(self._get_asset_by_storage_key_sync, storage_key)

    def _get_asset_by_storage_key_sync(self, storage_key: str) -> StickerAsset | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM sticker_assets WHERE storage_key = ?",
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
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM sticker_usage WHERE asset_id = ?", (asset_id,))
            conn.execute("DELETE FROM sticker_assets WHERE asset_id = ?", (asset_id,))
            conn.commit()
        return asset

    async def update_asset_metadata(
        self,
        asset_id: str,
        *,
        meme_def: str | None = None,
        description: str | None = None,
        source: str | None = None,
        tags: tuple[str, ...] | None = None,
    ) -> StickerAsset | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._update_asset_metadata_sync,
                asset_id,
                meme_def,
                description,
                source,
                tags,
            )

    def _update_asset_metadata_sync(
        self,
        asset_id: str,
        meme_def: str | None,
        description: str | None,
        source: str | None,
        tags: tuple[str, ...] | None,
    ) -> StickerAsset | None:
        asset = self._get_asset_sync(asset_id)
        if asset is None:
            return None
        target_def = (meme_def or asset.meme_def).strip()
        target_description = asset.description if description is None else description.strip()
        target_tags = asset.tags if tags is None else normalize_tags(tags)
        if not target_def or not target_description or not target_tags:
            raise ValueError("meme_def、description 和 tags 都不能为空")

        current_path = self._resolve_path_sync(asset.storage_key)
        target_path = current_path
        storage_key = asset.storage_key
        if target_def != asset.meme_def:
            target_path = self.paths.stickers_dir / safe_filename(
                target_def, current_path.suffix or asset.mime_hint or ".png"
            )
            if target_path.exists() and target_path.resolve() != current_path.resolve():
                raise ValueError(f"目标 meme_def 文件已存在: {target_def}")
            if current_path.exists():
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(current_path), str(target_path))
            storage_key = str(target_path.relative_to(self.paths.stickers_dir)).replace(
                "\\", "/"
            )

        try:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    UPDATE sticker_assets
                    SET meme_def = ?, storage_key = ?, description = ?,
                        source = ?, tags_json = ?
                    WHERE asset_id = ?
                    """,
                    (
                        target_def,
                        storage_key,
                        target_description,
                        asset.source if source is None else source.strip(),
                        self._tags_to_json(target_tags),
                        asset_id,
                    ),
                )
                conn.commit()
        except Exception:
            if target_path != current_path and target_path.exists() and not current_path.exists():
                current_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target_path), str(current_path))
            raise
        return self._get_asset_sync(asset_id)

    async def count_assets(self) -> int:
        return await asyncio.to_thread(self._count_assets_sync)

    def _count_assets_sync(self) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COUNT(*) FROM sticker_assets").fetchone()
        return int(row[0]) if row else 0

    async def prune_missing_assets(self) -> list[str]:
        async with self._lock:
            return await asyncio.to_thread(self._prune_missing_assets_sync)

    def _prune_missing_assets_sync(self) -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT asset_id, storage_key FROM sticker_assets"
            ).fetchall()
            stale = [
                str(row[0])
                for row in rows
                if not self._resolve_path_sync(str(row[1])).exists()
            ]
            if stale:
                conn.executemany(
                    "DELETE FROM sticker_usage WHERE asset_id = ?",
                    [(asset_id,) for asset_id in stale],
                )
                conn.executemany(
                    "DELETE FROM sticker_assets WHERE asset_id = ?",
                    [(asset_id,) for asset_id in stale],
                )
                conn.commit()
        return stale

    async def record_usage(self, event: StickerUsageEvent) -> None:
        async with self._lock:
            await asyncio.to_thread(self._record_usage_sync, event)

    def _record_usage_sync(self, event: StickerUsageEvent) -> None:
        with closing(self._connect()) as conn:
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
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT asset_id, scope_key, created_at FROM sticker_usage "
                "WHERE scope_key = ? ORDER BY created_at DESC LIMIT ?",
                (scope_key, limit),
            ).fetchall()
        return [
            StickerUsageEvent(
                asset_id=str(row[0]), scope_key=str(row[1]), created_at=float(row[2])
            )
            for row in rows
        ]

    async def import_file(self, source_path: Path, meme_def: str) -> tuple[str, str]:
        return await asyncio.to_thread(self._import_file_sync, source_path, meme_def)

    def _import_file_sync(self, source_path: Path, meme_def: str) -> tuple[str, str]:
        self.paths.stickers_dir.mkdir(parents=True, exist_ok=True)
        suffix = source_path.suffix.lower() or ".png"
        target_path = self.paths.stickers_dir / safe_filename(meme_def, suffix)
        if target_path.exists():
            target_path = self.paths.stickers_dir / (
                f"{target_path.stem}_{int(time.time() * 1000)}{target_path.suffix}"
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
        self._resolve_path_sync(storage_key).unlink(missing_ok=True)

    async def get_all_tags(self) -> list[str]:
        assets = await self.query_assets()
        return sorted({tag for asset in assets for tag in asset.tags}, key=str.casefold)

    async def get_all_meme_defs(self, limit: int | None = None) -> list[str]:
        assets = await self.query_assets()
        definitions = sorted(
            {asset.meme_def for asset in assets}, key=str.casefold
        )
        return definitions[:limit] if limit is not None else definitions

    async def get_tag_index(self) -> dict[str, list[str]]:
        assets = await self.query_assets()
        index: dict[str, list[str]] = {}
        for asset in assets:
            for tag in asset.tags:
                index.setdefault(tag, []).append(asset.asset_id)
        return index

    async def get_memes_by_tags(
        self, tags: list[str], match_all: bool = True
    ) -> list[dict[str, Any]]:
        assets = await self.query_assets(
            tags=normalize_tags(tags), match_all=match_all
        )
        result: list[dict[str, Any]] = []
        for asset in assets:
            resolved_path = await self.resolve_path(asset.storage_key)
            result.append(
                {
                    "asset_id": asset.asset_id,
                    "meme_def": asset.meme_def,
                    "file_path": str(resolved_path),
                    "tags": list(asset.tags),
                    "description": asset.description,
                    "source": asset.source,
                    "usage_count": asset.usage_count,
                    "last_used_at": asset.last_used_at,
                    "added_time": asset.created_at,
                }
            )
        return result

    async def get_meme_by_def(self, meme_def: str) -> dict[str, Any] | None:
        asset = await self.get_asset_by_meme_def(meme_def)
        return await self._asset_to_dict(asset) if asset else None

    async def get_meme_by_id(self, asset_id: str) -> dict[str, Any] | None:
        asset = await self.get_asset(asset_id)
        return await self._asset_to_dict(asset) if asset else None

    async def _asset_to_dict(self, asset: StickerAsset | None) -> dict[str, Any] | None:
        if asset is None:
            return None
        resolved_path = await self.resolve_path(asset.storage_key)
        return {
            "asset_id": asset.asset_id,
            "meme_def": asset.meme_def,
            "file_path": str(resolved_path),
            "tags": list(asset.tags),
            "description": asset.description,
            "source": asset.source,
            "usage_count": asset.usage_count,
            "last_used_at": asset.last_used_at,
            "added_time": asset.created_at,
        }

    async def increment_usage_count(
        self, asset_id: str, scope_key: str = "legacy-render"
    ) -> None:
        await self.record_usage(
            StickerUsageEvent(
                asset_id=asset_id, scope_key=scope_key, created_at=time.time()
            )
        )

    async def get_sticker_count(self) -> int:
        return await self.count_assets()

    async def get_usage_stats(self) -> dict[str, Any]:
        assets = await self.query_assets()
        total_count = len(assets)
        total_usage = sum(asset.usage_count for asset in assets)
        least_used = min(
            assets, key=lambda item: (item.usage_count, item.created_at), default=None
        )
        most_used = max(
            assets, key=lambda item: (item.usage_count, item.created_at), default=None
        )
        return {
            "total_count": total_count,
            "total_usage": total_usage,
            "avg_usage": total_usage / total_count if total_count else 0,
            "least_used": {
                "meme_def": least_used.meme_def,
                "usage_count": least_used.usage_count,
            }
            if least_used
            else None,
            "most_used": {
                "meme_def": most_used.meme_def,
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
        result: list[dict[str, Any]] = []
        for asset in assets:
            item = await self._asset_to_dict(asset)
            if item:
                result.append(item)
        return result

    async def get_all_memes(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for asset in await self.query_assets():
            item = await self._asset_to_dict(asset)
            if item:
                result.append(item)
        return result

    async def get_meme_by_file_path(
        self, file_path: str | Path
    ) -> dict[str, Any] | None:
        target = Path(file_path).resolve()
        for asset in await self.query_assets():
            if await self.resolve_path(asset.storage_key) == target:
                return await self._asset_to_dict(asset)
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
