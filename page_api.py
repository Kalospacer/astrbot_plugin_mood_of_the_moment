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
from .legacy_formatter import LegacyFormatService
from .models import StickerAsset
from .utils import normalize_meme_def, normalize_tags, safe_filename

try:
    from PIL import Image
except Exception:  # pragma: no cover - Pillow 缺失时回退原图
    Image = None

PAGE_API_PREFIX = f"/{PLUGIN_PACKAGE_NAME}/page"
# 浏览器 <img>/fetch 直连插件接口的完整前缀（宿主经 /api/v1/plugins/extensions 转发，同源带鉴权）。
BROWSER_API_PREFIX = f"/api/v1/plugins/extensions/{PLUGIN_PACKAGE_NAME}/page"
THUMBNAIL_MAX_EDGE = 256
THUMBNAIL_QUALITY = 80


class MoodPageApi:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.formatter = LegacyFormatService(plugin)

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
            ("/sticker/thumbnail", self.get_sticker_thumbnail, ["GET"], "Mood sticker thumbnail"),
            ("/sticker/image_data", self.get_sticker_image_data, ["GET"], "Mood sticker image data"),
            ("/sticker/import", self.import_sticker, ["POST"], "Mood sticker import"),
            ("/sticker/upload", self.upload_sticker, ["POST"], "Mood sticker upload"),
            ("/sticker/update", self.update_sticker, ["POST"], "Mood sticker update"),
            ("/sticker/delete", self.delete_sticker, ["POST"], "Mood sticker delete"),
            ("/sticker/bulk_delete", self.bulk_delete_stickers, ["POST"], "Mood sticker bulk delete"),
            ("/maintenance/prune_missing", self.prune_missing_assets, ["POST"], "Mood sticker prune missing assets"),
            ("/dedup/candidates", self.get_duplicate_candidates, ["GET"], "Mood sticker duplicate candidates"),
            ("/dedup/rebuild", self.rebuild_dhash_index, ["POST"], "Mood sticker rebuild dHash index"),
            ("/maintenance/format_old_library/prepare", self.prepare_old_library_format, ["POST"], "Prepare old sticker library format"),
            ("/maintenance/format_old_library/plugin_dirs", self.list_format_plugin_dirs, ["GET"], "List sibling plugin dirs for format scan"),
            ("/maintenance/format_old_library/resume", self.resume_old_library_format, ["POST"], "Resume old sticker library format"),
            ("/maintenance/format_old_library/status", self.get_old_library_format_status, ["GET"], "Old sticker library format status"),
            ("/maintenance/format_old_library/commit", self.commit_old_library_format, ["POST"], "Commit old sticker library format"),
            ("/maintenance/format_old_library/cancel", self.cancel_old_library_format, ["POST"], "Cancel old sticker library format"),
            ("/config", self.get_config, ["GET"], "Mood sticker config"),
            ("/config/update", self.update_config, ["POST"], "Mood sticker config update"),
        ]
        for path, handler, methods, desc in routes:
            register(f"{PAGE_API_PREFIX}{path}", handler, methods, desc)

    async def get_overview(self) -> dict[str, Any]:
        try:
            assets = await self.plugin.facade.storage.query_assets()
            tag_counts: dict[str, int] = {}
            missing_count = 0
            for asset in assets:
                for tag in asset.tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
                if not (await self.plugin.facade.storage.resolve_path(asset.storage_key)).exists():
                    missing_count += 1
            return self._ok(
                {
                    "total": len(assets),
                    "missing": missing_count,
                    "tags": self._ranked_counts(tag_counts),
                    "config": {
                        "max_stickers": self.plugin.config.get("max_stickers"),
                        "auto_steal": bool(self.plugin.config.get("enable_auto_steal", True)),
                        "auto_cleanup": bool(self.plugin.config.get("enable_auto_cleanup", True)),
                    },
                    "format_job": self.formatter.status(),
                }
            )
        except Exception as exc:
            logger.error(f"{PLUGIN_NAME}: WebUI overview failed: {exc}", exc_info=True)
            return self._error(str(exc))

    async def list_stickers(self) -> dict[str, Any]:
        try:
            query = self._query("q", 200).casefold()
            tag = self._query("tag", 80)
            status = self._query("status", 32)
            sort_by = self._query("sort_by", 32) or "created_at"
            sort_order = "asc" if self._query("sort_order", 16) == "asc" else "desc"
            if sort_by not in {"created_at", "last_used_at", "usage_count", "meme_def", "asset_id"}:
                sort_by = "created_at"
            page = self._query_int("page", 1, 1, 9999)
            page_size = self._query_int("page_size", 48, 12, 120)
            assets = await self.plugin.facade.storage.query_assets()
            filtered: list[StickerAsset] = []
            for asset in assets:
                if tag and tag.casefold() not in {item.casefold() for item in asset.tags}:
                    continue
                if status == "missing":
                    if (await self.plugin.facade.storage.resolve_path(asset.storage_key)).exists():
                        continue
                haystack = " ".join(
                    [asset.asset_id, asset.meme_def, asset.description, asset.source, " ".join(asset.tags)]
                ).casefold()
                if query and query not in haystack:
                    continue
                filtered.append(asset)
            filtered.sort(key=self._asset_sort_key(sort_by), reverse=sort_order == "desc")
            start = (page - 1) * page_size
            return self._ok(
                {
                    "items": [
                        await self._serialize_asset(asset, include_path=False)
                        for asset in filtered[start : start + page_size]
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
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    async def get_sticker_thumbnail(self):
        asset_id = self._query("asset_id", 120)
        asset = await self.plugin.facade.storage.get_asset(asset_id) if asset_id else None
        if asset is None:
            return self._error("没有找到这个贴纸")
        source = await self.plugin.facade.storage.resolve_path(asset.storage_key)
        if not source.exists():
            return self._error("图片文件不存在")
        thumb = await self._get_or_create_thumbnail(asset, source)
        # 生成失败时回退原图，保证可用。
        target = thumb if thumb is not None else source
        response = await send_file(target)
        response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    def _thumbnail_dir(self) -> Path:
        return self.plugin.paths.data_dir / ".thumbnails"

    async def _get_or_create_thumbnail(self, asset: StickerAsset, source: Path) -> Path | None:
        return await asyncio.to_thread(self._build_thumbnail_sync, asset, source)

    def _build_thumbnail_sync(self, asset: StickerAsset, source: Path) -> Path | None:
        if Image is None:
            return None
        thumb_dir = self._thumbnail_dir()
        thumb_path = thumb_dir / f"{asset.asset_id}.webp"
        try:
            if thumb_path.exists() and thumb_path.stat().st_mtime >= source.stat().st_mtime:
                return thumb_path
        except OSError:
            pass
        try:
            thumb_dir.mkdir(parents=True, exist_ok=True)
            with Image.open(source) as img:
                img = img.convert("RGB")
                img.thumbnail((THUMBNAIL_MAX_EDGE, THUMBNAIL_MAX_EDGE), Image.LANCZOS)
                img.save(thumb_path, "WEBP", quality=THUMBNAIL_QUALITY)
            return thumb_path
        except Exception as exc:
            logger.warning(f"{PLUGIN_NAME}: 生成缩略图失败 {asset.asset_id}: {exc}")
            return None

    def delete_thumbnail(self, asset_id: str) -> None:
        try:
            (self._thumbnail_dir() / f"{asset_id}.webp").unlink(missing_ok=True)
        except OSError:
            pass

    async def prune_orphan_thumbnails(self) -> int:
        """清理没有对应资产的孤兒缩略图，返回清理数量。"""
        return await asyncio.to_thread(self._prune_orphan_thumbnails_sync)

    def _prune_orphan_thumbnails_sync(self) -> int:
        thumb_dir = self._thumbnail_dir()
        if not thumb_dir.is_dir():
            return 0
        valid_ids = set()
        # 同步查库（仅在启动时调用一次）。
        import sqlite3 as _sqlite3
        db = self.plugin.paths.metadata_db
        if db.is_file():
            try:
                with _sqlite3.connect(str(db)) as conn:
                    valid_ids = {
                        str(row[0])
                        for row in conn.execute("SELECT asset_id FROM sticker_assets")
                    }
            except Exception:
                return 0
        removed = 0
        for path in thumb_dir.glob("*.webp"):
            if path.stem not in valid_ids:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    async def get_sticker_image_data(self) -> dict[str, Any]:
        resolved = await self._resolve_asset_image_path()
        if isinstance(resolved, dict):
            return self._error(str(resolved.get("error") or "图片不存在"))
        try:
            raw = await asyncio.to_thread(resolved.read_bytes)
            mime = mimetypes.guess_type(str(resolved))[0] or "image/png"
            return self._ok({"data_url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}", "mime": mime})
        except Exception as exc:
            logger.error(f"{PLUGIN_NAME}: WebUI image read failed: {exc}", exc_info=True)
            return self._error(str(exc))

    async def import_sticker(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        image_source = self._single_line(payload.get("image_source"), 500)
        meme_def = normalize_meme_def(payload.get("meme_def"))
        tags = self._normalize_tags(payload.get("tags"))
        description = self._text(payload.get("description"), 2000)
        if not image_source or not meme_def or not tags or not description:
            return self._error("请提供图片、meme_def、至少一个 tag 和 description")
        try:
            if image_source.startswith(("http://", "https://")):
                result = await self.plugin.facade.save_remote_image(
                    image_url=image_source,
                    meme_def=meme_def,
                    tags=tags,
                    description=description,
                    source="webui_remote",
                )
            else:
                result = await self.plugin.facade.ingest_local_file(
                    source_path=image_source,
                    meme_def=meme_def,
                    tags=tags,
                    description=description,
                    source="webui_local",
                )
            if not result.ok:
                return self._error(result.message)
            return self._ok({"message": result.message, "asset": await self._serialize_asset(result.asset, include_path=True)})
        except Exception as exc:
            logger.error(f"{PLUGIN_NAME}: WebUI import failed: {exc}", exc_info=True)
            return self._error(str(exc))

    async def upload_sticker(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        raw_data_url = str(payload.get("data_url") or "").strip()
        meme_def = normalize_meme_def(payload.get("meme_def"))
        tags = self._normalize_tags(payload.get("tags"))
        description = self._text(payload.get("description"), 2000)
        if not raw_data_url or not meme_def or not tags or not description:
            return self._error("请选择图片，并提供 meme_def、至少一个 tag 和 description")
        try:
            raw_base64 = raw_data_url.split(",", 1)[1] if raw_data_url.startswith("data:") and "," in raw_data_url else raw_data_url
            image_bytes = base64.b64decode(raw_base64, validate=True)
        except (ValueError, binascii.Error):
            return self._error("上传内容不是有效的 base64 图片")
        max_bytes = int(self.plugin.config.get("webui_upload_max_mb", 20) or 20) * 1024 * 1024
        if len(image_bytes) > max_bytes:
            return self._error(f"上传文件超过 {max_bytes // 1024 // 1024} MB")
        upload_dir = self.plugin.paths.data_dir / "webui_uploads"
        upload_name = safe_filename("upload.png", ".png")
        upload_path = upload_dir / f"{int(time.time() * 1000)}_{upload_name}"
        try:
            await asyncio.to_thread(upload_dir.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(upload_path.write_bytes, image_bytes)
            result = await self.plugin.facade.ingest_local_file(
                source_path=str(upload_path),
                meme_def=meme_def,
                tags=tags,
                description=description,
                source="webui_upload",
            )
            if not result.ok:
                return self._error(result.message)
            return self._ok({"message": result.message, "asset": await self._serialize_asset(result.asset, include_path=True)})
        except Exception as exc:
            logger.error(f"{PLUGIN_NAME}: WebUI upload failed: {exc}", exc_info=True)
            return self._error(str(exc))
        finally:
            await asyncio.to_thread(upload_path.unlink, missing_ok=True)

    async def update_sticker(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        asset_id = self._single_line(payload.get("asset_id"), 120)
        meme_def = normalize_meme_def(payload.get("meme_def"))
        tags = self._normalize_tags(payload.get("tags"))
        description = self._text(payload.get("description"), 2000)
        source = self._text(payload.get("source"), 500)
        if not asset_id or not meme_def or not tags or not description:
            return self._error("缺少 asset_id、meme_def、tags 或 description")
        if not await self._definition_available(meme_def, asset_id):
            return self._error("meme_def 已被其他 meme_def 或 tag 占用")
        try:
            asset = await self.plugin.facade.storage.update_asset_metadata(
                asset_id,
                meme_def=meme_def,
                description=description,
                source=source,
                tags=tags,
            )
            if asset is None:
                return self._error("没有找到这个贴纸")
            await self.plugin.facade.dedup.register_file(
                await self.plugin.facade.storage.resolve_path(asset.storage_key), asset
            )
            return self._ok(await self._serialize_asset(asset, include_path=True))
        except Exception as exc:
            logger.error(f"{PLUGIN_NAME}: WebUI update failed: {exc}", exc_info=True)
            return self._error(str(exc))

    async def delete_sticker(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        asset_id = self._single_line(payload.get("asset_id"), 120)
        if not asset_id or not bool(payload.get("confirm")):
            return self._error("删除贴纸需要 asset_id 和 confirm=true")
        result = await self.plugin.facade.delete_asset(asset_id)
        return self._ok({"deleted": asset_id, "message": result.message}) if result.ok else self._error(result.message)

    async def bulk_delete_stickers(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        asset_ids = [self._single_line(item, 120) for item in payload.get("asset_ids", []) if self._single_line(item, 120)][:100]
        if not asset_ids or not bool(payload.get("confirm")):
            return self._error("批量删除需要 asset_ids 和 confirm=true")
        deleted: list[str] = []
        errors: list[str] = []
        for asset_id in asset_ids:
            result = await self.plugin.facade.delete_asset(asset_id)
            (deleted if result.ok else errors).append(asset_id if result.ok else f"{asset_id}: {result.message}")
        return self._ok({"deleted": deleted, "errors": errors})

    async def prune_missing_assets(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        if not bool(payload.get("confirm")):
            return self._error("清理失效记录需要 confirm=true")
        stale = await self.plugin.facade.storage.prune_missing_assets()
        for asset_id in stale:
            await self.plugin.facade.dedup.unregister_asset(
                StickerAsset(asset_id=asset_id, meme_def="", storage_key="")
            )
        return self._ok({"removed": stale})

    async def get_duplicate_candidates(self) -> dict[str, Any]:
        limit = self._query_int("limit", 30, 1, 100)
        items = await self._duplicate_pairs(limit)
        return self._ok({"items": items, "total": len(items)})

    async def rebuild_dhash_index(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        if not bool(payload.get("confirm")):
            return self._error("重建 dHash 索引需要 confirm=true")
        return self._ok({"indexed": await self.plugin.facade.dedup.rebuild_index()})

    async def prepare_old_library_format(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        source = self._single_line(payload.get("source"), 32) or "legacy"
        plugin_dir = self._single_line(payload.get("plugin_dir"), 200) or None
        try:
            return self._ok(await self.formatter.prepare(source=source, plugin_dir_name=plugin_dir))
        except Exception as exc:
            return self._error(str(exc))

    async def list_format_plugin_dirs(self) -> dict[str, Any]:
        try:
            return self._ok({"items": self.formatter.list_sibling_plugin_dirs()})
        except Exception as exc:
            return self._error(str(exc))

    async def resume_old_library_format(self) -> dict[str, Any]:
        try:
            return self._ok(await self.formatter.resume())
        except Exception as exc:
            return self._error(str(exc))

    async def get_old_library_format_status(self) -> dict[str, Any]:
        return self._ok(self.formatter.status())

    async def commit_old_library_format(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            result = await self.formatter.commit(
                job_id=self._single_line(payload.get("job_id"), 100),
                confirm=bool(payload.get("confirm")),
                discard_failed=bool(payload.get("discard_failed")),
                partial=bool(payload.get("partial")),
            )
            return self._ok(result)
        except Exception as exc:
            return self._error(str(exc))

    async def cancel_old_library_format(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            return self._ok(await self.formatter.cancel(self._single_line(payload.get("job_id"), 100)))
        except Exception as exc:
            return self._error(str(exc))

    async def get_config(self) -> dict[str, Any]:
        raw_config = self.plugin.config
        config = dict(raw_config.data) if hasattr(raw_config, "data") and isinstance(raw_config.data, dict) else dict(raw_config) if isinstance(raw_config, dict) else {}
        providers = []
        try:
            manager = getattr(self.plugin.context, "provider_manager", None)
            for provider in getattr(manager, "providers_config", []) if manager else []:
                if isinstance(provider, dict) and provider.get("id"):
                    providers.append({"id": str(provider["id"]), "name": str(provider.get("name") or provider["id"])})
        except Exception as exc:
            logger.warning(f"{PLUGIN_NAME}: 获取 provider 列表失败: {exc}")
        return self._ok({"config": config, "providers": providers})

    async def update_config(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        schema = {
            "meme_review_provider_id": ("string", 160),
            "review_system_prompt": ("string", 8192),
            "max_stickers": ("int", 0, None),
            "max_stickers_per_message": ("int", 0, 10),
            "max_prompt_meme_defs": ("int", 0, 200),
            "max_prompt_tags": ("int", 0, 200),
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
            if rules[0] == "bool":
                if not isinstance(value, bool):
                    return self._error(f"字段 {key} 必须是布尔值")
                updates[key] = value
            elif rules[0] == "string":
                if not isinstance(value, str) or len(value) > rules[1]:
                    return self._error(f"字段 {key} 格式或长度错误")
                updates[key] = value
            else:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return self._error(f"字段 {key} 必须是整数")
                value = int(value)
                if rules[1] is not None and value < rules[1] or rules[2] is not None and value > rules[2]:
                    return self._error(f"字段 {key} 超出允许范围")
                updates[key] = value
        if not updates:
            return self._error("没有需要更新的配置字段")
        for key, value in updates.items():
            self.plugin.config[key] = value
        save = getattr(self.plugin.config, "save_config", None)
        if callable(save):
            save()
        self.plugin.facade.set_plugin_config(dict(self.plugin.config))
        return self._ok({"config": dict(self.plugin.config), "updated": updates})

    async def _serialize_asset(self, asset: StickerAsset | None, *, include_path: bool) -> dict[str, Any]:
        if asset is None:
            return {}
        resolved = await self.plugin.facade.storage.resolve_path(asset.storage_key)
        payload = {
            "asset_id": asset.asset_id,
            "meme_def": asset.meme_def,
            "storage_key": asset.storage_key,
            "mime_hint": asset.mime_hint,
            "description": asset.description,
            "source": asset.source,
            "created_at": int(asset.created_at or 0),
            "created_label": time.strftime("%Y-%m-%d %H:%M", time.localtime(asset.created_at or time.time())),
            "usage_count": int(asset.usage_count or 0),
            "last_used_at": int(asset.last_used_at or 0),
            "tags": list(asset.tags),
            "exists": resolved.exists() and resolved.is_file(),
            "image_endpoint": f"{BROWSER_API_PREFIX}/sticker/image?asset_id={quote(asset.asset_id, safe='')}",
            "thumbnail_endpoint": f"{BROWSER_API_PREFIX}/sticker/thumbnail?asset_id={quote(asset.asset_id, safe='')}",
        }
        if include_path:
            payload["file_path"] = asset.storage_key
        return payload

    async def _resolve_asset_image_path(self) -> Path | dict[str, str]:
        asset_id = self._query("asset_id", 120)
        asset = await self.plugin.facade.storage.get_asset(asset_id) if asset_id else None
        if asset is None:
            return {"error": "没有找到这个贴纸"}
        path = await self.plugin.facade.storage.resolve_path(asset.storage_key)
        try:
            path.resolve().relative_to(self.plugin.paths.stickers_dir.resolve())
        except ValueError:
            return {"error": "图片路径超出贴纸目录"}
        return path if path.exists() and path.is_file() else {"error": "图片文件不存在"}

    async def _duplicate_pairs(self, limit: int) -> list[dict[str, Any]]:
        items = await self.plugin.facade.dedup.list_index_entries()
        pairs: list[dict[str, Any]] = []
        for left_index, left in enumerate(items):
            for right in items[left_index + 1 :]:
                distance = self.plugin.facade.dedup.hamming_distance(left.dhash, right.dhash)
                if distance > self.plugin.facade.dedup.threshold:
                    continue
                left_asset = await self.plugin.facade.storage.get_asset(left.asset_id)
                right_asset = await self.plugin.facade.storage.get_asset(right.asset_id)
                if left_asset and right_asset:
                    pairs.append({"distance": distance, "left": await self._serialize_asset(left_asset, include_path=False), "right": await self._serialize_asset(right_asset, include_path=False)})
                if len(pairs) >= limit:
                    return pairs
        return pairs

    async def _definition_available(self, meme_def: str, exclude_asset_id: str) -> bool:
        normalized = meme_def.casefold()
        for asset in await self.plugin.facade.storage.query_assets():
            if asset.asset_id != exclude_asset_id and asset.meme_def.casefold() == normalized:
                return False
            if asset.asset_id != exclude_asset_id and normalized in {tag.casefold() for tag in asset.tags}:
                return False
        return True

    @staticmethod
    def _normalize_tags(value: Any) -> tuple[str, ...]:
        return normalize_tags(value)

    @staticmethod
    def _text(value: Any, limit: int) -> str:
        return str(value or "").strip()[:limit]

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
    def _asset_sort_key(sort_by: str):
        if sort_by == "usage_count":
            return lambda asset: (asset.usage_count, asset.created_at, asset.meme_def.casefold())
        if sort_by == "last_used_at":
            return lambda asset: (asset.last_used_at or 0, asset.created_at, asset.meme_def.casefold())
        if sort_by == "meme_def":
            return lambda asset: (asset.meme_def.casefold(), asset.created_at)
        if sort_by == "asset_id":
            return lambda asset: (asset.asset_id.casefold(), asset.created_at)
        return lambda asset: (asset.created_at, asset.meme_def.casefold())

    @staticmethod
    def _ranked_counts(counts: dict[str, int]) -> list[dict[str, Any]]:
        return [{"name": name, "count": count} for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))]

    @staticmethod
    def _ok(data: Any = None) -> dict[str, Any]:
        return {"success": True, "data": data, "ts": int(time.time())}

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        return {"success": False, "error": str(message), "ts": int(time.time())}
