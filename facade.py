from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from astrbot.api import logger
from PIL import Image, UnidentifiedImageError

from .constants import DEFAULT_CATEGORY, SUPPORTED_IMAGE_SUFFIXES
from .dedup import DHashDedupService
from .downloader import RemoteImageDownloader
from .models import (
    DeleteResult,
    DecoratedContent,
    IngestResult,
    PluginPaths,
    StickerAssetDraft,
    StickerGroup,
)

from .render import StickerRenderer
from .review import ReviewService
from .storage import StickerStorage
from .utils import (
    get_allowed_image_roots,
    is_path_within_roots,
    normalize_category_name,
    normalize_tag_display_name,
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
                f"此刻的心情: 启动时清理失效资产 {len(stale_asset_ids)} 个: "
                + ", ".join(stale_asset_ids[:10])
            )
        else:
            logger.info("此刻的心情: 启动时未发现失效资产")
        await self.dedup.initialize()
        await self._import_default_catalog()

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
            path = parsed.path or "/"
            return f"{parsed.scheme}://{parsed.netloc}{path}"
        path = Path(image_source)
        return f"local:{path.name or '[unknown]'}"

    @staticmethod
    def _normalize_text(value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()

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
        def _is_emoji_summary(summary: object) -> bool:
            text = cls._normalize_text(summary).lower()
            return bool(text) and (
                "表情" in text or "emoji" in text or "sticker" in text
            )

        def _is_sub_type_emoji(sub_type: object) -> bool:
            if sub_type in (1, "1"):
                return True
            try:
                return int(sub_type) == 1
            except Exception:
                return False

        has_store_markers, _ = cls._has_store_emoji_markers(item, raw_image_data)
        if has_store_markers:
            return True

        candidate_payloads: list[dict] = []
        if isinstance(raw_image_data, dict):
            candidate_payloads.append(raw_image_data)

        item_dict = getattr(item, "__dict__", None)
        if isinstance(item_dict, dict):
            candidate_payloads.append(item_dict)

        try:
            raw_dict = item.toDict()
            if isinstance(raw_dict, dict):
                data = raw_dict.get("data")
                if isinstance(data, dict):
                    candidate_payloads.append(data)
                else:
                    candidate_payloads.append(raw_dict)
        except Exception:
            pass

        for payload in candidate_payloads:
            sub_type = payload.get("sub_type")
            if _is_sub_type_emoji(sub_type):
                return True
            sub_type = payload.get("subType")
            if _is_sub_type_emoji(sub_type):
                return True

            summary = payload.get("summary")
            if _is_emoji_summary(summary):
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

        sub_type = getattr(item, "subType", None)
        if _is_sub_type_emoji(sub_type):
            return True

        return False

    @staticmethod
    def extract_image_segment_payloads(raw_message) -> list[dict]:
        segments = getattr(raw_message, "message", None)
        if segments is None and isinstance(raw_message, dict):
            segments = raw_message.get("message")
        if not isinstance(segments, list):
            return []
        payloads: list[dict] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if segment.get("type") != "image":
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

    @staticmethod
    def _derive_preferred_name(image_url: str) -> str | None:
        parsed = urlparse(image_url)
        candidate = Path(parsed.path).name.strip()
        return candidate or None

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

    async def ingest_local_file(
        self,
        source_path: str,
        group_name: str,
        description: str = "",
        preferred_name: str | None = None,
        labels: tuple[str, ...] | None = None,
        source: str = "manual",
    ) -> IngestResult:
        resolved = resolve_user_path(source_path)
        return await self._ingest_resolved_file(
            resolved=resolved,
            group_name=group_name,
            description=description,
            preferred_name=preferred_name,
            labels=labels,
            source=source,
            skip_validation=False,
            skip_duplicate_check=False,
        )

    async def _ingest_resolved_file(
        self,
        resolved: Path,
        group_name: str,
        description: str = "",
        preferred_name: str | None = None,
        labels: tuple[str, ...] | None = None,
        source: str = "manual",
        *,
        skip_validation: bool,
        skip_duplicate_check: bool,
    ) -> IngestResult:
        if not resolved.exists() or not resolved.is_file():
            return IngestResult(ok=False, message=f"图片不存在或不是文件: {resolved}")
        if resolved.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
            return IngestResult(
                ok=False,
                message=f"不支持的图片格式: {resolved.suffix or '无扩展名'}",
            )
        if not is_path_within_roots(resolved, self.allowed_image_roots):
            return IngestResult(ok=False, message=f"图片路径超出允许范围: {resolved}")
        if not skip_validation and not await self._validate_image_file(resolved):
            return IngestResult(ok=False, message=f"图片内容无效或已损坏: {resolved}")
        normalized_group = normalize_category_name(group_name)
        async with self._ingest_lock:
            if not skip_duplicate_check:
                duplicate = await self.dedup.find_similar_duplicate(resolved)
                if duplicate is not None:
                    return IngestResult(
                        ok=False,
                        message=f"检测到重复资产: {duplicate.asset_id}",
                        duplicate_of=duplicate.asset_id,
                    )
            storage_key, original_name = await self.storage.import_file(
                resolved, normalized_group, preferred_name
            )
            await self.storage.upsert_group(
                StickerGroup(
                    name=normalized_group, description=(description or "").strip()
                )
            )
            normalized_labels_list: list[str] = []
            for raw_label in labels or (group_name,):
                stripped_label = (raw_label or "").strip()
                if not stripped_label:
                    continue
                normalized_label = normalize_tag_display_name(stripped_label)
                if normalized_label and normalized_label not in normalized_labels_list:
                    normalized_labels_list.append(normalized_label)
            normalized_labels = tuple(normalized_labels_list) or (
                normalize_tag_display_name(group_name),
            )
            asset = await self.storage.add_asset(
                StickerAssetDraft(
                    group_name=normalized_group,
                    storage_key=storage_key,
                    original_name=original_name,
                    mime_hint=resolved.suffix.lower(),
                    description=(description or "").strip(),
                    source=source,
                    labels=normalized_labels,
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
        group_name: str,
        description: str = "",
        preferred_name: str | None = None,
        source: str = "remote",
        labels: tuple[str, ...] | None = None,
    ) -> IngestResult:
        temp_file = await self.downloader.download(image_url)
        if temp_file is None:
            return IngestResult(ok=False, message="下载图片失败")
        try:
            result = await self.ingest_local_file(
                str(temp_file),
                group_name,
                description,
                preferred_name,
                labels=labels,
                source=source,
            )
            return result
        finally:
            self.downloader.cleanup(temp_file)

    async def can_accept_more_assets(self, max_stickers: int | None = None) -> bool:
        limit = (
            max_stickers
            if max_stickers is not None
            else self.plugin_config.get("max_stickers")
        )
        if limit in (None, ""):
            return True
        try:
            limit_value = int(limit)
        except (TypeError, ValueError):
            return True
        if limit_value <= 0:
            return True
        return await self.storage.count_assets() < limit_value

    async def maybe_auto_collect_image(
        self,
        image_url: str,
        source_group: str,
        source_user: str,
    ) -> dict:
        sanitized_source = self.summarize_image_source(image_url)
        logger.info(
            f"此刻的心情: 开始处理自动采集 source_group={source_group} "
            f"source_user={self._mask_identifier(source_user)} image_url={sanitized_source}"
        )
        async with self._inflight_lock:
            if image_url in self.inflight_sources:
                logger.info(
                    f"此刻的心情: 跳过重复自动采集任务 image_url={sanitized_source}"
                )
                return {"success": False, "message": "图片正在处理，已跳过重复任务"}
            self.inflight_sources.add(image_url)
        if not self._is_remote_image_source(image_url):
            logger.warning(
                f"此刻的心情: 自动采集仅支持远程图片源，已跳过 image_url={sanitized_source}"
            )
            return {"success": False, "message": "自动采集仅支持远程图片 URL"}
        if not await self.can_accept_more_assets(
            self.plugin_config.get("max_stickers")
        ):
            logger.info("此刻的心情: 自动采集跳过，图片资产数量已达到上限")
            return {"success": False, "message": "当前图片资产数量已达到上限"}
        temp_file: Path | None = None
        try:
            temp_file = await self.downloader.download(image_url)
            if temp_file is None:
                return {"success": False, "message": "下载图片失败"}
            if not await self._validate_image_file(temp_file):
                return {"success": False, "message": "图片内容无效或已损坏"}
            duplicate = await self.dedup.find_similar_duplicate(temp_file)
            if duplicate is not None:
                logger.info(
                    "此刻的心情: 自动采集跳过，检测到重复图片 "
                    f"asset_id={duplicate.asset_id} image_url={sanitized_source}"
                )
                return {
                    "success": False,
                    "message": f"检测到重复资产: {duplicate.asset_id}",
                    "duplicate_of": duplicate.asset_id,
                }
            review_result = await self.review_remote_image(image_url)
            logger.info(
                "此刻的心情: 图片审查完成 "
                f"should_steal={review_result.get('should_steal')} "
                f"reason={review_result.get('reason')} "
                f"tags={review_result.get('tags')} "
                f"filename={review_result.get('filename')}"
            )
            if not review_result.get("should_steal"):
                return {
                    "success": False,
                    "message": str(review_result.get("reason") or "LLM 审查未通过"),
                    "review": review_result,
                }
            tags = [
                str(tag).strip()
                for tag in review_result.get("tags", [])
                if str(tag).strip()
            ]
            preferred_name = (
                str(review_result.get("filename") or "").strip()
                or self._derive_preferred_name(image_url)
            )
            normalized_group = normalize_category_name(
                tags[0] if tags else DEFAULT_CATEGORY
            )
            result = await self._ingest_resolved_file(
                resolved=temp_file,
                group_name=normalized_group,
                description=str(review_result.get("reason") or "").strip(),
                preferred_name=preferred_name,
                source=f"auto_steal_group:{source_group}_user:{source_user}",
                labels=tuple(tags) if tags else None,
                skip_validation=True,
                skip_duplicate_check=True,
            )
            logger.info(
                f"此刻的心情: 自动采集保存完成 ok={result.ok} "
                f"message={result.message} asset_id={result.asset.asset_id if result.asset else ''}"
            )
            return {
                "success": result.ok,
                "message": result.message,
                "meme_id": result.asset.asset_id if result.asset else "",
                "review": review_result,
            }
        except Exception as exc:
            logger.error(f"此刻的心情: 自动采集失败: {exc}", exc_info=True)
            return {"success": False, "message": f"自动采集失败: {exc}"}
        finally:
            self.downloader.cleanup(temp_file)
            async with self._inflight_lock:
                self.inflight_sources.discard(image_url)

    async def inspect_recent(self, scope_key: str, limit: int = 5):
        usage_events = await self.storage.list_recent_usage(scope_key, limit)
        results = []
        seen_asset_ids: set[str] = set()
        for event in usage_events:
            if event.asset_id in seen_asset_ids:
                continue
            asset = await self.storage.get_asset(event.asset_id)
            if asset is None:
                continue
            results.append(
                {
                    "asset_id": asset.asset_id,
                    "group_name": asset.group_name,
                    "original_name": asset.original_name,
                    "description": asset.description,
                    "usage_count": asset.usage_count,
                }
            )
            seen_asset_ids.add(event.asset_id)
            if len(results) >= limit:
                break
        return results

    async def delete_asset(self, asset_id: str) -> DeleteResult:
        asset = await self.storage.delete_asset(asset_id)
        if asset is None:
            return DeleteResult(
                ok=False, message=f"未找到 asset_id={asset_id} 的图片资产。"
            )
        await self.storage.delete_file(asset.storage_key)
        await self.dedup.unregister_asset(asset)
        return DeleteResult(
            ok=True, message=f"已删除图片资产: {asset.asset_id}", asset=asset
        )

    def explain_auto_collect_item(
        self, item, raw_image_data: dict | None = None
    ) -> tuple[bool, str]:
        if not self.plugin_config.get("enable_auto_steal", True):
            return False, "enable_auto_steal=false"
        only_store_emojis = self.plugin_config.get("only_store_emojis", False)
        has_store_markers, matched_attrs = self._has_store_emoji_markers(
            item, raw_image_data
        )
        if only_store_emojis:
            if has_store_markers:
                return True, f"only_store_emojis=true，命中特征字段: {', '.join(matched_attrs)}"
            return False, "only_store_emojis=true 且图片不含 emoji_id/emoji_package_id/key"
        if self.plugin_config.get("steal_all_images", False):
            return True, "steal_all_images=true"
        if self._is_emoji_type_image(item, raw_image_data):
            if matched_attrs:
                return True, f"命中商城表情字段: {', '.join(matched_attrs)}"
            return True, "命中表情类型图片特征"
        return False, "steal_all_images=false 且图片未命中表情类型特征"

    def should_auto_collect_item(self, item) -> bool:
        should_collect, _ = self.explain_auto_collect_item(item)
        return should_collect

    async def maybe_run_cleanup(self) -> None:
        if not self.plugin_config.get("enable_auto_cleanup", True):
            return
        interval_hours = int(self.plugin_config.get("cleanup_interval_hours", 1) or 1)
        cleanup_count = int(self.plugin_config.get("cleanup_count", 5) or 5)
        min_keep = int(self.plugin_config.get("min_stickers_to_keep", 0) or 0)
        now = time.time()
        if (
            self._cleanup_last_run
            and now - self._cleanup_last_run < interval_hours * 3600
        ):
            return
        self._cleanup_last_run = now
        total_count = await self.storage.count_assets()
        removable = max(0, total_count - max(min_keep, 0))
        if removable <= 0:
            return
        to_delete = await self.storage.get_least_used_memes(
            min(cleanup_count, removable)
        )
        for item in to_delete:
            asset_id = str(item.get("meme_id") or "")
            if asset_id:
                await self.delete_asset(asset_id)

    async def _import_default_catalog(self) -> None:
        default_json = self.paths.default_dir / "memes_data.json"
        if default_json.exists():
            try:
                raw_data = json.loads(default_json.read_text(encoding="utf-8"))
                if isinstance(raw_data, dict):
                    for category, description in raw_data.items():
                        normalized = normalize_category_name(category)
                        await self.storage.upsert_group(
                            StickerGroup(
                                name=normalized,
                                description=str(description or "").strip(),
                            )
                        )
            except Exception as exc:
                logger.warning(f"此刻的心情: 读取默认分类描述失败: {exc}")
