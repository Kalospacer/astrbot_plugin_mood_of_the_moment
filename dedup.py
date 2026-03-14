from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from astrbot.api import logger

from .models import PluginPaths, StickerAsset


@dataclass(slots=True)
class IndexedHash:
    asset_id: str
    storage_key: str
    dhash: str


class DHashDedupService:
    def __init__(self, storage, paths: PluginPaths, threshold: int = 8):
        self.storage = storage
        self.paths = paths
        self.threshold = threshold
        self.index_path = self.paths.data_dir / "image_dhash_index.json"
        self.index: dict[str, IndexedHash] = {}

    def initialize(self) -> None:
        self.index = self._load_index()
        self._rebuild_missing_entries()

    def compute_dhash(self, image_path: Path) -> str:
        try:
            with Image.open(image_path) as image:
                image = image.convert("L")
                image = image.resize((9, 8), Image.Resampling.LANCZOS)
                pixels = list(image.getdata())
            diff = []
            width, height = 9, 8
            for row in range(height):
                for col in range(width - 1):
                    pixel_left_idx = row * width + col
                    pixel_right_idx = pixel_left_idx + 1
                    diff.append(pixels[pixel_left_idx] > pixels[pixel_right_idx])
            decimal_value = 0
            for index, value in enumerate(diff):
                if value:
                    decimal_value += 1 << index
            return hex(decimal_value)[2:]
        except (OSError, UnidentifiedImageError) as exc:
            logger.warning(f"此刻的心情: dHash 计算失败: {exc}")
            return ""

    def hamming_distance(self, left: str, right: str) -> int:
        max_len = max(len(left), len(right))
        left_bits = bin(int(left, 16))[2:].zfill(max_len * 4)
        right_bits = bin(int(right, 16))[2:].zfill(max_len * 4)
        return sum(a != b for a, b in zip(left_bits, right_bits))

    def find_similar_duplicate(self, source_path: Path) -> StickerAsset | None:
        candidate_hash = self.compute_dhash(source_path)
        if not candidate_hash:
            return None
        for item in list(self.index.values()):
            existing_path = self.storage.resolve_path(item.storage_key)
            if not existing_path.exists():
                continue
            distance = self.hamming_distance(candidate_hash, item.dhash)
            if distance <= self.threshold:
                return self.storage.get_asset(item.asset_id)
        return None

    def register_file(self, file_path: Path, asset: StickerAsset | None = None) -> None:
        if asset is None:
            try:
                relative_key = str(file_path.resolve().relative_to(self.paths.stickers_dir.resolve())).replace('\\', '/')
            except ValueError:
                relative_key = str(file_path)
            asset = self.storage.get_asset_by_storage_key(relative_key)
        if asset is None:
            return
        image_hash = self.compute_dhash(file_path)
        if not image_hash:
            return
        self.index[asset.asset_id] = IndexedHash(asset_id=asset.asset_id, storage_key=asset.storage_key, dhash=image_hash)
        self._persist_index()

    def unregister_asset(self, asset: StickerAsset) -> None:
        if self.index.pop(asset.asset_id, None) is not None:
            self._persist_index()

    def _load_index(self) -> dict[str, IndexedHash]:
        if not self.index_path.exists():
            return {}
        try:
            raw_data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if not isinstance(raw_data, dict):
                return {}
            result: dict[str, IndexedHash] = {}
            for asset_id, payload in raw_data.items():
                if not isinstance(asset_id, str) or not isinstance(payload, dict):
                    continue
                storage_key = str(payload.get("storage_key") or "").strip()
                dhash = str(payload.get("dhash") or "").strip()
                if storage_key and dhash:
                    result[asset_id] = IndexedHash(asset_id=asset_id, storage_key=storage_key, dhash=dhash)
            return result
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning(f"此刻的心情: 读取 dHash 索引失败，将重建: {exc}")
            return {}

    def _rebuild_missing_entries(self) -> None:
        changed = False
        assets = self.storage.query_assets()
        existing_assets = {asset.asset_id: asset for asset in assets}
        for asset_id in list(self.index):
            if asset_id not in existing_assets:
                self.index.pop(asset_id, None)
                changed = True
        for asset in assets:
            if asset.asset_id in self.index:
                continue
            file_path = self.storage.resolve_path(asset.storage_key)
            image_hash = self.compute_dhash(file_path)
            if image_hash:
                self.index[asset.asset_id] = IndexedHash(asset_id=asset.asset_id, storage_key=asset.storage_key, dhash=image_hash)
                changed = True
        if changed:
            self._persist_index()

    def _persist_index(self) -> None:
        try:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            raw_data = {
                asset_id: {"storage_key": item.storage_key, "dhash": item.dhash}
                for asset_id, item in self.index.items()
            }
            self.index_path.write_text(json.dumps(raw_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            logger.warning(f"此刻的心情: 持久化 dHash 索引失败: {exc}")
