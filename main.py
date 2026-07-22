from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.message import TextPart

from .constants import (
    CHECK_MEMES_DEF_TOOL_NAME,
    PLUGIN_NAME,
    PLUGIN_PACKAGE_NAME,
    PLUGIN_VERSION,
    ROUGH_SEARCH_MEMES_TOOL_NAME,
    STEAL_TOOL_NAME,
)
from .facade import PluginFacade
from .models import PluginPaths
from .page_api import MoodPageApi
from .tooling import CheckMemesDefTool, RoughSearchMemesTool, StealMemesTool


def _inject_sticker_reminder(req: ProviderRequest, summary: str) -> None:
    if not summary.strip():
        return
    if not req.extra_user_content_parts:
        req.extra_user_content_parts = []
    req.extra_user_content_parts.append(TextPart(text=summary).mark_as_temp())


@register(
    PLUGIN_PACKAGE_NAME,
    "Kalo",
    "此刻的心情：meme_def 精确选图与 tag 分组表情包插件。",
    PLUGIN_VERSION,
)
class MoodOfTheMomentPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self.context = context
        self.config = config or {}
        self.plugin_dir = Path(__file__).resolve().parent
        self.data_dir = StarTools.get_data_dir()
        self.paths = PluginPaths(
            plugin_dir=self.plugin_dir,
            data_dir=self.data_dir,
            stickers_dir=self.data_dir / "meme_defs",
            metadata_db=self.data_dir / "meme_defs.sqlite3",
        )
        self.facade = PluginFacade(
            paths=self.paths, context=context, plugin_config=self.config
        )
        self.steal_tool = StealMemesTool(facade=self.facade)
        self.check_memes_def_tool = CheckMemesDefTool(facade=self.facade)
        self.rough_search_memes_tool = RoughSearchMemesTool(facade=self.facade)
        self.page_api = MoodPageApi(self)
        self._auto_collect_tasks: set[asyncio.Task] = set()
        for tool_name in (
            STEAL_TOOL_NAME,
            CHECK_MEMES_DEF_TOOL_NAME,
            ROUGH_SEARCH_MEMES_TOOL_NAME,
            "steal_memes",
            "check_memes",
            "rough_search",
            "mood_of_the_moment_steal_memes",
            "mood_of_the_moment_check_memes_def",
            "mood_of_the_moment_rough_search_memes",
        ):
            StarTools.unregister_llm_tool(tool_name)
        self.context.add_llm_tools(
            self.steal_tool,
            self.check_memes_def_tool,
            self.rough_search_memes_tool,
        )

    def _finalize_task(self, task: asyncio.Task) -> None:
        self._auto_collect_tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning(f"{PLUGIN_NAME}: 读取后台任务结果失败: {exc}")
            return
        if exc is not None:
            logger.error(f"{PLUGIN_NAME}: 后台任务执行失败: {exc}", exc_info=exc)

    def _track_task(self, task: asyncio.Task) -> None:
        self._auto_collect_tasks.add(task)
        task.add_done_callback(self._finalize_task)

    async def initialize(self):
        self.page_api.register_routes()
        await self.facade.startup()
        logger.info(f"{PLUGIN_NAME}: 插件已初始化")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if self.facade.format_busy:
            return
        message_chain = getattr(event.message_obj, "message", None)
        if not message_chain:
            return
        raw_image_payloads = self.facade.extract_image_segment_payloads(
            getattr(event.message_obj, "raw_message", None)
        )
        raw_image_index = 0
        scheduled = False
        for item in message_chain:
            if not isinstance(item, Image):
                continue
            raw_image_data = (
                raw_image_payloads[raw_image_index]
                if raw_image_index < len(raw_image_payloads)
                else None
            )
            raw_image_index += 1
            should_collect, reason = self.facade.explain_auto_collect_item(
                item, raw_image_data=raw_image_data
            )
            image_url = self.facade.get_image_source(item, raw_image_data)
            logger.info(
                f"{PLUGIN_NAME}: 收到图片消息 unified_msg_origin={event.unified_msg_origin} "
                f"image_url={self.facade.summarize_image_source(image_url or '')} "
                f"decision={should_collect} reason={reason}"
            )
            if not should_collect or not image_url:
                continue
            task = asyncio.create_task(
                self.facade.maybe_auto_collect_image(
                    image_url=image_url,
                    source_origin=str(event.get_group_id()),
                    source_user=str(event.get_sender_id()),
                )
            )
            self._track_task(task)
            scheduled = True
        if scheduled:
            self._track_task(asyncio.create_task(self.facade.maybe_run_cleanup()))

    @filter.on_llm_request()
    async def on_llm_req(self, event: AstrMessageEvent, req: ProviderRequest):
        _ = event
        _inject_sticker_reminder(req, await self.facade.build_llm_summary())

    @filter.on_decorating_result(priority=500)
    async def on_decorating_result(self, event: AstrMessageEvent):
        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return
        new_chain = []
        for item in result.chain:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                decorated = await self.facade.decorate_text(
                    text=text,
                    scope_key=event.unified_msg_origin,
                )
                for segment in decorated.segments:
                    if segment.kind == "image":
                        new_chain.append(Image.fromFileSystem(segment.value))
                    else:
                        new_chain.append(Plain(segment.value))
            else:
                new_chain.append(item)
        result.chain = new_chain

    @filter.command("mood_check")
    async def check_meme(self, event: AstrMessageEvent, limit: int = 5) -> None:
        items = await self.facade.inspect_recent(
            scope_key=event.unified_msg_origin,
            limit=max(1, min(limit, 20)),
        )
        if not items:
            await event.send(MessageChain().message("当前会话还没有此刻的心情图片记录。"))
            return
        lines = [f"当前会话最近 {len(items)} 条图片资产记录："]
        for index, item in enumerate(items, start=1):
            lines.append(
                f"{index}. meme_def={item['meme_def']} | "
                f"tags={','.join(item['tags'])} | usage={item['usage_count']}\n"
                f"   {item['description']}"
            )
        await event.send(MessageChain().message("\n".join(lines)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mood_delete")
    async def delete_asset_command(
        self, event: AstrMessageEvent, asset_id: str = ""
    ) -> None:
        normalized_asset_id = asset_id.strip()
        if not normalized_asset_id:
            await event.send(MessageChain().message("请使用 mood_delete <asset_id> 删除图片资产。"))
            return
        result = await self.facade.delete_asset(normalized_asset_id)
        await event.send(MessageChain().message(result.message))

    async def terminate(self):
        for tool_name in (
            STEAL_TOOL_NAME,
            CHECK_MEMES_DEF_TOOL_NAME,
            ROUGH_SEARCH_MEMES_TOOL_NAME,
        ):
            StarTools.unregister_llm_tool(tool_name)
        if self._auto_collect_tasks:
            for task in list(self._auto_collect_tasks):
                task.cancel()
            await asyncio.gather(
                *list(self._auto_collect_tasks), return_exceptions=True
            )
            self._auto_collect_tasks.clear()
        await self.facade.shutdown()
        logger.info(f"{PLUGIN_NAME}: 插件已停止")
