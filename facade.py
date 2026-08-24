from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from astrbot.api import logger
from PIL import Image, UnidentifiedImageError

from .constants import SUPPORTED_IMAGE_SUFFIXES
from .dedup import DHashDedupService
from .downloader import RemoteImageDownloader
from .models import (
    DeleteResult,
    DecoratedContent,
    IngestResult,
    PluginPaths,
    StickerAssetDraft,
    StickerUsageEvent,
)
from .render import StickerRenderer
from .review import ReviewService
from .storage import StickerStorage
from .utils import (
    get_allowed_image_roots,
    is_path_within_roots,
    normalize_meme_def,
    normalize_tags,
    resolve_user_path,
)


class PluginFacade:
    def __init__(self, paths: PluginPaths, context=None, plugin_config=None):
        self.paths = paths
        self.context = context
        self.plugin_config = plugin_config or {}
        self.storage = StickerStorage(paths)
        self.dedup = DHashDedupService(storage=self.storage, paths=paths)
        self.review = ReviewService(context=context, plugin_config=self.plugin_config)
        self.downloader = RemoteImageDownloader()
        self.renderer = StickerRenderer(
            storage=self.storage,
            max_stickers_per_message=int(
                self.plugin_config.get("max_stickers_per_message", 1) or 1
            ),
            max_prompt_tags=int(self.plugin_config.get("max_prompt_tags", 30) or 30),
            max_prompt_meme_defs=int(
                self.plugin_config.get("max_prompt_meme_defs", 30) or 30
            ),
        )
        self.allowed_image_roots = get_allowed_image_roots(
            data_dir=self.paths.data_dir,
            extra_roots=(
                self.paths.plugin_dir,
                self.paths.data_dir,
                self.downloader.temp_dir,
            ),
        )
        self.inflight_sources: set[str] = set()
        self._inflight_lock = asyncio.Lock()
        self._ingest_lock = asyncio.Lock()
        self._cleanup_last_run = 0.0
        self.format_busy = False

    def set_context(self, context) -> None:
        self.context = context
        self.review.set_context(context)

    def set_plugin_config(self, plugin_config: dict | None) -> None:
        self.plugin_config = plugin_config or {}
        self.review.set_plugin_config(self.plugin_config)
        self.renderer.max_stickers_per_message = max(
            0, int(self.plugin_config.get("max_stickers_per_message", 1) or 1)
        )
        self.renderer.max_prompt_tags = max(
            0, int(self.plugin_config.get("max_prompt_tags", 30) or 30)
        )
        self.renderer.max_prompt_meme_defs = max(
            0, int(self.plugin_config.get("max_prompt_meme_defs", 30) or 30)
        )

    async def startup(self) -> None:
        try:
            deleted_temp_files = await asyncio.to_thread(
                self.downloader.cleanup_temp_dir
            )
            if deleted_temp_files:
                logger.info(
                    f"此刻的心情: 启动时清理下载临时文件 {deleted_temp_files} 个"
                )
        except Exception:
            logger.warning(
                "此刻的心情: 启动时清理下载临时文件失败，但将继续初始化流程",
                exc_info=True,
            )
        await self.storage.initialize()
        stale_asset_ids = await self.storage.prune_missing_assets()
        if stale_asset_ids:
            logger.info(
                f"此刻的心情: 启动时清理失效资产 {len(stale_asset_ids)} 个"
            )
        await self.dedup.initialize()
        await self._prune_orphan_thumbnails()

    async def _prune_orphan_thumbnails(self) -> None:
        """清理无对应资产的孤儿缩略图。"""
        thumb_dir = self.paths.data_dir / ".thumbnails"
        if not thumb_dir.is_dir():
            return

        def _clean() -> int:
            removed = 0
            valid_ids = set()
            import sqlite3 as _sqlite3

            if self.paths.metadata_db.is_file():
                try:
                    with _sqlite3.connect(str(self.paths.metadata_db)) as conn:
                        valid_ids = {
                            str(row[0])
                            for row in conn.execute("SELECT asset_id FROM sticker_assets")
                        }
                except Exception:
                    # 数据库读失败时不做任何清理，避免误删全部缩略图。
                    return 0
            for path in thumb_dir.glob("*.webp"):
                if path.stem not in valid_ids:
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        pass
            return removed

        removed_count = await asyncio.to_thread(_clean)
        if removed_count:
            logger.info(f"此刻的心情: 启动时清理孤儿缩略图 {removed_count} 个")

    async def shutdown(self) -> None:
        await self.storage.close()

    async def build_llm_summary(self) -> str:
        return await self.renderer.build_prompt_catalog()

    async def decorate_text(self, text: str, scope_key: str) -> DecoratedContent:
        return await self.renderer.decorate_text(text=text, scope_key=scope_key)

    @staticmethod
    def _is_remote_image_source(image_source: str) -> bool:
        parsed = urlparse(image_source)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _mask_identifier(value: str) -> str:
        text = value.strip()
        if len(text) <= 4:
            return text or "-"
        return f"{text[:2]}***{text[-2:]}"

    def summarize_image_source(self, image_source: str) -> str:
        if self._is_remote_image_source(image_source):
            parsed = urlparse(image_source)
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"
        path = Path(image_source)
        return f"local:{path.name or '[unknown]'}"

    @staticmethod
    def _normalize_text(value: object) -> str:
        return str(value).strip() if value is not None else ""

    @classmethod
    def _has_store_emoji_markers(
        cls, item, raw_image_data: dict | None = None
    ) -> tuple[bool, list[str]]:
        matched_attrs: list[str] = []
        if raw_image_data:
            matched_attrs.extend(
                attr
                for attr in ("emoji_id", "emoji_package_id", "key")
                if raw_image_data.get(attr)
            )
        if not matched_attrs:
            matched_attrs.extend(
                attr
                for attr in ("emoji_id", "emoji_package_id", "key")
                if getattr(item, attr, None)
            )
        return bool(matched_attrs), matched_attrs

    @classmethod
    def _is_emoji_type_image(cls, item, raw_image_data: dict | None = None) -> bool:
        def is_summary(value: object) -> bool:
            text = cls._normalize_text(value).lower()
            return bool(text) and any(
                marker in text for marker in ("表情", "emoji", "sticker")
            )

        def is_emoji_sub_type(value: object) -> bool:
            try:
                return int(value) == 1
            except (TypeError, ValueError):
                return False

        has_markers, _ = cls._has_store_emoji_markers(item, raw_image_data)
        if has_markers:
            return True

        payloads: list[dict] = []
        if isinstance(raw_image_data, dict):
            payloads.append(raw_image_data)
        item_dict = getattr(item, "__dict__", None)
        if isinstance(item_dict, dict):
            payloads.append(item_dict)
        try:
            raw_dict = item.toDict()
            if isinstance(raw_dict, dict):
                data = raw_dict.get("data")
                payloads.append(data if isinstance(data, dict) else raw_dict)
        except Exception:
            pass

        for payload in payloads:
            if is_emoji_sub_type(payload.get("sub_type")) or is_emoji_sub_type(
                payload.get("subType")
            ):
                return True
            if is_summary(payload.get("summary")):
                return True
            image_type = cls._normalize_text(
                payload.get("type")
                or payload.get("imageType")
                or payload.get("image_type")
            ).lower()
            if image_type in {"emoji", "sticker", "face", "meme"}:
                return True
            url = cls._normalize_text(payload.get("url"))
            if "vip.qq.com/club/item/parcel" in url or "gxh.vip.qq.com" in url:
                return True
        return is_emoji_sub_type(getattr(item, "subType", None))

    @staticmethod
    def extract_image_segment_payloads(raw_message) -> list[dict]:
        segments = getattr(raw_message, "message", None)
        if segments is None and isinstance(raw_message, dict):
            segments = raw_message.get("message")
        if not isinstance(segments, list):
            return []
        payloads: list[dict] = []
        for segment in segments:
            if not isinstance(segment, dict) or segment.get("type") != "image":
                continue
            data = segment.get("data")
            if isinstance(data, dict):
                payloads.append(data)
        return payloads

    @staticmethod
    def get_image_source(item, raw_image_data: dict | None = None) -> str:
        if raw_image_data:
            for key in ("url", "path", "file"):
                value = str(raw_image_data.get(key) or "").strip()
                if value:
                    return value
        for key in ("url", "path", "file"):
            value = str(getattr(item, key, "") or "").strip()
            if value:
                return value
        return ""

    async def _validate_image_file(self, image_path: Path) -> bool:
        return await asyncio.to_thread(self._validate_image_file_sync, image_path)

    @staticmethod
    def _validate_image_file_sync(image_path: Path) -> bool:
        try:
            with Image.open(image_path) as image:
                image.verify()
            return True
        except (OSError, UnidentifiedImageError):
            return False

    async def _allocate_meme_def(self, candidate: str) -> str:
        base = normalize_meme_def(candidate)
        if not base:
            return ""
        assets = await self.storage.query_assets()
        used_defs = {asset.meme_def.casefold() for asset in assets}
        used_tags = {tag.casefold() for asset in assets for tag in asset.tags}
        candidate_def = base
        counter = 2
        while candidate_def.casefold() in used_defs or candidate_def.casefold() in used_tags:
            candidate_def = f"{base}_{counter}"
            counter += 1
        return candidate_def

    async def ingest_local_file(
        self,
        source_path: str,
        meme_def: str,
        tags: tuple[str, ...] | list[str],
        description: str,
        source: str = "manual",
    ) -> IngestResult:
        if self.format_busy:
            return IngestResult(ok=False, message="旧库格式化进行中，暂时不能导入图片")
        resolved = resolve_user_path(source_path)
        return await self._ingest_resolved_file(
            resolved=resolved,
            meme_def=meme_def,
            tags=tuple(tags),
            description=description,
            source=source,
            skip_validation=False,
            skip_duplicate_check=False,
        )

    async def _ingest_resolved_file(
        self,
        resolved: Path,
        meme_def: str,
        tags: tuple[str, ...],
        description: str,
        source: str,
        *,
        skip_validation: bool,
        skip_duplicate_check: bool,
    ) -> IngestResult:
        if not resolved.exists() or not resolved.is_file():
            return IngestResult(ok=False, message=f"图片不存在或不是文件: {resolved}")
        if resolved.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
            return IngestResult(ok=False, message=f"不支持的图片格式: {resolved.suffix or '无扩展名'}")
        if not is_path_within_roots(resolved, self.allowed_image_roots):
            return IngestResult(ok=False, message=f"图片路径超出允许范围: {resolved}")
        if not skip_validation and not await self._validate_image_file(resolved):
            return IngestResult(ok=False, message=f"图片内容无效或已损坏: {resolved}")

        normalized_tags = normalize_tags(tags)
        normalized_description = str(description or "").strip()
        if not normalized_description:
            return IngestResult(ok=False, message="description 不能为空")
        if not normalized_tags:
            return IngestResult(ok=False, message="至少需要一个 tag")
        allocated_def = await self._allocate_meme_def(meme_def)
        if not allocated_def:
            return IngestResult(ok=False, message="filename/meme_def 无法规范化")

        async with self._ingest_lock:
            if not skip_duplicate_check:
                duplicate = await self.dedup.find_similar_duplicate(resolved)
                if duplicate is not None:
                    return IngestResult(
                        ok=False,
                        message=f"检测到重复资产: {duplicate.meme_def}",
                        duplicate_of=duplicate.asset_id,
                    )
            storage_key, _ = await self.storage.import_file(resolved, allocated_def)
            asset = await self.storage.add_asset(
                StickerAssetDraft(
                    meme_def=allocated_def,
                    storage_key=storage_key,
                    mime_hint=resolved.suffix.lower(),
                    description=normalized_description,
                    source=source,
                    tags=normalized_tags,
                )
            )
            await self.dedup.register_file(
                await self.storage.resolve_path(asset.storage_key), asset
            )
            return IngestResult(ok=True, message="导入成功", asset=asset)

    async def review_remote_image(self, image_url: str) -> dict:
        return await self.review.review_image(image_url)

    async def save_remote_image(
        self,
        image_url: str,
        meme_def: str,
        tags: tuple[str, ...] | list[str],
        description: str,
        source: str = "remote",
    ) -> IngestResult:
        if self.format_busy:
            return IngestResult(ok=False, message="旧库格式化进行中，暂时不能导入图片")
        temp_file = await self.downloader.download(image_url)
        if temp_file is None:
            return IngestResult(ok=False, message="下载图片失败")
        try:
            return await self.ingest_local_file(
                str(temp_file),
                meme_def=meme_def,
                tags=tags,
                description=description,
                source=source,
            )
        finally:
            self.downloader.cleanup(temp_file)

    async def can_accept_more_assets(self, max_stickers: int | None = None) -> bool:
        limit = max_stickers if max_stickers is not None else self.plugin_config.get("max_stickers")
        if limit in (None, ""):
            return True
        try:
            limit_value = int(limit)
        except (TypeError, ValueError):
            return True
        return limit_value <= 0 or await self.storage.count_assets() < limit_value

    async def maybe_auto_collect_image(
        self,
        image_url: str,
        source_origin: str,
        source_user: str,
    ) -> dict:
        if self.format_busy:
            return {"success": False, "message": "旧库格式化进行中，跳过自动采集"}
        sanitized_source = self.summarize_image_source(image_url)
        async with self._inflight_lock:
            if image_url in self.inflight_sources:
                return {"success": False, "message": "图片正在处理，已跳过重复任务"}
            self.inflight_sources.add(image_url)
        if not self._is_remote_image_source(image_url):
            async with self._inflight_lock:
                self.inflight_sources.discard(image_url)
            return {"success": False, "message": "自动采集仅支持远程图片 URL"}
        if not await self.can_accept_more_assets(self.plugin_config.get("max_stickers")):
            async with self._inflight_lock:
                self.inflight_sources.discard(image_url)
            return {"success": False, "message": "当前图片资产数量已达到上限"}

        temp_file: Path | None = None
        try:
            temp_file = await self.downloader.download(image_url)
            if temp_file is None or not await self._validate_image_file(temp_file):
                return {"success": False, "message": "下载失败或图片内容无效"}
            duplicate = await self.dedup.find_similar_duplicate(temp_file)
            if duplicate is not None:
                return {
                    "success": False,
                    "message": f"检测到重复资产: {duplicate.meme_def}",
                    "duplicate_of": duplicate.asset_id,
                }
            review_result = await self.review_remote_image(image_url)
            logger.info(
                "此刻的心情: 图片审查完成 should_steal=%s filename=%s tags=%s",
                review_result.get("should_steal"),
                review_result.get("filename"),
                review_result.get("tags"),
            )
            if not review_result.get("should_steal"):
                return {
                    "success": False,
                    "message": str(review_result.get("reason") or "LLM 审查未通过"),
                    "review": review_result,
                }
            result = await self._ingest_resolved_file(
                resolved=temp_file,
                meme_def=str(review_result.get("filename") or ""),
                tags=tuple(str(tag) for tag in review_result.get("tags", [])),
                description=str(review_result.get("description") or ""),
                source=f"auto_steal_origin:{source_origin}_user:{source_user}",
                skip_validation=True,
                skip_duplicate_check=True,
            )
            return {
                "success": result.ok,
                "message": result.message,
                "meme_def": result.asset.meme_def if result.asset else "",
                "review": review_result,
            }
        except Exception as exc:
            logger.error(f"此刻的心情: 自动采集失败: {exc}", exc_info=True)
            return {"success": False, "message": f"自动采集失败: {exc}"}
        finally:
            self.downloader.cleanup(temp_file)
            async with self._inflight_lock:
                self.inflight_sources.discard(image_url)

    async def inspect_recent(self, scope_key: str, limit: int = 5) -> list[dict]:
        usage_events = await self.storage.list_recent_usage(scope_key, limit)
        results: list[dict] = []
        seen: set[str] = set()
        for event in usage_events:
            if event.asset_id in seen:
                continue
            asset = await self.storage.get_asset(event.asset_id)
            if asset is None:
                continue
            results.append(
                {
                    "asset_id": asset.asset_id,
                    "meme_def": asset.meme_def,
                    "description": asset.description,
                    "tags": list(asset.tags),
                    "usage_count": asset.usage_count,
                }
            )
            seen.add(event.asset_id)
        return results

    async def delete_asset(self, asset_id: str) -> DeleteResult:
        asset = await self.storage.delete_asset(asset_id)
        if asset is None:
            return DeleteResult(ok=False, message=f"未找到 asset_id={asset_id} 的图片资产。")
        await self.storage.delete_file(asset.storage_key)
        await self.dedup.unregister_asset(asset)
        # 同步清理缩略图缓存。
        try:
            (self.paths.data_dir / ".thumbnails" / f"{asset_id}.webp").unlink(missing_ok=True)
        except OSError:
            pass
        return DeleteResult(ok=True, message=f"已删除图片资产: {asset.meme_def}", asset=asset)

    async def check_meme_def(self, meme_def: str) -> dict | None:
        return await self.storage.get_meme_by_def(meme_def.strip())

    async def rough_search_memes(self, query: str, limit: int = 8) -> list[dict]:
        query_text = query.strip().casefold()
        if not query_text:
            return []
        scored: list[tuple[int, dict]] = []
        for asset in await self.storage.query_assets():
            key = asset.meme_def.casefold()
            description = asset.description.casefold()
            tags = " ".join(asset.tags).casefold()
            source = asset.source.casefold()
            score = 0
            if query_text == key:
                score += 1000
            if query_text in key:
                score += 300
            if query_text in description:
                score += 100
            if query_text in tags:
                score += 80
            if query_text in source:
                score += 20
            for term in re.findall(r"[\w\u4e00-\u9fff]+", query_text):
                if term in description:
                    score += 10
                if term in tags:
                    score += 8
            if score:
                scored.append(
                    (
                        score,
                        {
                            "meme_def": asset.meme_def,
                            "description": asset.description,
                            "tags": list(asset.tags),
                            "usage_count": asset.usage_count,
                        },
                    )
                )
        scored.sort(
            key=lambda item: (
                -item[0],
                item[1]["usage_count"],
                item[1]["meme_def"].casefold(),
            )
        )
        return [item[1] for item in scored[: max(1, min(int(limit), 20))]]

    async def maybe_run_cleanup(self) -> None:
        if self.format_busy or not self.plugin_config.get("enable_auto_cleanup", True):
            return
        interval_hours = int(self.plugin_config.get("cleanup_interval_hours", 1) or 1)
        cleanup_count = int(self.plugin_config.get("cleanup_count", 5) or 5)
        min_keep = int(self.plugin_config.get("min_stickers_to_keep", 0) or 0)
        now = time.time()
        if self._cleanup_last_run and now - self._cleanup_last_run < interval_hours * 3600:
            return
        self._cleanup_last_run = now
        removable = max(0, await self.storage.count_assets() - max(min_keep, 0))
        for item in await self.storage.get_least_used_memes(min(cleanup_count, removable)):
            asset_id = str(item.get("asset_id") or "")
            if asset_id:
                await self.delete_asset(asset_id)

    def explain_auto_collect_item(
        self, item, raw_image_data: dict | None = None
    ) -> tuple[bool, str]:
        if not self.plugin_config.get("enable_auto_steal", True):
            return False, "enable_auto_steal=false"
        only_store_emojis = self.plugin_config.get("only_store_emojis", False)
        has_markers, matched_attrs = self._has_store_emoji_markers(item, raw_image_data)
        if only_store_emojis:
            if has_markers:
                return True, f"only_store_emojis=true，命中特征字段: {', '.join(matched_attrs)}"
            return False, "only_store_emojis=true 且图片不含商城表情特征"
        if self.plugin_config.get("steal_all_images", False):
            return True, "steal_all_images=true"
        if self._is_emoji_type_image(item, raw_image_data):
            return True, "命中表情类型图片特征"
        return False, "steal_all_images=false 且图片未命中表情类型特征"
