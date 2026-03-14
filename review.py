from __future__ import annotations

import json
import re

from astrbot.api import logger

from .constants import (
    DEFAULT_REVIEW_SYSTEM_PROMPT,
    FALLBACK_REVIEW_NEGATIVE_MARKERS,
    FALLBACK_REVIEW_POSITIVE_MARKERS,
)


class ReviewService:
    def __init__(self, context=None, plugin_config=None):
        self.context = context
        self.plugin_config = plugin_config or {}

    def set_context(self, context) -> None:
        self.context = context

    def set_plugin_config(self, plugin_config) -> None:
        self.plugin_config = plugin_config or {}

    def _get_provider(self):
        if self.context is None:
            return None
        try:
            provider_id = self.plugin_config.get("tag_provider_id", "")
            if provider_id:
                return self.context.get_provider_by_id(provider_id)
            return self.context.get_using_provider() or None
        except Exception as exc:
            logger.error(f"此刻的心情: 获取 provider 失败: {exc}")
            return None

    def _get_review_prompt(self) -> str:
        prompt = str(self.plugin_config.get("review_system_prompt", "") or "").strip()
        return prompt or DEFAULT_REVIEW_SYSTEM_PROMPT

    @staticmethod
    def _parse_should_steal(raw_value) -> bool:
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, (int, float)):
            return raw_value != 0
        if isinstance(raw_value, str):
            normalized = raw_value.strip().lower()
            if normalized in {"true", "1", "yes"}:
                return True
            if normalized in {"false", "0", "no"}:
                return False
        return False

    @staticmethod
    def _fallback_should_steal(result_text: str) -> bool:
        text_lower = result_text.lower()
        if any(marker in text_lower for marker in FALLBACK_REVIEW_NEGATIVE_MARKERS):
            return False
        return any(marker in text_lower for marker in FALLBACK_REVIEW_POSITIVE_MARKERS)

    async def review_image(self, image_url: str) -> dict:
        provider = self._get_provider()
        if provider is None:
            return {
                "should_steal": False,
                "reason": "未找到可用的 LLM 提供商，无法审查图片",
                "tags": [],
            }
        try:
            response = await provider.text_chat(
                prompt=self._get_review_prompt(), image_urls=[image_url]
            )
            if response is None or not hasattr(response, "completion_text"):
                return {"should_steal": False, "reason": "LLM 返回结果为空", "tags": []}
            result_text = response.completion_text.strip()
            json_match = re.search(r"\{[^}]*\}", result_text, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                    tags = result.get("tags", [])
                    return {
                        "should_steal": self._parse_should_steal(
                            result.get("should_steal", False)
                        ),
                        "reason": str(result.get("reason", "未知")),
                        "tags": list(tags) if isinstance(tags, list) else [],
                    }
                except json.JSONDecodeError:
                    pass
            should_steal = self._fallback_should_steal(result_text)
            tags = []
            if should_steal:
                tag_matches = re.findall(r'["\']([^"\']{2,10})["\']', result_text)
                tags = tag_matches[:4] if tag_matches else ["未分类"]
            return {
                "should_steal": should_steal,
                "reason": result_text[:200] if not should_steal else "判断为表情包",
                "tags": tags,
            }
        except Exception as exc:
            logger.error(f"此刻的心情: LLM 审查图片失败: {exc}", exc_info=True)
            return {"should_steal": False, "reason": f"审查过程出错: {exc}", "tags": []}
