from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

from .constants import PLUGIN_NAME, PLUGIN_PACKAGE_NAME, PLUGIN_VERSION, STEAL_TOOL_NAME
from .facade import PluginFacade
from .models import PluginPaths
from .tooling import StealMemesTool


def _append_summary(req: ProviderRequest, summary: str) -> None:
    if not summary.strip():
        return
    base = (req.system_prompt or "").strip()
    req.system_prompt = f"{base}\n\n{summary}".strip()


@register(
    PLUGIN_PACKAGE_NAME,
    "Kalo",
    "此刻的心情：纯净重写版 AstrBot 表情包插件。",
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
            stickers_dir=self.data_dir / "stickers",
            metadata_db=self.data_dir / "stickers.sqlite3",
            default_dir=self.plugin_dir / "default",
        )
        self.facade = PluginFacade(paths=self.paths, context=context, plugin_config=self.config)
        self.steal_tool = StealMemesTool(facade=self.facade)
        self._auto_collect_tasks: set[asyncio.Task] = set()
        StarTools.unregister_llm_tool(STEAL_TOOL_NAME)
        self.context.add_llm_tools(self.steal_tool)

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
        await self.facade.startup()
        logger.info(f"{PLUGIN_NAME}: 插件已初始化")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        message_chain = getattr(event.message_obj, "message", None)
        if not message_chain:
            return
        scheduled = False
        for item in message_chain:
            if not isinstance(item, Image):
                continue
            if not self.facade.should_auto_collect_item(item):
                continue
            image_url = item.url or item.path
            if not image_url:
                continue
            task = asyncio.create_task(
                self.facade.maybe_auto_collect_image(
                    image_url=image_url,
                    source_group=str(event.get_group_id()),
                    source_user=str(event.get_sender_id()),
                )
            )
            self._track_task(task)
            scheduled = True
        if scheduled:
            cleanup_task = asyncio.create_task(self.facade.maybe_run_cleanup())
            self._track_task(cleanup_task)

    @filter.on_llm_request()
    async def on_llm_req(self, event: AstrMessageEvent, req: ProviderRequest):
        _ = event
        _append_summary(req, await self.facade.build_llm_summary())

    @filter.on_decorating_result()
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

    @filter.command("smile_check")
    async def check_meme(self, event: AstrMessageEvent, limit: int = 5) -> None:
        limit = max(1, min(limit, 20))
        items = await self.facade.inspect_recent(
            scope_key=event.unified_msg_origin,
            limit=limit,
        )
        if not items:
            await event.send(
                MessageChain().message("当前会话里还没有 cleanroom foundation 发出的图片记录。")
            )
            return
        lines = [f"当前会话最近 {len(items)} 条图片资产记录："]
        for index, item in enumerate(items, start=1):
            lines.append(
                f"{index}. asset_id={item['asset_id']} | group={item['group_name']} | file={item['original_name']} | usage={item['usage_count']}"
            )
        await event.send(MessageChain().message("\n".join(lines)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("smile_delete")
    async def delete_asset_command(self, event: AstrMessageEvent, asset_id: str = "") -> None:
        normalized_asset_id = asset_id.strip()
        if not normalized_asset_id:
            await event.send(MessageChain().message("请使用 smile_delete <asset_id> 删除图片资产。"))
            return
        result = await self.facade.delete_asset(normalized_asset_id)
        await event.send(MessageChain().message(result.message))

    async def terminate(self):
        StarTools.unregister_llm_tool(self.steal_tool.name)
        if self._auto_collect_tasks:
            for task in list(self._auto_collect_tasks):
                task.cancel()
            await asyncio.gather(*list(self._auto_collect_tasks), return_exceptions=True)
            self._auto_collect_tasks.clear()
        await self.facade.shutdown()
        logger.info(f"{PLUGIN_NAME}: 插件已停止")
