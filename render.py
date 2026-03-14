from __future__ import annotations

import random
import re

from astrbot.api import logger
from astrbot.api.message_components import Image, Plain

from .models import DecoratedContent, DecoratedSegment, ParsedMarker


class StickerRenderer:
    def __init__(self, storage, max_stickers_per_message: int = 1):
        self.storage = storage
        self.max_stickers_per_message = max(0, int(max_stickers_per_message))

    def build_sticker_list(self) -> str:
        all_tags = self.storage.get_all_tags()
        if not all_tags:
            return ""
        return "\n".join(f"- :{tag}:" for tag in all_tags[:20])

    def build_prompt_catalog(self) -> str:
        all_tags = self.storage.get_all_tags()
        if not all_tags:
            return ""
        tag_index = self.storage.get_tag_index()
        tag_counts = []
        for tag in all_tags:
            meme_ids = tag_index.get(tag, [])
            tag_counts.append((tag, len(meme_ids)))
        tag_counts.sort(key=lambda item: item[1], reverse=True)
        top_tags = [tag for tag, _ in tag_counts[:30]]
        tag_list = ", ".join(f":{tag}:" for tag in top_tags)
        return (
            "<表情包标签库>\n"
            f"可用标签：{tag_list}\n"
            "使用方式：组合多个标签，如 :amused:cat: 表示又开心又是猫的表情\n"
            "提示：标签越多，匹配越精准；没有匹配则不发送表情包\n"
            "</表情包标签库>"
        )

    def parse_markers(self, text: str) -> list[ParsedMarker]:
        markers: list[ParsedMarker] = []
        pattern = re.compile(r"(?:(?::|：)[a-zA-Z0-9_\-\u4e00-\u9fff]+)+(?:[:：])")
        for match in pattern.finditer(text):
            raw_text = match.group(0)
            tags = tuple(tag for tag in re.findall(r"[:：]([a-zA-Z0-9_\-\u4e00-\u9fff]+)", raw_text) if tag)
            if not tags:
                continue
            markers.append(ParsedMarker(raw_text=raw_text, tags=tags, start=match.start(), end=match.end()))
        return markers

    async def decorate_text(self, text: str, scope_key: str) -> DecoratedContent:
        markers = self.parse_markers(text)
        if not markers:
            return DecoratedContent(segments=[DecoratedSegment(kind="text", value=text)])

        segments: list[DecoratedSegment] = []
        cursor = 0
        stickers_used = 0
        for marker in markers:
            if marker.start > cursor:
                segments.append(DecoratedSegment(kind="text", value=text[cursor:marker.start]))
            assets = self.storage.get_memes_by_tags(list(marker.tags))
            if assets and stickers_used < self.max_stickers_per_message:
                meme_data = random.choice(assets)
                file_path = str(meme_data.get("file_path") or "")
                meme_id = str(meme_data.get("meme_id") or "")
                if file_path:
                    segments.append(DecoratedSegment(kind="image", value=file_path))
                    stickers_used += 1
                    if meme_id:
                        self.storage.increment_usage_count(meme_id)
                else:
                    segments.append(DecoratedSegment(kind="text", value=marker.raw_text))
            else:
                segments.append(DecoratedSegment(kind="text", value=marker.raw_text))
            cursor = marker.end
        if cursor < len(text):
            segments.append(DecoratedSegment(kind="text", value=text[cursor:]))
        return DecoratedContent(segments=segments)

    async def render_text(self, text: str) -> list:
        try:
            decorated = await self.decorate_text(text, scope_key="legacy-render")
            components = []
            for segment in decorated.segments:
                if segment.kind == "image":
                    components.append(Image.fromFileSystem(segment.value))
                else:
                    components.append(Plain(segment.value))
            return components
        except Exception as exc:
            logger.error(f"此刻的心情: 处理表情标签时出错: {exc}", exc_info=True)
            return [Plain(text)]
