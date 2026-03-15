from __future__ import annotations

import random
import re
from difflib import SequenceMatcher
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
        all_tags = await self.storage.get_tag_index()
        tag_counts = []
        for tag, meme_ids in all_tags.items():
            tag_counts.append((tag, len(meme_ids)))
        tag_counts.sort(key=lambda item: item[1], reverse=True)
        top_tags = [tag for tag, _ in tag_counts[:30]]
        tag_list = ", ".join(f":{tag}:" for tag in top_tags)
        return (
            "<表情包标签库>\n"
            f"可用标签：{tag_list}\n"
            "使用方式：组合多个标签，如 :amused:cat: 表示又开心又是猫的表情\n"
            "提示：标签越多，匹配越精准；没有匹配则静默删除标签不发送表情包\n"
            "</表情包标签库>"
        )

    def parse_markers(self, text: str) -> list[ParsedMarker]:
        markers: list[ParsedMarker] = []
        # 支持中英文冒号，标签可包含字母、数字、下划线、连字符、中文字符
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

    def _calculate_similarity(self, s1: str, s2: str) -> float:
        """计算两个字符串的相似度 (0.0 - 1.0)"""
        if s1 == s2:
            return 1.0
        if not s1 or not s2:
            return 0.0
        return SequenceMatcher(None, s1.lower(), s2.lower()).ratio()

    def _score_asset_match(
        self, requested_tags: tuple[str, ...], candidate_tags: list[str]
    ) -> float:
        """
        评分体系（优化版）：
        - 精确匹配标签：+10分/个
        - 匹配比例加成：+(匹配数/请求数)×5
        - 首标签匹配：+8分
        - 高相似度匹配(>=0.8)：+5分
        - 中等相似度匹配(>=0.6)：+2分
        - 子串匹配：+2.5分
        - 前缀匹配(前2字符)：+1分
        """
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
        exact_matches = 0
        
        # 精确匹配得分（最高优先级）
        for tag in normalized_requested:
            if tag in candidate_set:
                score += 10.0
                exact_matches += 1
        
        if exact_matches > 0:
            score += (exact_matches / len(normalized_requested)) * 5.0

        # 首标签匹配额外加分
        first_tag = normalized_requested[0]
        if first_tag in candidate_set:
            score += 8.0

        # 模糊匹配：相似度、子串匹配和前缀匹配
        for requested in normalized_requested:
            for candidate in normalized_candidate:
                if requested == candidate:
                    continue  # 精确匹配已计分
                
                # 计算相似度
                similarity = self._calculate_similarity(requested, candidate)
                if similarity >= 0.8:
                    score += 5.0 * similarity  # 高相似度
                elif similarity >= 0.6:
                    score += 2.0 * similarity  # 中等相似度
                
                # 子串匹配
                if requested in candidate or candidate in requested:
                    score += 2.5
                # 前缀匹配（前2字符）
                elif len(requested) >= 2 and len(candidate) >= 2:
                    if requested[:2].lower() == candidate[:2].lower():
                        score += 1.0

        # 完全没有任何匹配时返回0分（表示不相关）
        if score == 0.0:
            return 0.0
        return score

    def _iter_tag_subsets_by_level(
        self, requested_tags: tuple[str, ...]
    ) -> list[tuple[int, list[tuple[str, ...]]]]:
        """
        按匹配数层级生成所有子集组合
        返回：[(匹配数, [子集1, 子集2, ...]), ...]
        例如3个标签abc → [(3, [(a,b,c)]), (2, [(a,b), (a,c), (b,c)]), (1, [(a,), (b,), (c,)])]
        """
        normalized = tuple(tag for tag in requested_tags if tag)
        if not normalized:
            return []
        
        result: list[tuple[int, list[tuple[str, ...]]]] = []
        for size in range(len(normalized), 0, -1):
            level_subsets = list(combinations(normalized, size))
            if level_subsets:
                result.append((size, level_subsets))
        return result

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
        """
        逐级降级匹配算法（5→4→3→2→1→静默失败）：
        1. 按匹配标签数从高到低遍历
        2. 每级收集所有该匹配数的候选资源
        3. 只要某一级有候选，立即在该级内评分选最优返回
        4. 完全不匹配时返回 None（静默失败）
        """
        if not requested_tags:
            return None
        
        # 生成按匹配数分组的子集
        subsets_by_level = self._iter_tag_subsets_by_level(requested_tags)
        
        # 逐级查询（从高到低）
        for match_count, subsets in subsets_by_level:
            candidates = []
            for subset in subsets:
                # match_all=True: 资源必须包含子集中的所有标签
                assets = await self.storage.get_memes_by_tags(list(subset), match_all=True)
                candidates.extend(assets)
            
            if candidates:
                # 去重（同一个资源可能被多个子集匹配到）
                seen_ids = set()
                unique_candidates = []
                for asset in candidates:
                    meme_id = asset.get("meme_id")
                    if meme_id and meme_id not in seen_ids:
                        seen_ids.add(meme_id)
                        unique_candidates.append(asset)
                
                # 在该匹配级别内评分选最优
                scored = [
                    (self._score_asset_match(requested_tags, list(asset.get("tags") or [])), asset)
                    for asset in unique_candidates
                ]
                return self._pick_top_scored_asset(scored)
        
        # 逐级降级完全无命中，返回 None（静默失败）
        return None

    async def decorate_text(self, text: str, scope_key: str) -> DecoratedContent:
        """
        处理文本中的标签，替换为表情包图片
        未匹配的标签会被静默删除（不显示给用户）
        """
        markers = self.parse_markers(text)
        if not markers:
            return DecoratedContent(
                segments=[DecoratedSegment(kind="text", value=text)]
            )

        segments: list[DecoratedSegment] = []
        cursor = 0
        stickers_used = 0
        replaced_ranges = []  # 记录成功替换的位置
        
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
                    replaced_ranges.append((marker.start, marker.end))
                    if meme_id:
                        await self.storage.increment_usage_count(meme_id, scope_key)
                else:
                    # 表情包数据存在但文件路径无效，静默删除该标签
                    pass
            else:
                # 未匹配到表情包，静默删除该标签（不添加到消息中）
                pass
            cursor = marker.end
        
        if cursor < len(text):
            segments.append(DecoratedSegment(kind="text", value=text[cursor:]))
        
        # 最终检查：扫描原始文本，删除残留的未匹配标签
        final_text = self._remove_unmatched_tags(text, replaced_ranges)
        
        # 重新构建segments（基于清理后的文本）
        return await self._rebuild_segments_from_text(final_text, segments)
    
    def _remove_unmatched_tags(self, text: str, replaced_ranges: list[tuple[int, int]]) -> str:
        """
        删除文本中未被替换的标签
        """
        if not replaced_ranges:
            # 没有任何替换，删除所有标签
            pattern = re.compile(r"(?:(?::|：)[a-zA-Z0-9_\-\u4e00-\u9fff]+)+(?:[:：])")
            return pattern.sub("", text)
        
        # 标记哪些位置被替换了
        result_parts = []
        last_end = 0
        
        # 先找到所有标签位置
        all_markers = self.parse_markers(text)
        
        for marker in all_markers:
            # 添加标签前的文本
            if marker.start > last_end:
                result_parts.append(text[last_end:marker.start])
            
            # 检查这个标签是否被替换了
            is_replaced = any(
                marker.start == r_start and marker.end == r_end 
                for r_start, r_end in replaced_ranges
            )
            
            if is_replaced:
                # 被替换的标签保留位置（后续会被图片替代）
                result_parts.append(f"\x00STICKER\x00")  # 占位符
            # 未被替换的标签直接删除（不添加）
            
            last_end = marker.end
        
        # 添加最后剩余的文本
        if last_end < len(text):
            result_parts.append(text[last_end:])
        
        return "".join(result_parts)
    
    async def _rebuild_segments_from_text(
        self, text: str, original_segments: list[DecoratedSegment]
    ) -> DecoratedContent:
        """
        根据清理后的文本和原始segments重建消息
        """
        if "\x00STICKER\x00" not in text:
            # 没有表情包，直接返回纯文本
            return DecoratedContent(segments=[DecoratedSegment(kind="text", value=text)])
        
        # 重建segments，将占位符替换为对应的图片
        new_segments: list[DecoratedSegment] = []
        parts = text.split("\x00STICKER\x00")
        image_segments = [s for s in original_segments if s.kind == "image"]
        
        for i, part in enumerate(parts):
            if part:
                new_segments.append(DecoratedSegment(kind="text", value=part))
            if i < len(parts) - 1 and i < len(image_segments):
                new_segments.append(image_segments[i])
        
        return DecoratedContent(segments=new_segments)

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
