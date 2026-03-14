from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .constants import DEFAULT_CATEGORY, DEFAULT_CATEGORY_DESCRIPTION, SUPPORTED_IMAGE_SUFFIXES
from .models import PluginPaths, StickerAsset, StickerAssetDraft, StickerGroup, StickerUsageEvent
from .utils import safe_filename


class StickerStorage:
    def __init__(self, paths: PluginPaths):
        self.paths = paths
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)
        self.paths.stickers_dir.mkdir(parents=True, exist_ok=True)
        self._init_database()
        self._ensure_default_groups()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.paths.metadata_db))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_database(self) -> None:
        conn = self._get_connection()
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sticker_assets_group_name ON sticker_assets(group_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sticker_usage_scope_key ON sticker_usage(scope_key, created_at DESC)")
        conn.commit()

    def _ensure_default_groups(self) -> None:
        if not self.list_groups():
            self.upsert_group(StickerGroup(name=DEFAULT_CATEGORY, description=DEFAULT_CATEGORY_DESCRIPTION))

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

    def upsert_group(self, group: StickerGroup) -> None:
        self._get_connection().execute(
            """
            INSERT INTO sticker_groups(name, description) VALUES(?, ?)
            ON CONFLICT(name) DO UPDATE SET description = excluded.description
            """,
            (group.name, group.description),
        )
        self._get_connection().commit()

    def list_groups(self) -> list[StickerGroup]:
        rows = self._get_connection().execute(
            "SELECT name, description FROM sticker_groups ORDER BY name"
        ).fetchall()
        return [StickerGroup(name=row[0], description=row[1]) for row in rows]

    def get_group(self, group_name: str) -> StickerGroup | None:
        row = self._get_connection().execute(
            "SELECT name, description FROM sticker_groups WHERE name = ?",
            (group_name,),
        ).fetchone()
        return StickerGroup(name=row[0], description=row[1]) if row else None

    def add_asset(self, draft: StickerAssetDraft) -> StickerAsset:
        asset_id = self._build_asset_id(draft.group_name)
        created_at = time.time()
        self._get_connection().execute(
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
        self._get_connection().commit()
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

    def query_assets(self, group_name: str | None = None, labels: tuple[str, ...] = (), limit: int | None = None) -> list[StickerAsset]:
        sql = (
            "SELECT asset_id, group_name, storage_key, original_name, mime_hint, description, source, created_at, usage_count, last_used_at, labels_json FROM sticker_assets"
        )
        params: list[object] = []
        if group_name:
            sql += " WHERE group_name = ?"
            params.append(group_name)
        sql += " ORDER BY usage_count DESC, created_at ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._get_connection().execute(sql, tuple(params)).fetchall()
        assets = [self._row_to_asset(row) for row in rows]
        if not labels:
            return assets
        expected = set(labels)
        return [asset for asset in assets if expected.issubset(set(asset.labels))]

    def get_asset(self, asset_id: str) -> StickerAsset | None:
        row = self._get_connection().execute(
            "SELECT asset_id, group_name, storage_key, original_name, mime_hint, description, source, created_at, usage_count, last_used_at, labels_json FROM sticker_assets WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()
        return self._row_to_asset(row) if row else None

    def get_asset_by_storage_key(self, storage_key: str) -> StickerAsset | None:
        row = self._get_connection().execute(
            "SELECT asset_id, group_name, storage_key, original_name, mime_hint, description, source, created_at, usage_count, last_used_at, labels_json FROM sticker_assets WHERE storage_key = ?",
            (storage_key,),
        ).fetchone()
        return self._row_to_asset(row) if row else None

    def delete_asset(self, asset_id: str) -> StickerAsset | None:
        asset = self.get_asset(asset_id)
        if asset is None:
            return None
        conn = self._get_connection()
        conn.execute("DELETE FROM sticker_usage WHERE asset_id = ?", (asset_id,))
        conn.execute("DELETE FROM sticker_assets WHERE asset_id = ?", (asset_id,))
        conn.commit()
        return asset

    def count_assets(self) -> int:
        row = self._get_connection().execute("SELECT COUNT(*) FROM sticker_assets").fetchone()
        return int(row[0]) if row else 0

    def record_usage(self, event: StickerUsageEvent) -> None:
        conn = self._get_connection()
        conn.execute(
            "INSERT INTO sticker_usage(asset_id, scope_key, created_at) VALUES(?, ?, ?)",
            (event.asset_id, event.scope_key, event.created_at),
        )
        conn.execute(
            "UPDATE sticker_assets SET usage_count = usage_count + 1, last_used_at = ? WHERE asset_id = ?",
            (event.created_at, event.asset_id),
        )
        conn.commit()

    def list_recent_usage(self, scope_key: str, limit: int) -> list[StickerUsageEvent]:
        rows = self._get_connection().execute(
            "SELECT asset_id, scope_key, created_at FROM sticker_usage WHERE scope_key = ? ORDER BY created_at DESC LIMIT ?",
            (scope_key, limit),
        ).fetchall()
        return [StickerUsageEvent(asset_id=row[0], scope_key=row[1], created_at=row[2]) for row in rows]

    def import_file(self, source_path: Path, group_name: str, preferred_name: str | None = None) -> tuple[str, str]:
        self.paths.stickers_dir.mkdir(parents=True, exist_ok=True)
        target_dir = self.paths.stickers_dir / group_name
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = safe_filename(preferred_name or source_path.name, source_path.suffix or ".jpg")
        target_path = target_dir / safe_name
        if target_path.exists():
            target_path = target_dir / f"{target_path.stem}_{int(time.time() * 1000)}{target_path.suffix}"
        shutil.copy2(source_path, target_path)
        storage_key = str(target_path.relative_to(self.paths.stickers_dir)).replace('\\', '/')
        return storage_key, target_path.name

    def resolve_path(self, storage_key: str) -> Path:
        path = Path(storage_key)
        if path.is_absolute():
            return path
        return (self.paths.stickers_dir / storage_key).resolve()

    def delete_file(self, storage_key: str) -> None:
        target = self.resolve_path(storage_key)
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

    def iter_all_assets(self) -> list[StickerAsset]:
        return self.query_assets()

    def get_all_tags(self) -> list[str]:
        tags: set[str] = set()
        for asset in self.query_assets():
            tags.update(asset.labels or (asset.group_name,))
        return sorted(tags)

    def get_tag_index(self) -> dict[str, list[str]]:
        index: dict[str, list[str]] = {}
        for asset in self.query_assets():
            for tag in asset.labels or (asset.group_name,):
                index.setdefault(tag, []).append(asset.asset_id)
        return index

    def get_memes_by_tags(self, tags: list[str]) -> list[dict[str, Any]]:
        normalized = tuple(tag for tag in tags if tag)
        assets = self.query_assets(labels=normalized)
        result = []
        for asset in assets:
            result.append(
                {
                    "meme_id": asset.asset_id,
                    "file_path": str(self.resolve_path(asset.storage_key)),
                    "tags": list(asset.labels) or [asset.group_name],
                    "source": asset.source,
                    "usage_count": asset.usage_count,
                    "added_time": asset.created_at,
                }
            )
        return result

    def increment_usage_count(self, asset_id: str) -> None:
        self.record_usage(StickerUsageEvent(asset_id=asset_id, scope_key="legacy-render", created_at=time.time()))

    def save_meme_with_tags(self, meme_id: str, file_path: str, tags: list[str], source: str = "") -> bool:
        try:
            normalized_storage_key = str(Path(file_path))
            if Path(file_path).is_absolute():
                normalized_storage_key = str(Path(file_path).resolve().relative_to(self.paths.stickers_dir.resolve())).replace('\\', '/')
            group_name = tags[0] if tags else DEFAULT_CATEGORY
            self.upsert_group(StickerGroup(name=group_name, description=DEFAULT_CATEGORY_DESCRIPTION if group_name == DEFAULT_CATEGORY else ""))
            self._get_connection().execute(
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
            self._get_connection().commit()
            return True
        except Exception as exc:
            logger.error(f"此刻的心情: 保存带标签表情包失败: {exc}", exc_info=True)
            return False

    def get_sticker_count(self) -> int:
        return self.count_assets()

    def get_usage_stats(self) -> dict[str, Any]:
        assets = self.query_assets()
        total_count = len(assets)
        total_usage = sum(asset.usage_count for asset in assets)
        avg_usage = total_usage / total_count if total_count else 0
        least_used = min(assets, key=lambda item: (item.usage_count, item.created_at), default=None)
        most_used = max(assets, key=lambda item: (item.usage_count, item.created_at), default=None)
        return {
            "total_count": total_count,
            "total_usage": total_usage,
            "avg_usage": avg_usage,
            "least_used": {"meme_id": least_used.asset_id, "usage_count": least_used.usage_count} if least_used else None,
            "most_used": {"meme_id": most_used.asset_id, "usage_count": most_used.usage_count} if most_used else None,
        }

    def get_least_used_memes(self, count: int) -> list[dict[str, Any]]:
        assets = sorted(self.query_assets(), key=lambda item: (item.usage_count, item.created_at))[:count]
        result = []
        for asset in assets:
            result.append(
                {
                    "meme_id": asset.asset_id,
                    "file_path": str(self.resolve_path(asset.storage_key)),
                    "tags": list(asset.labels) or [asset.group_name],
                    "source": asset.source,
                    "usage_count": asset.usage_count,
                    "added_time": asset.created_at,
                }
            )
        return result

    def get_random_sticker_path(self, category: str) -> str | None:
        assets = self.query_assets(group_name=category)
        if not assets:
            return None
        import random

        asset = random.choice(assets)
        self.record_usage(StickerUsageEvent(asset_id=asset.asset_id, scope_key="legacy-storage-random", created_at=time.time()))
        return str(self.resolve_path(asset.storage_key))

    def get_catalog_stickers_data(self) -> dict[str, str]:
        return {group.name: group.description for group in self.list_groups()}

    def get_available_stickers_data(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for group in self.list_groups():
            if self.query_assets(group_name=group.name, limit=1):
                result[group.name] = group.description
        return result

    def get_catalog_description(self, category: str) -> str | None:
        group = self.get_group(category)
        return group.description if group else None

    def has_sticker_assets(self, category: str) -> bool:
        return bool(self.query_assets(group_name=category, limit=1))

    def get_all_memes(self) -> list[dict[str, Any]]:
        return self.get_memes_by_tags([])

    def get_meme_by_id(self, asset_id: str) -> dict[str, Any] | None:
        asset = self.get_asset(asset_id)
        if asset is None:
            return None
        return {
            "meme_id": asset.asset_id,
            "file_path": str(self.resolve_path(asset.storage_key)),
            "tags": list(asset.labels) or [asset.group_name],
            "source": asset.source,
            "usage_count": asset.usage_count,
            "added_time": asset.created_at,
        }

    def get_meme_by_file_path(self, file_path: str | Path) -> dict[str, Any] | None:
        target = Path(file_path).resolve()
        for asset in self.query_assets():
            if self.resolve_path(asset.storage_key) == target:
                return {
                    "meme_id": asset.asset_id,
                    "file_path": str(target),
                    "tags": list(asset.labels) or [asset.group_name],
                    "source": asset.source,
                    "usage_count": asset.usage_count,
                    "added_time": asset.created_at,
                }
        return None

    def delete_meme(self, asset_id: str) -> bool:
        asset = self.delete_asset(asset_id)
        if asset is None:
            return False
        self.delete_file(asset.storage_key)
        return True

    def iter_all_sticker_files(self) -> list[Path]:
        return [self.resolve_path(asset.storage_key) for asset in self.query_assets()]
