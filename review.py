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
            provider_id = self.plugin_config.get("meme_review_provider_id", "")
            if provider_id:
                return self.context.get_provider_by_id(provider_id)
            return self.context.get_using_provider() or None
        except Exception as exc:
            logger.error(f"此刻的心情: 获取 review provider 失败: {exc}", exc_info=True)
            return None

    def _get_review_prompt(self) -> str:
        prompt = str(self.plugin_config.get("review_system_prompt", "") or "").strip()
        return prompt or DEFAULT_REVIEW_SYSTEM_PROMPT

    @staticmethod
    def _parse_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes"}
        return False

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        decoder = json.JSONDecoder()
        start = text.find("{")
        while start >= 0:
            try:
                value, _ = decoder.raw_decode(text[start:])
                return value if isinstance(value, dict) else None
            except json.JSONDecodeError:
                start = text.find("{", start + 1)
        return None

    @staticmethod
    def _fallback_should_steal(result_text: str) -> bool:
        lowered = result_text.lower()
        if any(marker in lowered for marker in FALLBACK_REVIEW_NEGATIVE_MARKERS):
            return False
        return any(marker in lowered for marker in FALLBACK_REVIEW_POSITIVE_MARKERS)

    @staticmethod
    def _empty_result(reason: str) -> dict:
        return {
            "should_steal": False,
            "reason": reason,
            "description": "",
            "filename": "",
            "tags": [],
        }

    async def review_image(
        self,
        image_url: str,
        reference_context: str | None = None,
    ) -> dict:
        provider = self._get_provider()
        if provider is None:
            return self._empty_result("未找到可用的 LLM 提供商，无法审查图片")
        try:
            prompt = self._get_review_prompt()
            if reference_context and reference_context.strip():
                prompt = (
                    f"{prompt}\n\n以下是这张图片在旧库中的参考信息，仅供参考，"
                    "不要直接沿用旧 tags 或旧描述，请以你看到的图片内容重新生成：\n"
                    f"{reference_context.strip()}"
                )
            response = await provider.text_chat(
                prompt=prompt, image_urls=[image_url]
            )
            result_text = str(getattr(response, "completion_text", "") or "").strip()
            if not result_text:
                return self._empty_result("LLM 返回结果为空")
            payload = self._extract_json(result_text)
            if payload is None:
                return self._empty_result("LLM 未返回合法 JSON")

            should_steal = self._parse_bool(payload.get("should_steal"))
            reason = str(payload.get("reason") or "未知").strip()
            description = str(payload.get("description") or "").strip()
            filename = str(payload.get("filename") or "").strip()
            raw_tags = payload.get("tags")
            tags = (
                [str(item).strip() for item in raw_tags if str(item).strip()]
                if isinstance(raw_tags, list)
                else []
            )
            if not should_steal:
                return {
                    "should_steal": False,
                    "reason": reason,
                    "description": description,
                    "filename": filename,
                    "tags": tags,
                }
            if not description or not filename or not tags:
                return self._empty_result(
                    "LLM 判定可以保存，但缺少 filename、description 或 tags"
                )
            return {
                "should_steal": True,
                "reason": reason,
                "description": description,
                "filename": filename,
                "tags": tags,
            }
        except Exception as exc:
            logger.error(f"此刻的心情: LLM 识图失败: {exc}", exc_info=True)
            return self._empty_result(f"识图过程出错: {exc}")
