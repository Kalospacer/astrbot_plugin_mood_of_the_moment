from __future__ import annotations

import asyncio
import base64
import binascii
import mimetypes
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from astrbot.api import logger
from quart import request, send_file

from .constants import PLUGIN_NAME, PLUGIN_PACKAGE_NAME
from .models import StickerAsset
from .utils import normalize_category_name, normalize_tag_display_name, safe_filename

PAGE_API_PREFIX = f"/{PLUGIN_PACKAGE_NAME}/page"


class MoodPageApi:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def register_routes(self) -> None:
        if not hasattr(self.plugin.context, "register_web_api"):
            logger.debug(f"{PLUGIN_NAME}: 当前 AstrBot 版本未提供 register_web_api，跳过 WebUI API 注册")
            return
        register = self.plugin.context.register_web_api
        routes = [
            ("/overview", self.get_overview, ["GET"], "Mood sticker page overview"),
            ("/stickers", self.list_stickers, ["GET"], "Mood sticker page list"),
            ("/sticker", self.get_sticker, ["GET"], "Mood sticker page detail"),
            ("/sticker/image", self.get_sticker_image, ["GET"], "Mood sticker image"),
            (
                "/sticker/image_data",
                self.get_sticker_image_data,
                ["GET"],
                "Mood sticker image data",
            ),
            ("/sticker/import", self.import_sticker, ["POST"], "Mood sticker import"),
            ("/sticker/upload", self.upload_sticker, ["POST"], "Mood sticker upload"),
            ("/sticker/update", self.update_sticker, ["POST"], "Mood sticker update"),
            ("/sticker/delete", self.delete_sticker, ["POST"], "Mood sticker delete"),
            (
                "/sticker/bulk_delete",
                self.bulk_delete_stickers,
                ["POST"],
                "Mood sticker bulk delete",
            ),
            (
                "/maintenance/prune_missing",
                self.prune_missing_assets,
                ["POST"],
                "Mood sticker prune missing assets",
            ),
            (
                "/dedup/candidates",
                self.get_duplicate_candidates,
                ["GET"],
                "Mood sticker duplicate candidates",
            ),
            (
                "/dedup/rebuild",
                self.rebuild_dhash_index,
                ["POST"],
                "Mood sticker rebuild dHash index",
            ),
            ("/config", self.get_config, ["GET"], "Mood sticker config"),
            ("/config/update", self.update_config, ["POST"], "Mood sticker config update"),
        ]
        for path, handler, methods, desc in routes:
            register(f"{PAGE_API_PREFIX}{path}", handler, methods, desc)

    async def get_overview(self) -> dict[str, Any]:
        try:
            assets = await self.plugin.facade.storage.query_assets()
            tag_counts: dict[str, int] = {}
            group_counts: dict[str, int] = {}
            missing_count = 0
            for asset in assets:
                group_counts[asset.group_name] = group_counts.get(asset.group_name, 0) + 1
                for tag in asset.labels or (asset.group_name,):
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
                resolved = await self.plugin.facade.storage.resolve_path(asset.storage_key)
                if not resolved.exists():
                    missing_count += 1
            return self._ok(
                {
                    "total": len(assets),
                    "missing": missing_count,
                    "groups": self._ranked_counts(group_counts),
                    "tags": self._ranked_counts(tag_counts),
                    "config": {
                        "max_stickers": self.plugin.config.get("max_stickers"),
                        "auto_steal": bool(
                            self.plugin.config.get("enable_auto_steal", True)
                        ),
                        "auto_cleanup": bool(
                            self.plugin.config.get("enable_auto_cleanup", True)
                        ),
                    },
                }
            )
        except Exception as exc:
            logger.error(f"{PLUGIN_NAME}: WebUI overview failed: {exc}", exc_info=True)
            return self._error(str(exc))

    async def list_stickers(self) -> dict[str, Any]:
        try:
            query = self._query("q", 80).casefold()
            group = self._query("group", 80)
            tag = self._query("tag", 80)
            status = self._query("status", 32)
            sort_by = self._query("sort_by", 32) or "created_at"
            sort_order = self._query("sort_order", 16) or "desc"
            if sort_by not in {
                "created_at",
                "last_used_at",
                "usage_count",
                "original_name",
                "group_name",
                "asset_id",
            }:
                sort_by = "created_at"
            sort_order = "asc" if sort_order == "asc" else "desc"
            page = self._query_int("page", 1, 1, 9999)
            page_size = self._query_int("page_size", 48, 12, 120)

            assets = await self.plugin.facade.storage.query_assets()
            filtered: list[StickerAsset] = []
            for asset in assets:
                if group and asset.group_name != group:
                    continue
                if tag and tag not in set(asset.labels or (asset.group_name,)):
                    continue
                if status == "missing":
                    resolved = await self.plugin.facade.storage.resolve_path(asset.storage_key)
                    if resolved.exists():
                        continue
                haystack = " ".join(
                    [
                        asset.asset_id,
                        asset.group_name,
                        asset.original_name,
                        str(asset.description or ""),
                        str(asset.source or ""),
                        " ".join(asset.labels),
                    ]
                ).casefold()
                if query and query not in haystack:
                    continue
                filtered.append(asset)

            filtered.sort(
                key=self._asset_sort_key(sort_by),
                reverse=sort_order == "desc",
            )
            start = (page - 1) * page_size
            page_items = filtered[start : start + page_size]
            return self._ok(
                {
                    "items": [
                        await self._serialize_asset(asset, include_path=False)
                        for asset in page_items
                    ],
                    "page": page,
                    "page_size": page_size,
                    "total": len(filtered),
                    "sort_by": sort_by,
                    "sort_order": sort_order,
                }
            )
        except Exception as exc:
            logger.error(f"{PLUGIN_NAME}: WebUI list failed: {exc}", exc_info=True)
            return self._error(str(exc))

    async def get_sticker(self) -> dict[str, Any]:
        asset_id = self._query("asset_id", 120)
        if not asset_id:
            return self._error("缺少 asset_id")
        asset = await self.plugin.facade.storage.get_asset(asset_id)
        if asset is None:
            return self._error("没有找到这个贴纸")
        return self._ok(await self._serialize_asset(asset, include_path=True))

    async def get_sticker_image(self):
        resolved = await self._resolve_asset_image_path()
        if isinstance(resolved, dict):
            return self._error(str(resolved.get("error") or "图片不存在"))
        response = await send_file(resolved)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    async def get_sticker_image_data(self) -> dict[str, Any]:
        resolved = await self._resolve_asset_image_path()
        if isinstance(resolved, dict):
            return self._error(str(resolved.get("error") or "图片不存在"))
        try:
            data = await self._read_file_base64(resolved)
            mime = mimetypes.guess_type(str(resolved))[0] or "image/png"
            return self._ok({"data_url": f"data:{mime};base64,{data}", "mime": mime})
        except Exception as exc:
            logger.error(f"{PLUGIN_NAME}: WebUI image read failed: {exc}", exc_info=True)
            return self._error(str(exc))

    async def import_sticker(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        image_source = self._single_line(payload.get("image_source"), 500)
        group_name = normalize_category_name(payload.get("group_name"))
        description = self._single_line(payload.get("description"), 500)
        preferred_name = self._single_line(payload.get("preferred_name"), 160) or None
        labels = self._normalize_labels(payload.get("labels"), fallback=group_name)
        if not image_source:
            return self._error("请提供本地图片路径或 http/https 图片地址")
        try:
            if image_source.startswith(("http://", "https://")):
                result = await self.plugin.facade.save_remote_image(
                    image_url=image_source,
                    group_name=group_name,
                    description=description,
                    preferred_name=preferred_name,
                    labels=labels,
                    source="webui_remote",
                )
            else:
                result = await self.plugin.facade.ingest_local_file(
                    source_path=image_source,
                    group_name=group_name,
                    description=description,
                    preferred_name=preferred_name,
                    labels=labels,
                    source="webui_local",
                )
            if not result.ok:
                return self._error(result.message)
            return self._ok(
                {
                    "message": result.message,
                    "asset": await self._serialize_asset(result.asset, include_path=True)
                    if result.asset
                    else None,
                }
            )
        except Exception as exc:
            logger.error(f"{PLUGIN_NAME}: WebUI import failed: {exc}", exc_info=True)
            return self._error(str(exc))

    async def upload_sticker(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        raw_data_url = str(payload.get("data_url") or "").strip()
        original_name = self._single_line(payload.get("filename"), 180) or "upload.png"
        group_name = normalize_category_name(payload.get("group_name"))
        description = self._single_line(payload.get("description"), 500)
        labels = self._normalize_labels(payload.get("labels"), fallback=group_name)
        if not raw_data_url:
            return self._error("请选择要上传的图片文件")

        try:
            if "," in raw_data_url and raw_data_url.startswith("data:"):
                _, raw_base64 = raw_data_url.split(",", 1)
            else:
                raw_base64 = raw_data_url
            image_bytes = base64.b64decode(raw_base64, validate=True)
        except (ValueError, binascii.Error):
            return self._error("上传内容不是有效的 base64 图片")

        max_bytes = int(self.plugin.config.get("webui_upload_max_mb", 20) or 20) * 1024 * 1024
        if len(image_bytes) > max_bytes:
            return self._error(f"上传文件超过 {max_bytes // 1024 // 1024} MB")

        upload_dir = self.plugin.paths.data_dir / "webui_uploads"
        upload_name = safe_filename(
            f"{int(time.time() * 1000)}_{original_name}",
            Path(original_name).suffix or ".png",
        )
        upload_path = upload_dir / upload_name
        try:
            await asyncio.to_thread(upload_dir.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(upload_path.write_bytes, image_bytes)
            result = await self.plugin.facade.ingest_local_file(
                source_path=str(upload_path),
                group_name=group_name,
                description=description,
                preferred_name=original_name,
                labels=labels,
                source="webui_upload",
            )
            if not result.ok:
                return self._error(result.message)
            return self._ok(
                {
                    "message": result.message,
                    "asset": await self._serialize_asset(result.asset, include_path=True)
                    if result.asset
                    else None,
                }
            )
        except Exception as exc:
            logger.error(f"{PLUGIN_NAME}: WebUI upload failed: {exc}", exc_info=True)
            return self._error(str(exc))
        finally:
            try:
                await asyncio.to_thread(upload_path.unlink, missing_ok=True)
            except OSError:
                pass

    async def update_sticker(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        asset_id = self._single_line(payload.get("asset_id"), 120)
        if not asset_id:
            return self._error("缺少 asset_id")
        group_name = normalize_category_name(payload.get("group_name"))
        labels = self._normalize_labels(payload.get("labels"), fallback=group_name)
        try:
            asset = await self.plugin.facade.storage.update_asset_metadata(
                asset_id,
                group_name=group_name,
                description=self._single_line(payload.get("description"), 500),
                source=self._single_line(payload.get("source"), 500),
                labels=labels,
            )
            if asset is None:
                return self._error("没有找到这个贴纸")
            file_path = await self.plugin.facade.storage.resolve_path(asset.storage_key)
            await self.plugin.facade.dedup.register_file(file_path, asset)
            return self._ok(await self._serialize_asset(asset, include_path=True))
        except Exception as exc:
            logger.error(f"{PLUGIN_NAME}: WebUI update failed: {exc}", exc_info=True)
            return self._error(str(exc))

    async def delete_sticker(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        asset_id = self._single_line(payload.get("asset_id"), 120)
        if not asset_id:
            return self._error("缺少 asset_id")
        if not bool(payload.get("confirm")):
            return self._error("删除贴纸需要 confirm=true")
        try:
            result = await self.plugin.facade.delete_asset(asset_id)
            if not result.ok:
                return self._error(result.message)
            return self._ok({"deleted": asset_id, "message": result.message})
        except Exception as exc:
            logger.error(f"{PLUGIN_NAME}: WebUI delete failed: {exc}", exc_info=True)
            return self._error(str(exc))

    async def bulk_delete_stickers(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        raw_ids = payload.get("asset_ids")
        asset_ids = [
            self._single_line(item, 120)
            for item in (raw_ids if isinstance(raw_ids, list) else [])
        ][:100]
        asset_ids = [item for item in asset_ids if item]
        if not asset_ids:
            return self._error("没有选择要删除的贴纸")
        if not bool(payload.get("confirm")):
            return self._error("批量删除贴纸需要 confirm=true")
        deleted: list[str] = []
        errors: list[str] = []
        for asset_id in asset_ids:
            result = await self.plugin.facade.delete_asset(asset_id)
            if result.ok:
                deleted.append(asset_id)
            else:
                errors.append(f"{asset_id}: {result.message}")
        return self._ok({"deleted": deleted, "errors": errors})

    async def prune_missing_assets(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        if not bool(payload.get("confirm")):
            return self._error("清理失效记录需要 confirm=true")
        stale_asset_ids = await self.plugin.facade.storage.prune_missing_assets()
        for asset_id in stale_asset_ids:
            asset = StickerAsset(asset_id=asset_id, group_name="", storage_key="", original_name="")
            await self.plugin.facade.dedup.unregister_asset(asset)
        return self._ok({"removed": stale_asset_ids})

    async def get_duplicate_candidates(self) -> dict[str, Any]:
        limit = self._query_int("limit", 30, 1, 100)
        pairs = await self._duplicate_pairs(limit=limit)
        return self._ok({"items": pairs, "total": len(pairs)})

    async def rebuild_dhash_index(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        if not bool(payload.get("confirm")):
            return self._error("重建 dHash 索引需要 confirm=true")
        indexed = await self.plugin.facade.dedup.rebuild_index()
        return self._ok({"indexed": indexed})

    async def get_config(self) -> dict[str, Any]:
        try:
            config = dict(self.plugin.config)
            providers = []
            try:
                provider_insts = self.plugin.context.get_all_providers()
                for prov in provider_insts:
                    prov_id = getattr(prov, "provider_id", "") or getattr(prov, "id", "")
                    prov_name = getattr(prov, "provider_name", "") or getattr(prov, "name", "") or prov_id
                    if prov_id:
                        providers.append({"id": str(prov_id), "name": str(prov_name)})
            except Exception as exc:
                logger.warning(f"{PLUGIN_NAME}: 获取 provider 列表失败: {exc}")
            return self._ok({"config": config, "providers": providers})
        except Exception as exc:
            logger.error(f"{PLUGIN_NAME}: WebUI 获取配置失败: {exc}", exc_info=True)
            return self._error(str(exc))

    async def update_config(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        schema = {
            "tag_provider_id": ("string", 160),
            "review_system_prompt": ("string", 4096),
            "max_stickers": ("int", 0, None),
            "max_stickers_per_message": ("int", 0, 10),
            "max_prompt_tags": ("int", 0, 100),
            "enable_auto_steal": ("bool",),
            "cleanup_interval_hours": ("int", 1, None),
            "cleanup_count": ("int", 1, None),
            "enable_auto_cleanup": ("bool",),
            "min_stickers_to_keep": ("int", 0, None),
            "steal_all_images": ("bool",),
            "only_store_emojis": ("bool",),
        }
        updates: dict[str, Any] = {}
        for key, rules in schema.items():
            if key not in payload:
                continue
            value = payload[key]
            kind = rules[0]
            try:
                if kind == "bool":
                    if not isinstance(value, bool):
                        return self._error(f"字段 {key} 必须是布尔值")
                    updates[key] = value
                elif kind == "int":
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        return self._error(f"字段 {key} 必须是整数")
                    int_value = int(value)
                    if len(rules) > 1 and rules[1] is not None and int_value < rules[1]:
                        return self._error(f"字段 {key} 不能小于 {rules[1]}")
                    if len(rules) > 2 and rules[2] is not None and int_value > rules[2]:
                        return self._error(f"字段 {key} 不能大于 {rules[2]}")
                    updates[key] = int_value
                elif kind == "string":
                    if not isinstance(value, str):
                        return self._error(f"字段 {key} 必须是字符串")
                    max_len = rules[1] if len(rules) > 1 else 4096
                    if len(value) > max_len:
                        return self._error(f"字段 {key} 长度不能超过 {max_len}")
                    updates[key] = value
            except (TypeError, ValueError) as exc:
                return self._error(f"字段 {key} 格式错误: {exc}")
        if not updates:
            return self._error("没有需要更新的配置字段")
        try:
            config = self.plugin.config
            for key, value in updates.items():
                config[key] = value
            saved = False
            save = getattr(config, "save_config", None)
            if callable(save):
                save()
                saved = True
            self.plugin.facade.set_plugin_config(dict(config))
            return self._ok({"config": dict(config), "saved": saved, "updated": updates})
        except Exception as exc:
            logger.error(f"{PLUGIN_NAME}: WebUI 更新配置失败: {exc}", exc_info=True)
            return self._error(str(exc))

    async def _serialize_asset(
        self,
        asset: StickerAsset | None,
        *,
        include_path: bool,
    ) -> dict[str, Any]:
        if asset is None:
            return {}
        resolved = await self.plugin.facade.storage.resolve_path(asset.storage_key)
        exists = resolved.exists() and resolved.is_file()
        payload = {
            "asset_id": asset.asset_id,
            "group_name": asset.group_name,
            "storage_key": asset.storage_key,
            "original_name": asset.original_name,
            "mime_hint": asset.mime_hint,
            "description": asset.description,
            "source": asset.source,
            "created_at": int(asset.created_at or 0),
            "created_label": time.strftime(
                "%Y-%m-%d %H:%M",
                time.localtime(asset.created_at or time.time()),
            ),
            "usage_count": int(asset.usage_count or 0),
            "last_used_at": int(asset.last_used_at or 0),
            "labels": list(asset.labels or ()),
            "exists": exists,
            "image_endpoint": f"/sticker/image_data?asset_id={quote(asset.asset_id, safe='')}",
        }
        if include_path:
            payload["file_path"] = asset.storage_key
        return payload

    async def _resolve_asset_image_path(self) -> Path | dict[str, str]:
        asset_id = self._query("asset_id", 120)
        if not asset_id:
            return {"error": "缺少 asset_id"}
        asset = await self.plugin.facade.storage.get_asset(asset_id)
        if asset is None:
            return {"error": "没有找到这个贴纸"}
        path = await self.plugin.facade.storage.resolve_path(asset.storage_key)
        stickers_root = self.plugin.paths.stickers_dir.resolve()
        try:
            path.resolve().relative_to(stickers_root)
        except ValueError:
            return {"error": "图片路径超出贴纸目录"}
        if not path.exists() or not path.is_file():
            return {"error": "图片文件不存在"}
        return path

    async def _duplicate_pairs(self, *, limit: int) -> list[dict[str, Any]]:
        dedup = self.plugin.facade.dedup
        items = await dedup.list_index_entries()
        pairs: list[dict[str, Any]] = []
        for left_index, left in enumerate(items):
            for right in items[left_index + 1 :]:
                distance = dedup.hamming_distance(left.dhash, right.dhash)
                if distance > dedup.threshold:
                    continue
                left_asset = await self.plugin.facade.storage.get_asset(left.asset_id)
                right_asset = await self.plugin.facade.storage.get_asset(right.asset_id)
                if left_asset is None or right_asset is None:
                    continue
                pairs.append(
                    {
                        "distance": distance,
                        "left": await self._serialize_asset(left_asset, include_path=False),
                        "right": await self._serialize_asset(
                            right_asset, include_path=False
                        ),
                    }
                )
                if len(pairs) >= limit:
                    return pairs
        return pairs

    @staticmethod
    async def _read_file_base64(path: Path) -> str:
        raw = await asyncio.to_thread(path.read_bytes)
        return base64.b64encode(raw).decode("ascii")

    @staticmethod
    def _ranked_counts(counts: dict[str, int]) -> list[dict[str, Any]]:
        return [
            {"name": name, "count": count}
            for name, count in sorted(
                counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]

    @staticmethod
    def _asset_sort_key(sort_by: str):
        if sort_by == "usage_count":
            return lambda asset: (int(asset.usage_count or 0), asset.created_at or 0, asset.asset_id)
        if sort_by == "last_used_at":
            return lambda asset: (asset.last_used_at or 0, asset.created_at or 0, asset.asset_id)
        if sort_by == "original_name":
            return lambda asset: ((asset.original_name or "").casefold(), asset.created_at or 0, asset.asset_id)
        if sort_by == "group_name":
            return lambda asset: ((asset.group_name or "").casefold(), asset.created_at or 0, asset.asset_id)
        if sort_by == "asset_id":
            return lambda asset: ((asset.asset_id or "").casefold(), asset.created_at or 0)
        return lambda asset: (asset.created_at or 0, asset.asset_id)

    @staticmethod
    def _normalize_labels(value: Any, *, fallback: str) -> tuple[str, ...]:
        if isinstance(value, str):
            raw_items = value.replace("，", ",").replace("、", ",").split(",")
        elif isinstance(value, list):
            raw_items = value
        else:
            raw_items = []
        labels: list[str] = []
        seen: set[str] = set()
        for raw in raw_items:
            label = normalize_tag_display_name(str(raw or "").strip())
            if not label or label in seen:
                continue
            seen.add(label)
            labels.append(label)
            if len(labels) >= 12:
                break
        return tuple(labels or (normalize_tag_display_name(fallback),))

    @staticmethod
    def _single_line(value: Any, limit: int) -> str:
        return " ".join(str(value or "").strip().split())[:limit]

    @staticmethod
    def _query(name: str, limit: int) -> str:
        return MoodPageApi._single_line(request.args.get(name, ""), limit)

    @staticmethod
    def _query_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(request.args.get(name, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    @staticmethod
    def _ok(data: Any = None) -> dict[str, Any]:
        return {"success": True, "data": data, "ts": int(time.time())}

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        return {"success": False, "error": str(message), "ts": int(time.time())}
