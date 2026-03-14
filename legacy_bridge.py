from __future__ import annotations

import json
from pathlib import Path

from .constants import SUPPORTED_IMAGE_SUFFIXES
from .utils import normalize_category_name, resolve_user_path


class LegacyManagerBridge:
    def __init__(self, facade, context=None, plugin_config=None):
        self.facade = facade
        self.context = context
        self.plugin_config = plugin_config or {}

    def initialize(self) -> None:
        self.facade.set_context(self.context)
        self.facade.set_plugin_config(self.plugin_config)

    def set_context(self, context) -> None:
        self.context = context
        self.facade.set_context(context)

    def set_plugin_config(self, plugin_config) -> None:
        self.plugin_config = plugin_config or {}
        self.facade.set_plugin_config(self.plugin_config)

    async def steal_meme(
        self,
        image_path: str,
        category: str,
        description: str | None = None,
        save_name: str | None = None,
    ) -> str:
        raw_path = resolve_user_path(image_path)
        if not raw_path.exists() or not raw_path.is_file():
            return f"图片不存在或不是文件: {raw_path}"
        if raw_path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
            return f"暂不支持的图片格式: {raw_path.suffix or '无扩展名'}"
        normalized_category = normalize_category_name(category)
        if not normalized_category.strip():
            return (
                "缺少 category。请先根据分类目录选择一个分类，再调用图片入库工具保存。"
            )
        result = await self.facade.ingest_local_file(
            source_path=str(raw_path),
            group_name=normalized_category,
            description=(description or "").strip(),
            preferred_name=save_name,
        )
        payload = {
            "ok": result.ok,
            "saved": result.ok,
            "category": normalized_category,
            "description": (description or "").strip(),
            "message": result.message,
            "reason": result.message,
            "duplicate": bool(result.duplicate_of),
            "duplicate_type": "similar" if result.duplicate_of else "",
            "matched_file": result.duplicate_of or "",
            "distance": None,
        }
        if result.asset is not None:
            payload["saved_file"] = result.asset.original_name
        return json.dumps(payload, ensure_ascii=False)

    async def review_image(self, image_url: str) -> dict:
        return await self.facade.review_remote_image(image_url)

    async def save_with_tags(
        self,
        image_url: str,
        tags: list,
        source_group: str,
        source_user: str,
        max_stickers: int | None = None,
    ) -> dict:
        if not await self.facade.can_accept_more_assets(max_stickers):
            return {
                "success": False,
                "meme_id": "",
                "message": f"当前表情包数量已达到上限 ({max_stickers})",
            }
        normalized_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
        normalized_tags = normalized_tags[:4]
        normalized_tags = [tag for tag in normalized_tags if len(tag) <= 10]
        if not normalized_tags:
            normalized_tags = ["未分类"]
        normalized_category = normalize_category_name(normalized_tags[0])
        result = await self.facade.save_remote_image(
            image_url=image_url,
            group_name=normalized_category,
            description=", ".join(normalized_tags),
            source=f"auto_steal_group:{source_group}_user:{source_user}",
        )
        return {
            "success": result.ok,
            "meme_id": result.asset.asset_id if result.asset else "",
            "message": result.message,
        }


class LegacyStorageFacade:
    def __init__(self, facade):
        self.facade = facade

    async def get_catalog_stickers_data(self) -> dict[str, str]:
        return await self.facade.storage.get_catalog_stickers_data()

    async def get_available_stickers_data(self) -> dict[str, str]:
        return await self.facade.storage.get_available_stickers_data()

    async def get_catalog_description(self, category: str) -> str | None:
        return await self.facade.storage.get_catalog_description(category)

    async def has_sticker_assets(self, category: str) -> bool:
        return await self.facade.storage.has_sticker_assets(category)

    async def get_sticker_count(self) -> int:
        return await self.facade.storage.get_sticker_count()

    async def get_all_memes(self) -> list[dict]:
        return await self.facade.storage.get_all_memes()

    async def get_meme_by_id(self, asset_id: str) -> dict | None:
        return await self.facade.storage.get_meme_by_id(asset_id)

    async def get_meme_by_file_path(self, file_path: str | Path) -> dict | None:
        return await self.facade.storage.get_meme_by_file_path(file_path)

    async def get_random_sticker_path(self, category: str) -> str | None:
        return await self.facade.storage.get_random_sticker_path(category)

    async def delete_meme(self, asset_id: str) -> bool:
        return await self.facade.storage.delete_meme(asset_id)

    async def iter_all_sticker_files(self) -> list[Path]:
        return await self.facade.storage.iter_all_sticker_files()

    async def get_least_used_memes(self, count: int) -> list[dict]:
        return await self.facade.storage.get_least_used_memes(count)

    async def get_usage_stats(self) -> dict:
        return await self.facade.storage.get_usage_stats()
