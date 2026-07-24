from __future__ import annotations

from collections import deque
from collections.abc import Iterable
import re
from difflib import SequenceMatcher
from itertools import combinations

from astrbot.api import logger
from astrbot.api.message_components import Image, Plain

from .models import DecoratedContent, DecoratedSegment, ParsedMarker


class StickerRenderer:
    def __init__(
        self,
        storage,
        max_stickers_per_message: int = 1,
        max_prompt_tags: int = 30,
        max_prompt_meme_defs: int = 30,
    ):
        self.storage = storage
        self.max_stickers_per_message = max(0, int(max_stickers_per_message))
        self.max_prompt_tags = max(0, int(max_prompt_tags))
        self.max_prompt_meme_defs = max(0, int(max_prompt_meme_defs))

    async def build_prompt_catalog(self) -> str:
        meme_defs = await self.storage.get_all_meme_defs(self.max_prompt_meme_defs)
        tags = (await self.storage.get_all_tags())[: self.max_prompt_tags]
        if not meme_defs and not tags:
            return ""

        lines = [
            "<此刻的心情·表情包索引>",
            "精确表情定义：",
        ]
        lines.extend(f":{meme_def}:" for meme_def in meme_defs)
        lines.append("分组标签：")
        lines.extend(f":{tag}:" for tag in tags)
        lines.extend(
            [
                "规则：",
                "- 已知具体图片时，优先输出单个 meme_def。",
                "- 不确定 meme_def 含义时，调用 mood_check_memes_def。",
                "- 不知道候选名称时，调用 mood_rough_search_memes。",
                "- 也可以输出单个或组合 tag 作为兜底。",
                "- 一个 meme_def 只对应一张图片；tag 可以对应多张图片。",
                "</此刻的心情·表情包索引>",
            ]
        )
        return "\n".join(lines)

    def parse_markers(self, text: str) -> list[ParsedMarker]:
        markers: list[ParsedMarker] = []
        # 标签必须从独立边界开始。否则会把 AstrBot 的 unified_msg_origin
        #（例如 `michelle2:GroupMessage:123`）中的 `:GroupMessage:` 当成标签，
        # 进而在未命中资产时丢失原始会话 ID。
        pattern = re.compile(
            r"(?<![a-zA-Z0-9_-])"
            r"(?:(?::|：)[a-zA-Z0-9_\-\u4e00-\u9fff ]+)+(?:[:：])"
        )
        token_pattern = re.compile(r"[:：]([a-zA-Z0-9_\-\u4e00-\u9fff ]+)")
        for match in pattern.finditer(text):
            raw_text = match.group(0)
            tokens = tuple(
                token.strip()
                for token in token_pattern.findall(raw_text)
                if token.strip()
            )
            if tokens:
                markers.append(
                    ParsedMarker(
                        raw_text=raw_text,
                        tokens=tokens,
                        start=match.start(),
                        end=match.end(),
                    )
                )
        return markers

    @staticmethod
    def _calculate_similarity(left: str, right: str) -> float:
        if left == right:
            return 1.0
        if not left or not right:
            return 0.0
        return SequenceMatcher(None, left.casefold(), right.casefold()).ratio()

    def _score_asset_match(
        self, requested_tags: tuple[str, ...], candidate_tags: Iterable[str]
    ) -> float:
        normalized_requested = [tag.strip() for tag in requested_tags if tag.strip()]
        normalized_candidate = [
            str(tag).strip() for tag in candidate_tags if str(tag).strip()
        ]
        if not normalized_requested or not normalized_candidate:
            return 0.0

        candidate_set = {tag.casefold() for tag in normalized_candidate}
        score = 0.0
        exact_matches = 0
        for requested in normalized_requested:
            if requested.casefold() in candidate_set:
                score += 10.0
                exact_matches += 1
        if exact_matches:
            score += (exact_matches / len(normalized_requested)) * 5.0
        if normalized_requested[0].casefold() in candidate_set:
            score += 8.0

        for requested in normalized_requested:
            for candidate in normalized_candidate:
                if requested.casefold() == candidate.casefold():
                    continue
                similarity = self._calculate_similarity(requested, candidate)
                if similarity >= 0.8:
                    score += 5.0 * similarity
                elif similarity >= 0.6:
                    score += 2.0 * similarity
                if requested.casefold() in candidate.casefold() or candidate.casefold() in requested.casefold():
                    score += 2.5
                elif len(requested) >= 2 and len(candidate) >= 2:
                    if requested[:2].casefold() == candidate[:2].casefold():
                        score += 1.0
        return score

    @staticmethod
    def _iter_tag_subsets(requested_tags: tuple[str, ...]) -> list[tuple[str, ...]]:
        normalized = tuple(tag for tag in requested_tags if tag)
        result: list[tuple[str, ...]] = []
        for size in range(len(normalized), 0, -1):
            result.extend(combinations(normalized, size))
        return result

    @staticmethod
    def _pick_top_scored_asset(scored_assets: list[tuple[float, dict]]) -> dict | None:
        if not scored_assets:
            return None
        scored_assets.sort(
            key=lambda item: (
                -item[0],
                float(item[1].get("last_used_at") or 0.0),
                int(item[1].get("usage_count") or 0),
                str(item[1].get("meme_def") or "").casefold(),
            )
        )
        return scored_assets[0][1] if scored_assets[0][0] > 0 else None

    async def _select_best_tag_asset(self, requested_tags: tuple[str, ...]) -> dict | None:
        if not requested_tags:
            return None
        for size in range(len(requested_tags), 0, -1):
            candidates: list[dict] = []
            for subset in combinations(requested_tags, size):
                candidates.extend(
                    await self.storage.get_memes_by_tags(list(subset), match_all=True)
                )
            unique: dict[str, dict] = {
                str(asset.get("asset_id")): asset
                for asset in candidates
                if asset.get("asset_id")
            }
            if unique:
                scored = [
                    (
                        self._score_asset_match(
                            requested_tags, tuple(asset.get("tags") or ())
                        ),
                        asset,
                    )
                    for asset in unique.values()
                ]
                return self._pick_top_scored_asset(scored)
        return None

    async def _select_asset(self, tokens: tuple[str, ...]) -> dict | None:
        if len(tokens) == 1:
            exact = await self.storage.get_meme_by_def(tokens[0])
            if exact is not None:
                return exact
        return await self._select_best_tag_asset(tokens)

    async def decorate_text(self, text: str, scope_key: str) -> DecoratedContent:
        markers = self.parse_markers(text)
        if not markers:
            return DecoratedContent(segments=[DecoratedSegment(kind="text", value=text)])

        segments: list[DecoratedSegment] = []
        cursor = 0
        stickers_used = 0
        for marker in markers:
            if marker.start > cursor:
                segments.append(
                    DecoratedSegment(kind="text", value=text[cursor : marker.start])
                )
            asset = await self._select_asset(marker.tokens)
            if asset is not None and stickers_used < self.max_stickers_per_message:
                file_path = str(asset.get("file_path") or "")
                asset_id = str(asset.get("asset_id") or "")
                if file_path:
                    segments.append(DecoratedSegment(kind="image", value=file_path))
                    stickers_used += 1
                    if asset_id:
                        await self.storage.increment_usage_count(asset_id, scope_key)
            cursor = marker.end

        if cursor < len(text):
            segments.append(DecoratedSegment(kind="text", value=text[cursor:]))

        merged: list[DecoratedSegment] = []
        text_buffer: list[str] = []
        for segment in segments:
            if segment.kind == "text":
                if segment.value:
                    text_buffer.append(segment.value)
                continue
            if text_buffer:
                merged.append(DecoratedSegment(kind="text", value="".join(text_buffer)))
                text_buffer = []
            merged.append(segment)
        if text_buffer:
            merged.append(DecoratedSegment(kind="text", value="".join(text_buffer)))
        return DecoratedContent(segments=merged)

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
