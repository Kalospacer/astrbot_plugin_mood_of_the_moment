from __future__ import annotations

import random
import re
from itertools import combinations

from astrbot.api import logger
from astrbot.api.message_components import Image, Plain

from .models import DecoratedContent, DecoratedSegment, ParsedMarker


class StickerRenderer:
    def __init__(self, storage, max_stickers_per_message: int = 1):
        self.storage = storage
        self.max_stickers_per_message = max(0, int(max_stickers_per_message))

    async def build_sticker_list(self) -> str:
        all_tags = await self.storage.get_all_tags()
        if not all_tags:
            return ""
        return "\n".join(f"- :{tag}:" for tag in all_tags[:20])

    async def build_prompt_catalog(self) -> str:
        all_tags = await self.storage.get_all_tags()
        if not all_tags:
            return ""
        tag_index = await self.storage.get_tag_index()
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
            tags = tuple(
                tag
                for tag in re.findall(r"[:：]([a-zA-Z0-9_\-\u4e00-\u9fff]+)", raw_text)
                if tag
            )
            if not tags:
                continue
            markers.append(
                ParsedMarker(
                    raw_text=raw_text,
                    tags=tags,
                    start=match.start(),
                    end=match.end(),
                )
            )
        return markers

    def _score_asset_match(
        self, requested_tags: tuple[str, ...], candidate_tags: list[str]
    ) -> float:
        if not requested_tags:
            return 0.0
        normalized_requested = [tag.strip() for tag in requested_tags if tag.strip()]
        normalized_candidate = [
            str(tag).strip() for tag in candidate_tags if str(tag).strip()
        ]
        if not normalized_candidate:
            return 0.0

        candidate_set = set(normalized_candidate)
        score = 0.0
        exact_hits = [tag for tag in normalized_requested if tag in candidate_set]
        score += len(exact_hits) * 10.0
        score += (len(exact_hits) / len(normalized_requested)) * 5.0

        first_tag = normalized_requested[0]
        if first_tag in candidate_set:
            score += 8.0

        for requested in normalized_requested:
            for candidate in normalized_candidate:
                if requested == candidate:
                    continue
                if requested in candidate or candidate in requested:
                    score += 2.5
                elif requested[:2] and candidate[:2] and requested[:2] == candidate[:2]:
                    score += 1.0

        if not exact_hits and first_tag not in candidate_set:
            score -= 0.5
        return score

    def _iter_tag_subsets(
        self, requested_tags: tuple[str, ...]
    ) -> list[tuple[str, ...]]:
        normalized = tuple(tag for tag in requested_tags if tag)
        subsets: list[tuple[str, ...]] = []
        for size in range(len(normalized), 0, -1):
            level = list(combinations(normalized, size))
            level.sort(key=lambda subset: (subset[0] != normalized[0], subset))
            subsets.extend(level)
        return subsets

    def _pick_top_scored_asset(
        self, scored_assets: list[tuple[float, dict]]
    ) -> dict | None:
        if not scored_assets:
            return None
        scored_assets.sort(
            key=lambda item: (
                item[0],
                int(item[1].get("usage_count") or 0) * -1,
                float(item[1].get("added_time") or 0.0),
            ),
            reverse=True,
        )
        top_score = scored_assets[0][0]
        if top_score <= 0:
            return None
        top_assets = [asset for score, asset in scored_assets if score == top_score]
        return random.choice(top_assets) if top_assets else None

    async def _select_best_asset(self, requested_tags: tuple[str, ...]) -> dict | None:
        for subset in self._iter_tag_subsets(requested_tags):
            exact_assets = await self.storage.get_memes_by_tags(list(subset))
            if exact_assets:
                if len(subset) == len(requested_tags):
                    return random.choice(exact_assets)
                return self._pick_top_scored_asset(
                    [
                        (
                            self._score_asset_match(
                                requested_tags, list(asset.get("tags") or [])
                            ),
                            asset,
                        )
                        for asset in exact_assets
                    ]
                )

        candidates = await self.storage.get_all_memes()
        if not candidates:
            return None

        scored_candidates: list[tuple[float, dict]] = []
        for candidate in candidates:
            candidate_tags = list(candidate.get("tags") or [])
            score = self._score_asset_match(requested_tags, candidate_tags)
            scored_candidates.append((score, candidate))
        return self._pick_top_scored_asset(scored_candidates)

    async def decorate_text(self, text: str, scope_key: str) -> DecoratedContent:
        markers = self.parse_markers(text)
        if not markers:
            return DecoratedContent(
                segments=[DecoratedSegment(kind="text", value=text)]
            )

        segments: list[DecoratedSegment] = []
        cursor = 0
        stickers_used = 0
        for marker in markers:
            if marker.start > cursor:
                segments.append(
                    DecoratedSegment(kind="text", value=text[cursor : marker.start])
                )

            meme_data = await self._select_best_asset(marker.tags)
            if meme_data is not None and stickers_used < self.max_stickers_per_message:
                file_path = str(meme_data.get("file_path") or "")
                meme_id = str(meme_data.get("meme_id") or "")
                if file_path:
                    segments.append(DecoratedSegment(kind="image", value=file_path))
                    stickers_used += 1
                    if meme_id:
                        await self.storage.increment_usage_count(meme_id, scope_key)
                else:
                    segments.append(
                        DecoratedSegment(kind="text", value=marker.raw_text)
                    )
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
