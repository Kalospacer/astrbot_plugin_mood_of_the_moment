from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent

from .constants import STEAL_TOOL_NAME


class IngestFacadeProtocol(Protocol):
    async def ingest_local_file(
        self,
        source_path: str,
        group_name: str,
        description: str = "",
        preferred_name: str | None = None,
    ): ...
    async def save_remote_image(
        self,
        image_url: str,
        group_name: str,
        description: str = "",
        preferred_name: str | None = None,
        source: str = "remote",
    ): ...


@dataclass
class StealMemesTool(FunctionTool):
    facade: IngestFacadeProtocol | None = field(repr=False, default=None)
    name: str = STEAL_TOOL_NAME
    description: str = "接收本地图片路径或图片 URL，并按给定 category 保存到此刻的心情插件数据目录。调用前应先给出明确分类。"
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "本地图片绝对路径、相对路径或 http/https 图片地址。",
                },
                "category": {
                    "type": "string",
                    "description": "必填，要保存到的图片分组名。",
                },
                "description": {
                    "type": "string",
                    "description": "可选，分组的中文用途描述。",
                },
                "save_name": {
                    "type": "string",
                    "description": "可选，保存后的文件名，不含路径。建议按「角色-动作」格式，如：金色猫娘-困倦。",
                },
            },
            "required": ["image_path", "category"],
        }
    )

    async def call(
        self,
        context: Any,
        image_path: str,
        category: str,
        description: str | None = None,
        save_name: str | None = None,
    ) -> str:
        _ = context
        if self.facade is None:
            raise RuntimeError("StealMemesTool facade is not initialized")
        normalized_description = description or ""
        if image_path.startswith("http://") or image_path.startswith("https://"):
            result = await self.facade.save_remote_image(
                image_url=image_path,
                group_name=category,
                description=normalized_description,
                preferred_name=save_name,
                source="tool_remote",
            )
        else:
            result = await self.facade.ingest_local_file(
                source_path=image_path,
                group_name=category,
                description=normalized_description,
                preferred_name=save_name,
            )
        if result.ok and result.asset is not None:
            return f"导入成功: asset_id={result.asset.asset_id}, group={result.asset.group_name}, file={result.asset.original_name}"
        if result.duplicate_of:
            return f"检测到重复资产，已跳过: {result.duplicate_of}"
        return f"导入失败: {result.message}"

    async def run(
        self,
        event: AstrMessageEvent,
        image_path: str,
        category: str,
        description: str | None = None,
        save_name: str | None = None,
    ) -> str:
        _ = event
        return await self.call(
            context=None,
            image_path=image_path,
            category=category,
            description=description,
            save_name=save_name,
        )
