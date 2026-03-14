from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from astrbot.api import logger

from .constants import DEFAULT_CATEGORY, DEFAULT_CATEGORY_DESCRIPTION, SUPPORTED_IMAGE_SUFFIXES
from .dedup import DHashDedupService
from .downloader import RemoteImageDownloader
from .models import DeleteResult, DecoratedContent, DecoratedSegment, IngestResult, PluginPaths, StickerAssetDraft, StickerGroup, StickerUsageEvent
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
            max_stickers_per_message=int(self.plugin_config.get("max_stickers_per_message", 1) or 1),
        )
        self.allowed_image_roots = get_allowed_image_roots(extra_roots=(self.paths.plugin_dir, self.paths.data_dir))
        self.inflight_sources: set[str] = set()
        self._cleanup_last_run = 0.0

    def set_context(self, context) -> None:
        self.context = context
        self.review.set_context(context)

    def set_plugin_config(self, plugin_config: dict | None) -> None:
        self.plugin_config = plugin_config or {}
        self.review.set_plugin_config(self.plugin_config)
        self.renderer.max_stickers_per_message = max(0, int(self.plugin_config.get("max_stickers_per_message", 1) or 1))

    async def startup(self) -> None:
        self.storage.initialize()
        self.dedup.initialize()
        self._import_default_catalog()
        self._import_legacy_assets_if_needed()

    async def shutdown(self) -> None:
        self.storage.close()

    def build_llm_summary(self) -> str:
        return self.renderer.build_prompt_catalog()

    async def decorate_text(self, text: str, scope_key: str) -> DecoratedContent:
        return await self.renderer.decorate_text(text=text, scope_key=scope_key)

    async def ingest_local_file(self, source_path: str, group_name: str, description: str = "", preferred_name: str | None = None) -> IngestResult:
        resolved = resolve_user_path(source_path)
        if not resolved.exists() or not resolved.is_file():
            return IngestResult(ok=False, message=f"图片不存在或不是文件: {resolved}")
        if resolved.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
            return IngestResult(ok=False, message=f"不支持的图片格式: {resolved.suffix or '无扩展名'}")
        if not is_path_within_roots(resolved, self.allowed_image_roots):
            return IngestResult(ok=False, message=f"图片路径超出允许范围: {resolved}")
        normalized_group = normalize_category_name(group_name)
        duplicate = self.dedup.find_similar_duplicate(resolved)
        if duplicate is not None:
            return IngestResult(ok=False, message=f"检测到重复资产: {duplicate.asset_id}", duplicate_of=duplicate.asset_id)
        storage_key, original_name = self.storage.import_file(resolved, normalized_group, preferred_name)
        self.storage.upsert_group(StickerGroup(name=normalized_group, description=(description or "").strip()))
        asset = self.storage.add_asset(
            StickerAssetDraft(
                group_name=normalized_group,
                storage_key=storage_key,
                original_name=original_name,
                mime_hint=resolved.suffix.lower(),
                description=(description or "").strip(),
                source="manual",
                labels=(normalize_tag_display_name(group_name),),
            )
        )
        self.dedup.register_file(self.storage.resolve_path(asset.storage_key), asset)
        return IngestResult(ok=True, message="导入成功", asset=asset)

    async def review_remote_image(self, image_url: str) -> dict:
        return await self.review.review_image(image_url)

    async def save_remote_image(self, image_url: str, group_name: str, description: str = "", preferred_name: str | None = None, source: str = "remote") -> IngestResult:
        temp_file = await self.downloader.download(image_url)
        if temp_file is None:
            return IngestResult(ok=False, message="下载图片失败")
        try:
            result = await self.ingest_local_file(str(temp_file), group_name, description, preferred_name)
            if result.ok and result.asset is not None and source != "manual":
                self.storage._get_connection().execute(
                    "UPDATE sticker_assets SET source = ? WHERE asset_id = ?",
                    (source, result.asset.asset_id),
                )
                self.storage._get_connection().commit()
                result.asset.source = source
            return result
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
        if limit_value <= 0:
            return True
        return self.storage.count_assets() < limit_value

    async def maybe_auto_collect_image(self, image_url: str, source_group: str, source_user: str) -> dict:
        if image_url in self.inflight_sources:
            return {"success": False, "message": "图片正在处理，已跳过重复任务"}
        if not await self.can_accept_more_assets(self.plugin_config.get("max_stickers")):
            return {"success": False, "message": "当前图片资产数量已达到上限"}
        self.inflight_sources.add(image_url)
        try:
            review_result = await self.review_remote_image(image_url)
            if not review_result.get("should_steal"):
                return {
                    "success": False,
                    "message": str(review_result.get("reason") or "LLM 审查未通过"),
                    "review": review_result,
                }
            tags = [str(tag).strip() for tag in review_result.get("tags", []) if str(tag).strip()]
            normalized_group = normalize_category_name(tags[0] if tags else DEFAULT_CATEGORY)
            result = await self.save_remote_image(
                image_url=image_url,
                group_name=normalized_group,
                description=str(review_result.get("reason") or "").strip(),
                source=f"auto_steal_group:{source_group}_user:{source_user}",
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
            self.inflight_sources.discard(image_url)

    async def inspect_recent(self, scope_key: str, limit: int = 5):
        usage_events = self.storage.list_recent_usage(scope_key, limit)
        results = []
        seen_asset_ids: set[str] = set()
        for event in usage_events:
            if event.asset_id in seen_asset_ids:
                continue
            asset = self.storage.get_asset(event.asset_id)
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
        asset = self.storage.delete_asset(asset_id)
        if asset is None:
            return DeleteResult(ok=False, message=f"未找到 asset_id={asset_id} 的图片资产。")
        self.storage.delete_file(asset.storage_key)
        self.dedup.unregister_asset(asset)
        return DeleteResult(ok=True, message=f"已删除图片资产: {asset.asset_id}", asset=asset)

    def should_auto_collect_item(self, item) -> bool:
        if self.plugin_config.get("steal_all_images", False):
            return True
        return any(getattr(item, attr, None) for attr in ("emoji_id", "emoji_package_id", "key"))

    async def maybe_run_cleanup(self) -> None:
        if not self.plugin_config.get("enable_auto_cleanup", True):
            return
        interval_hours = int(self.plugin_config.get("cleanup_interval_hours", 1) or 1)
        cleanup_count = int(self.plugin_config.get("cleanup_count", 5) or 5)
        min_keep = int(self.plugin_config.get("min_stickers_to_keep", 0) or 0)
        now = time.time()
        if self._cleanup_last_run and now - self._cleanup_last_run < interval_hours * 3600:
            return
        self._cleanup_last_run = now
        total_count = self.storage.count_assets()
        removable = max(0, total_count - max(min_keep, 0))
        if removable <= 0:
            return
        to_delete = self.storage.get_least_used_memes(min(cleanup_count, removable))
        for item in to_delete:
            asset_id = str(item.get("meme_id") or "")
            if asset_id:
                await self.delete_asset(asset_id)

    def _import_default_catalog(self) -> None:
        default_json = self.paths.default_dir / "memes_data.json"
        if default_json.exists():
            try:
                raw_data = json.loads(default_json.read_text(encoding="utf-8"))
                if isinstance(raw_data, dict):
                    for category, description in raw_data.items():
                        normalized = normalize_category_name(category)
                        self.storage.upsert_group(StickerGroup(name=normalized, description=str(description or "").strip()))
            except Exception as exc:
                logger.warning(f"此刻的心情: 读取默认分类描述失败: {exc}")

    def _import_legacy_assets_if_needed(self) -> None:
        if self.storage.count_assets() > 0:
            return
        imported = 0
        legacy_plugin_dirs = self.plugin_config.get("legacy_plugin_dirs") or []
        if isinstance(legacy_plugin_dirs, str):
            legacy_plugin_dirs = [legacy_plugin_dirs]
        for raw_dir in legacy_plugin_dirs:
            legacy_dir = Path(str(raw_dir)).expanduser().resolve()
            default_json = legacy_dir / "default" / "memes_data.json"
            if default_json.exists():
                try:
                    raw_data = json.loads(default_json.read_text(encoding="utf-8"))
                    if isinstance(raw_data, dict):
                        for category, description in raw_data.items():
                            normalized = normalize_category_name(category)
                            self.storage.upsert_group(StickerGroup(name=normalized, description=str(description or "").strip()))
                except Exception as exc:
                    logger.warning(f"此刻的心情: 读取旧分类描述失败: {exc}")
            memes_dir = legacy_dir / "default" / "memes"
            if not memes_dir.exists():
                continue
            for file_path in memes_dir.rglob("*"):
                if not file_path.is_file() or file_path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
                    continue
                try:
                    category_name = file_path.relative_to(memes_dir).parts[0]
                except Exception:
                    category_name = DEFAULT_CATEGORY
                normalized = normalize_category_name(category_name)
                duplicate = self.dedup.find_similar_duplicate(file_path)
                if duplicate is not None:
                    continue
                storage_key, original_name = self.storage.import_file(file_path, normalized)
                asset = self.storage.add_asset(
                    StickerAssetDraft(
                        group_name=normalized,
                        storage_key=storage_key,
                        original_name=original_name,
                        mime_hint=file_path.suffix.lower(),
                        description=self.storage.get_catalog_description(normalized) or "",
                        source="legacy_default_import",
                        labels=(normalize_tag_display_name(category_name),),
                    )
                )
                self.dedup.register_file(self.storage.resolve_path(asset.storage_key), asset)
                imported += 1
        legacy_data_dirs = self.plugin_config.get("legacy_data_dirs") or []
        if isinstance(legacy_data_dirs, str):
            legacy_data_dirs = [legacy_data_dirs]
        for raw_dir in legacy_data_dirs:
            candidate = Path(str(raw_dir)).expanduser().resolve()
            if not candidate.exists() or not candidate.is_dir():
                continue
            for file_path in candidate.rglob("*"):
                if not file_path.is_file() or file_path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
                    continue
                try:
                    category_name = file_path.parent.name or DEFAULT_CATEGORY
                except Exception:
                    category_name = DEFAULT_CATEGORY
                normalized = normalize_category_name(category_name)
                duplicate = self.dedup.find_similar_duplicate(file_path)
                if duplicate is not None:
                    continue
                storage_key, original_name = self.storage.import_file(file_path, normalized)
                asset = self.storage.add_asset(
                    StickerAssetDraft(
                        group_name=normalized,
                        storage_key=storage_key,
                        original_name=original_name,
                        mime_hint=file_path.suffix.lower(),
                        description=self.storage.get_catalog_description(normalized) or "",
                        source="legacy_dir_import",
                        labels=(normalize_tag_display_name(category_name),),
                    )
                )
                self.dedup.register_file(self.storage.resolve_path(asset.storage_key), asset)
                imported += 1
        if imported:
            logger.info(f"此刻的心情: 已导入旧资产 {imported} 个")
