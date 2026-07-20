from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from astrbot.api import logger

from .models import PluginPaths, StickerAssetDraft
from .utils import is_path_within_roots, normalize_meme_def, normalize_tags, safe_filename


class LegacyFormatService:
    """One-shot WebUI migration reader for the v1 library.

    The normal v2 storage never opens the legacy database. This service only
    reads the old schema when an administrator explicitly starts a format job.
    """

    def __init__(self, plugin):
        self.plugin = plugin
        self.data_dir: Path = plugin.paths.data_dir
        self.legacy_db = self.data_dir / "stickers.sqlite3"
        self.legacy_stickers = self.data_dir / "stickers"
        self.staging_root = self.data_dir / ".meme_format_staging"
        self._job: dict | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def prepare(self) -> dict:
        async with self._lock:
            if self._task and not self._task.done():
                return self.status()
            if await self.plugin.facade.storage.count_assets() > 0:
                raise ValueError("新库已有资产，不能格式化旧库")
            rows = await asyncio.to_thread(self._read_legacy_rows_sync)
            if not rows:
                raise ValueError("没有找到可格式化的旧库资产")
            job_id = uuid4().hex
            job_dir = self.staging_root / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            self._job = {
                "job_id": job_id,
                "status": "preparing",
                "total": len(rows),
                "processed": 0,
                "succeeded": 0,
                "failed": 0,
                "items": [],
                "created_at": int(time.time()),
                "staging_dir": str(job_dir),
            }
            await self._persist_manifest()
            self.plugin.facade.format_busy = True
            self._task = asyncio.create_task(self._run_prepare(rows))
            return self.status()

    def status(self) -> dict:
        if self._job is None:
            return {"status": "idle"}
        result = dict(self._job)
        result.pop("staging_dir", None)
        result["items"] = [
            {
                key: item.get(key)
                for key in (
                    "old_asset_id",
                    "old_storage_key",
                    "meme_def",
                    "tags",
                    "description",
                    "status",
                    "reason",
                )
            }
            for item in self._job.get("items", [])
        ]
        return result

    async def commit(self, job_id: str, confirm: bool, discard_failed: bool) -> dict:
        if not confirm:
            raise ValueError("格式化旧库提交需要 confirm=true")
        if not discard_failed:
            raise ValueError("当前策略要求明确确认 discard_failed=true")
        if self._job is None or self._job.get("job_id") != job_id:
            raise ValueError("格式化任务不存在或已过期")
        if self._task and not self._task.done():
            raise ValueError("格式化分析尚未完成")
        if self._job.get("status") != "ready":
            raise ValueError(f"当前任务不能提交: {self._job.get('status')}")

        job_dir = Path(str(self._job["staging_dir"])).resolve()
        temp_dir = job_dir / "meme_defs"
        temp_db = job_dir / "meme_defs.sqlite3"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_paths = PluginPaths(
            plugin_dir=self.plugin.paths.plugin_dir,
            data_dir=job_dir,
            stickers_dir=temp_dir,
            metadata_db=temp_db,
        )
        from .storage import StickerStorage

        temp_storage = StickerStorage(temp_paths)
        await temp_storage.initialize()
        successful_items = [
            item
            for item in self._job["items"]
            if item.get("status") == "success"
        ]
        try:
            for item in successful_items:
                staged_path = job_dir / "assets" / str(item["staged_name"])
                storage_key, _ = await temp_storage.import_file(
                    staged_path, str(item["meme_def"])
                )
                await temp_storage.add_asset(
                    StickerAssetDraft(
                        meme_def=str(item["meme_def"]),
                        storage_key=storage_key,
                        mime_hint=staged_path.suffix.lower(),
                        description=str(item["description"]),
                        source=str(item.get("source") or "legacy_format"),
                        tags=tuple(item["tags"]),
                        usage_count=int(item.get("usage_count") or 0),
                        last_used_at=item.get("last_used_at"),
                    )
                )
            if await temp_storage.count_assets() != len(successful_items):
                raise RuntimeError("临时新库资产数量校验失败")
        except Exception:
            await temp_storage.close()
            raise
        await temp_storage.close()

        final_db = self.plugin.paths.metadata_db.resolve()
        final_dir = self.plugin.paths.stickers_dir.resolve()
        old_db = self.legacy_db.resolve()
        old_dir = self.legacy_stickers.resolve()
        if final_db == old_db or final_dir == old_dir:
            raise RuntimeError("新旧数据路径重叠，拒绝执行破坏性格式化")

        try:
            if final_dir.exists():
                shutil.rmtree(final_dir)
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(temp_db), str(final_db))
            shutil.move(str(temp_dir), str(final_dir))
            await self.plugin.facade.storage.initialize()
            await self.plugin.facade.dedup.rebuild_index()
        except Exception:
            logger.error("此刻的心情: 切换格式化后的新库失败", exc_info=True)
            raise

        if old_db.exists():
            self._remove_file_with_retry(old_db)
        if old_dir.exists():
            shutil.rmtree(old_dir, ignore_errors=True)
        self._remove_tree_with_retry(job_dir)
        # 清理 staging 根目录（若已为空）
        try:
            self.staging_root.rmdir()
        except OSError:
            pass
        self._job["status"] = "committed"
        self.plugin.facade.format_busy = False
        return self.status()

    @staticmethod
    def _remove_file_with_retry(path: Path, attempts: int = 5) -> None:
        # Windows 上 sqlite 文件句柄释放可能有延迟，重试并强制 GC 回收连接对象。
        import gc

        for attempt in range(attempts):
            try:
                gc.collect()
                path.unlink()
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))

    @staticmethod
    def _remove_tree_with_retry(path: Path, attempts: int = 5) -> None:
        import gc

        for attempt in range(attempts):
            try:
                gc.collect()
                shutil.rmtree(path, ignore_errors=False)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    shutil.rmtree(path, ignore_errors=True)
                    return
                time.sleep(0.05 * (attempt + 1))

    async def cancel(self, job_id: str) -> dict:
        if self._job is None or self._job.get("job_id") != job_id:
            raise ValueError("格式化任务不存在或已过期")
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        shutil.rmtree(Path(str(self._job["staging_dir"])), ignore_errors=True)
        try:
            self.staging_root.rmdir()
        except OSError:
            pass
        self._job["status"] = "cancelled"
        self.plugin.facade.format_busy = False
        return self.status()

    async def _run_prepare(self, rows: list[dict]) -> None:
        assert self._job is not None
        job_dir = Path(str(self._job["staging_dir"]))
        assets_dir = job_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        used_defs: set[str] = set()
        used_tags: set[str] = set()
        try:
            for row in rows:
                item = await self._format_one(row, assets_dir, used_defs, used_tags)
                self._job["items"].append(item)
                self._job["processed"] += 1
                if item["status"] == "success":
                    self._job["succeeded"] += 1
                else:
                    self._job["failed"] += 1
                await self._persist_manifest()
            self._job["status"] = "ready"
        except asyncio.CancelledError:
            self._job["status"] = "cancelled"
            raise
        except Exception as exc:
            self._job["status"] = "failed"
            self._job["error"] = str(exc)
            logger.error("此刻的心情: 旧库格式化分析失败", exc_info=True)
        finally:
            await self._persist_manifest()
            if self._job.get("status") == "failed":
                self.plugin.facade.format_busy = False

    async def _format_one(
        self,
        row: dict,
        assets_dir: Path,
        used_defs: set[str],
        used_tags: set[str],
    ) -> dict:
        old_asset_id = str(row.get("asset_id") or "")
        old_storage_key = str(row.get("storage_key") or "")
        source_path = self._legacy_path(old_storage_key)
        base = {
            "old_asset_id": old_asset_id,
            "old_storage_key": old_storage_key,
            "status": "failed",
            "reason": "",
        }
        if source_path is None or not source_path.is_file():
            base["reason"] = "旧图片文件不存在或路径不在旧库目录内"
            return base
        reference_lines: list[str] = []
        old_description = str(row.get("description") or "").strip()
        old_tags = tuple(row.get("old_tags") or ())
        if old_description:
            reference_lines.append(f"旧描述: {old_description}")
        if old_tags:
            reference_lines.append(f"旧 tags: {', '.join(old_tags)}")
        review = await self.plugin.facade.review.review_image(
            str(source_path),
            reference_context="\n".join(reference_lines),
        )
        if not review.get("should_steal"):
            base["reason"] = str(review.get("reason") or "视觉模型未通过")
            return base
        meme_def = normalize_meme_def(str(review.get("filename") or ""))
        description = str(review.get("description") or "").strip()
        tags = normalize_tags(review.get("tags"))
        if not meme_def or not description or not tags:
            base["reason"] = "视觉模型结果缺少 filename、description 或 tags"
            return base
        meme_def = self._allocate_definition(meme_def, used_defs, used_tags)
        used_defs.add(meme_def.casefold())
        used_tags.update(tag.casefold() for tag in tags)
        staged_name = safe_filename(meme_def, source_path.suffix.lower() or ".png")
        shutil.copy2(source_path, assets_dir / staged_name)
        return {
            **base,
            "status": "success",
            "reason": str(review.get("reason") or ""),
            "meme_def": meme_def,
            "tags": list(tags),
            "description": description,
            "source": str(row.get("source") or "legacy_format"),
            "created_at": row.get("created_at"),
            "usage_count": row.get("usage_count") or 0,
            "last_used_at": row.get("last_used_at"),
            "staged_name": staged_name,
        }

    @staticmethod
    def _allocate_definition(
        base: str, used_defs: set[str], used_tags: set[str]
    ) -> str:
        candidate = base
        counter = 2
        while candidate.casefold() in used_defs or candidate.casefold() in used_tags:
            candidate = f"{base}_{counter}"
            counter += 1
        return candidate

    def _legacy_path(self, storage_key: str) -> Path | None:
        if not storage_key:
            return None
        candidate = Path(storage_key)
        if not candidate.is_absolute():
            candidate = self.legacy_stickers / candidate
        candidate = candidate.resolve()
        if not is_path_within_roots(candidate, (self.legacy_stickers, self.data_dir)):
            return None
        return candidate

    def _read_legacy_rows_sync(self) -> list[dict]:
        if not self.legacy_db.is_file():
            return []
        with closing(sqlite3.connect(str(self.legacy_db))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sticker_assets'"
            ).fetchone()
            if row is None:
                raise ValueError("旧数据库中没有 sticker_assets 表")
            rows = conn.execute(
                """
                SELECT asset_id, storage_key, description, source, created_at,
                       usage_count, last_used_at, labels_json
                FROM sticker_assets
                ORDER BY created_at ASC, asset_id ASC
                """
            ).fetchall()
        result = []
        for row in rows:
            try:
                old_tags = json.loads(row["labels_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                old_tags = []
            result.append(
                {
                    "asset_id": row["asset_id"],
                    "storage_key": row["storage_key"],
                    "description": row["description"],
                    "source": row["source"],
                    "created_at": row["created_at"],
                    "usage_count": row["usage_count"],
                    "last_used_at": row["last_used_at"],
                    "old_tags": normalize_tags(old_tags),
                }
            )
        return result

    async def _persist_manifest(self) -> None:
        if self._job is None:
            return
        manifest_path = Path(str(self._job["staging_dir"])) / "manifest.json"
        payload = dict(self._job)
        await asyncio.to_thread(
            manifest_path.write_text,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            "utf-8",
        )
