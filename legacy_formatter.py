from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from astrbot.api import logger

from .constants import SUPPORTED_IMAGE_SUFFIXES
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

    async def prepare(
        self,
        source: str = "legacy",
        plugin_dir_name: str | None = None,
    ) -> dict:
        async with self._lock:
            if self._task and not self._task.done():
                return self.status()
            # 断点续传：若已有未完成的 staging job，优先恢复而不是新建。
            resumed = await self._try_resume_from_staging()
            if resumed is not None:
                return resumed
            if source == "plugin_scan":
                target_dir = self._resolve_sibling_plugin_dir(plugin_dir_name)
                if target_dir is None:
                    raise ValueError(f"无效的插件目录: {plugin_dir_name or '(未指定)'}")
                rows = await asyncio.to_thread(
                    self._scan_plugin_images_sync, target_dir
                )
                if not rows:
                    raise ValueError(f"插件目录 {target_dir.name} 下没有找到图片文件")
            else:
                source = "legacy"
                target_dir = None
                rows = await asyncio.to_thread(self._read_legacy_rows_sync)
                if not rows:
                    raise ValueError("没有找到可格式化的旧库资产")
            job_id = uuid4().hex
            job_dir = self.staging_root / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            self._job = {
                "job_id": job_id,
                "status": "preparing",
                "source": source,
                "plugin_dir": target_dir.name if target_dir else "",
                "total": len(rows),
                "processed": 0,
                "succeeded": 0,
                "failed": 0,
                "items": [],
                "created_at": int(time.time()),
                "staging_dir": str(job_dir),
                "remaining_rows": rows,
            }
            await self._persist_manifest()
            self.plugin.facade.format_busy = True
            self._task = asyncio.create_task(self._run_prepare())
            return self.status()

    def list_sibling_plugin_dirs(self) -> list[dict]:
        """列出 plugin_data 下其他插件目录及其递归图片数量（供 WebUI 选择）。"""
        return self._list_sibling_plugin_dirs_sync()

    def _list_sibling_plugin_dirs_sync(self) -> list[dict]:
        root = self.data_dir.parent.resolve()
        if not root.is_dir():
            return []
        result: list[dict] = []
        try:
            children = sorted(
                (p for p in root.iterdir() if p.is_dir()),
                key=lambda p: p.name.casefold(),
            )
        except OSError:
            return []
        own = self.data_dir.resolve().name
        for child in children:
            if child.name == own:
                continue
            count = len(self._scan_plugin_images_sync(child))
            if count:
                result.append(
                    {"name": child.name, "path": str(child), "image_count": count}
                )
        return result

    def _resolve_sibling_plugin_dir(self, plugin_dir_name: str | None) -> Path | None:
        """校验并解析兄弟插件目录名，防路径穿越。返回 None 表示非法。"""
        name = str(plugin_dir_name or "").strip()
        if not name or name in {".", ".."}:
            return None
        if "/" in name or "\\" in name:
            return None
        root = self.data_dir.parent.resolve()
        candidate = (root / name).resolve()
        if candidate == self.data_dir.resolve():
            return None  # 排除本插件目录
        if not is_path_within_roots(candidate, (root,)):
            return None
        return candidate if candidate.is_dir() else None

    def _scan_plugin_images_sync(self, plugin_dir: Path) -> list[dict]:
        """递归扫描插件目录下的图片文件，伪造成统一 row。"""
        rows: list[dict] = []
        try:
            files = sorted(plugin_dir.rglob("*"))
        except OSError:
            return rows
        for path in files:
            if not path.is_file():
                continue
            if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
                continue
            try:
                resolved = path.resolve()
                stat = resolved.stat()
            except OSError:
                continue
            rows.append(
                {
                    "asset_id": f"scan-{uuid4().hex[:12]}",
                    "storage_key": str(resolved),
                    "description": "",
                    "source": f"scan_plugin:{plugin_dir.name}",
                    "created_at": stat.st_mtime,
                    "usage_count": 0,
                    "last_used_at": None,
                    "old_tags": (),
                }
            )
        return rows

    async def resume(self) -> dict:
        """显式恢复中断的格式化任务（供 WebUI 调用）。"""
        async with self._lock:
            if self._task and not self._task.done():
                return self.status()
            resumed = await self._try_resume_from_staging()
            if resumed is None:
                raise ValueError("没有可恢复的格式化任务")
            return resumed

    async def _try_resume_from_staging(self) -> dict | None:
        """从 staging manifest 恢复未完成的 job 并继续识别剩余项。"""
        job = await asyncio.to_thread(self._load_staging_manifest)
        if job is None:
            return None
        status = job.get("status")
        remaining = job.get("remaining_rows") or []
        # 可恢复：进行中、就绪，或失败/中断但仍有未识别项。
        if status not in ("preparing", "ready", "failed", "cancelled"):
            return None
        if status in ("failed", "cancelled") and not remaining:
            return None
        self._job = job
        self._job.pop("error", None)
        self.plugin.facade.format_busy = True
        if remaining:
            self._job["status"] = "preparing"
            await self._persist_manifest()
            self._task = asyncio.create_task(self._run_prepare())
        else:
            self._job["status"] = "ready"
            await self._persist_manifest()
        return self.status()

    def _load_staging_manifest(self) -> dict | None:
        if not self.staging_root.is_dir():
            return None
        try:
            job_dirs = sorted(
                (p for p in self.staging_root.iterdir() if p.is_dir()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return None
        for job_dir in job_dirs:
            manifest = job_dir / "manifest.json"
            if not manifest.is_file():
                continue
            try:
                job = json.loads(manifest.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(job, dict) and job.get("job_id"):
                job["staging_dir"] = str(job_dir)
                return job
        return None

    def status(self) -> dict:
        if self._job is None:
            return {"status": "idle"}
        result = dict(self._job)
        result.pop("staging_dir", None)
        result.pop("remaining_rows", None)
        result["pending_commit"] = sum(
            1
            for item in self._job.get("items", [])
            if item.get("status") == "success" and not item.get("committed")
        )
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
                    "committed",
                )
            }
            for item in self._job.get("items", [])
        ]
        return result

    async def commit(
        self,
        job_id: str,
        confirm: bool,
        discard_failed: bool,
        partial: bool = False,
    ) -> dict:
        if not confirm:
            raise ValueError("格式化旧库提交需要 confirm=true")
        if not discard_failed:
            raise ValueError("当前策略要求明确确认 discard_failed=true")
        if self._job is None or self._job.get("job_id") != job_id:
            raise ValueError("格式化任务不存在或已过期")
        if self._task and not self._task.done():
            if not partial:
                raise ValueError("格式化分析尚未完成；如需部分提交请使用 partial=true")
            # 部分提交：暂停识别，先提交已成功项。
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        status = self._job.get("status")
        if status not in ("ready", "preparing"):
            raise ValueError(f"当前任务不能提交: {status}")
        if not partial and status != "ready":
            raise ValueError(f"当前任务不能提交: {status}")

        successful_items = [
            item
            for item in self._job["items"]
            if item.get("status") == "success" and not item.get("committed")
        ]
        if not successful_items:
            raise ValueError("没有可提交的识图成功项")

        if partial:
            return await self._commit_partial(successful_items)
        return await self._commit_full(successful_items)

    async def _commit_full(self, successful_items: list[dict]) -> dict:
        """整体提交：把所有成功项合并进正式库，随后按来源收尾。"""
        await self._merge_items_into_storage(successful_items)
        await self._finalize_after_all_committed()
        return self.status()

    async def _merge_items_into_storage(self, successful_items: list[dict]) -> None:
        """把成功项逐张并入正式库并标记 committed（合并语义，不清空现有库）。"""
        job_dir = Path(str(self._job["staging_dir"])).resolve()
        storage = self.plugin.facade.storage
        for item in successful_items:
            staged_path = job_dir / "assets" / str(item["staged_name"])
            storage_key, _ = await storage.import_file(
                staged_path, str(item["meme_def"])
            )
            asset = await storage.add_asset(
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
            await self.plugin.facade.dedup.register_file(
                await storage.resolve_path(asset.storage_key), asset
            )
            item["committed"] = True
            staged_path.unlink(missing_ok=True)
        await self._persist_manifest()

    async def _commit_partial(self, successful_items: list[dict]) -> dict:
        """部分提交：把已成功项并入当前正式库，标记为已提交，保留剩余项。"""
        try:
            await self._merge_items_into_storage(successful_items)
        except Exception:
            logger.error("此刻的心情: 部分提交失败", exc_info=True)
            raise

        remaining_rows = self._job.get("remaining_rows") or []
        if remaining_rows:
            # 还有未识别项：恢复识别（used 集合由 manifest 中未提交+已提交项共同决定）。
            self._job["status"] = "preparing"
            await self._persist_manifest()
            self.plugin.facade.format_busy = True
            self._task = asyncio.create_task(self._run_prepare())
        else:
            # 全部识别完成且无剩余：按来源收尾。
            await self._finalize_after_all_committed()
        return self.status()

    async def _finalize_after_all_committed(self) -> None:
        """合并完成后的收尾：legacy 删除旧库，plugin_scan 保留源文件，仅清 staging。"""
        job_dir = Path(str(self._job["staging_dir"])).resolve()
        if self._job.get("source") != "plugin_scan":
            old_db = self.legacy_db.resolve()
            old_dir = self.legacy_stickers.resolve()
            if old_db.exists():
                self._remove_file_with_retry(old_db)
            if old_dir.exists():
                shutil.rmtree(old_dir, ignore_errors=True)
        self._remove_tree_with_retry(job_dir)
        try:
            self.staging_root.rmdir()
        except OSError:
            pass
        self._job["status"] = "committed"
        self.plugin.facade.format_busy = False
        # staging 已删除，不再持久化 manifest。

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

    async def _run_prepare(self) -> None:
        assert self._job is not None
        job_dir = Path(str(self._job["staging_dir"]))
        assets_dir = job_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        # 断点续传：used_defs/used_tags 需包含已处理项，避免恢复后 meme_def 重复。
        used_defs: set[str] = {
            str(item.get("meme_def") or "").casefold()
            for item in self._job["items"]
            if item.get("status") == "success" and item.get("meme_def")
        }
        used_tags: set[str] = {
            str(tag).casefold()
            for item in self._job["items"]
            for tag in (item.get("tags") or [])
        }
        current_row: dict | None = None
        try:
            while self._job.get("remaining_rows"):
                current_row = self._job["remaining_rows"].pop(0)
                item = await self._format_one(current_row, assets_dir, used_defs, used_tags)
                current_row = None
                self._job["items"].append(item)
                self._job["processed"] += 1
                if item["status"] == "success":
                    self._job["succeeded"] += 1
                else:
                    self._job["failed"] += 1
                await self._persist_manifest()
            self._job["status"] = "ready"
        except asyncio.CancelledError:
            # 中断时若当前项尚未完成识别，放回待识别队列，避免丢失。
            if current_row is not None:
                self._job["remaining_rows"].insert(0, current_row)
            self._job["status"] = "preparing"  # 保留进度，可再次 resume
            await self._persist_manifest()
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
            base["reason"] = "图片文件不存在或路径不在允许目录内"
            return base
        # 识图前 dHash 去重：与库中已有图片相似则跳过。
        try:
            duplicate = await self.plugin.facade.dedup.find_similar_duplicate(
                source_path
            )
        except Exception:
            duplicate = None
        if duplicate is not None:
            base["status"] = "duplicate"
            base["reason"] = f"与现有 {duplicate.meme_def} 重复"
            return base
        try:
            review = await self.plugin.facade.review.review_image(str(source_path))
        except Exception as exc:
            base["reason"] = f"视觉模型调用失败: {exc}"
            return base
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
        # 允许旧库目录、本插件 data_dir，以及 plugin_data 根（扫描其他插件来源）。
        roots = (self.legacy_stickers, self.data_dir, self.data_dir.parent)
        if not is_path_within_roots(candidate, roots):
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
