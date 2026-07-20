from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent

from .constants import (
    CHECK_MEMES_DEF_TOOL_NAME,
    ROUGH_SEARCH_MEMES_TOOL_NAME,
    STEAL_TOOL_NAME,
)


class MemesFacadeProtocol(Protocol):
    async def ingest_local_file(
        self,
        source_path: str,
        meme_def: str,
        tags: tuple[str, ...] | list[str],
        description: str,
        source: str = "manual",
    ): ...

    async def save_remote_image(
        self,
        image_url: str,
        meme_def: str,
        tags: tuple[str, ...] | list[str],
        description: str,
        source: str = "remote",
    ): ...

    async def check_meme_def(self, meme_def: str): ...

    async def rough_search_memes(self, query: str, limit: int = 8): ...


def _format_meme_definition(item: dict) -> str:
    return (
        f"# 此刻的心情 / {item.get('meme_def', '')}\n\n"
        f"描述：{item.get('description', '')}\n"
        f"tags：{', '.join(item.get('tags') or [])}\n"
        f"发送 marker：:{item.get('meme_def', '')}:"
    )


@dataclass
class CheckMemesDefTool(FunctionTool):
    facade: MemesFacadeProtocol | None = field(repr=False, default=None)
    name: str = CHECK_MEMES_DEF_TOOL_NAME
    description: str = (
        "此刻的心情插件：按已知 meme_def 精确查询一张表情包的视觉描述、tags 和发送 marker。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "meme_def": {
                    "type": "string",
                    "description": "此刻的心情插件注入的唯一表情定义，例如 真冬_低头。",
                }
            },
            "required": ["meme_def"],
        }
    )

    async def call(self, context: Any, meme_def: str) -> str:
        _ = context
        if self.facade is None:
            raise RuntimeError("CheckMemesDefTool facade is not initialized")
        item = await self.facade.check_meme_def(meme_def)
        if item is None:
            return f"未找到 meme_def={meme_def}。请调用 mood_rough_search_memes 搜索候选。"
        return _format_meme_definition(item)

    async def run(self, event: AstrMessageEvent, meme_def: str) -> str:
        return await self.call(None, meme_def)


@dataclass
class RoughSearchMemesTool(FunctionTool):
    facade: MemesFacadeProtocol | None = field(repr=False, default=None)
    name: str = ROUGH_SEARCH_MEMES_TOOL_NAME
    description: str = (
        "此刻的心情插件：根据自然语言在 meme_def、表情描述和分组 tags 中搜索候选表情包。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "想表达的动作、情绪、角色或使用场景。",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回候选数量，范围 1-20。",
                    "default": 8,
                },
            },
            "required": ["query"],
        }
    )

    async def call(self, context: Any, query: str, limit: int = 8) -> str:
        _ = context
        if self.facade is None:
            raise RuntimeError("RoughSearchMemesTool facade is not initialized")
        items = await self.facade.rough_search_memes(query, limit)
        if not items:
            return "没有找到匹配的表情包候选。"
        lines = ["此刻的心情插件候选表情包："]
        for index, item in enumerate(items, start=1):
            lines.append(
                f"{index}. {item.get('meme_def', '')} | "
                f"tags={', '.join(item.get('tags') or [])} | "
                f"描述={item.get('description', '')}"
            )
        return "\n".join(lines)

    async def run(
        self, event: AstrMessageEvent, query: str, limit: int = 8
    ) -> str:
        return await self.call(None, query, limit)


@dataclass
class StealMemesTool(FunctionTool):
    facade: MemesFacadeProtocol | None = field(repr=False, default=None)
    name: str = STEAL_TOOL_NAME
    description: str = (
        "此刻的心情插件：导入一张图片，必须提供唯一 meme_def、至少一个分组 tag 和视觉描述。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "本地图片路径或 http/https 图片地址。",
                },
                "meme_def": {
                    "type": "string",
                    "description": "这张图片的唯一定义，格式建议为 角色_动作。",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "至少一个可被多张图片共享的分组 tag。",
                },
                "description": {
                    "type": "string",
                    "description": "描述画面、情绪和适用场景。",
                },
            },
            "required": ["image_path", "meme_def", "tags", "description"],
        }
    )

    async def call(
        self,
        context: Any,
        image_path: str,
        meme_def: str,
        tags: list[str],
        description: str,
    ) -> str:
        _ = context
        if self.facade is None:
            raise RuntimeError("StealMemesTool facade is not initialized")
        if image_path.startswith(("http://", "https://")):
            result = await self.facade.save_remote_image(
                image_url=image_path,
                meme_def=meme_def,
                tags=tags,
                description=description,
                source="tool_remote",
            )
        else:
            result = await self.facade.ingest_local_file(
                source_path=image_path,
                meme_def=meme_def,
                tags=tags,
                description=description,
                source="tool_local",
            )
        if result.ok and result.asset is not None:
            return (
                f"导入成功: asset_id={result.asset.asset_id}, "
                f"meme_def={result.asset.meme_def}, tags={list(result.asset.tags)}"
            )
        if result.duplicate_of:
            return f"检测到重复资产，已跳过: {result.duplicate_of}"
        return f"导入失败: {result.message}"

    async def run(
        self,
        event: AstrMessageEvent,
        image_path: str,
        meme_def: str,
        tags: list[str],
        description: str,
    ) -> str:
        return await self.call(None, image_path, meme_def, tags, description)
