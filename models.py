from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class PluginPaths:
    plugin_dir: Path
    data_dir: Path
    stickers_dir: Path
    metadata_db: Path
    default_dir: Path


@dataclass(slots=True)
class StickerGroup:
    name: str
    description: str = ""


@dataclass(slots=True)
class StickerAsset:
    asset_id: str
    group_name: str
    storage_key: str
    original_name: str
    mime_hint: str = ""
    description: str = ""
    source: str = ""
    created_at: float = 0.0
    usage_count: int = 0
    last_used_at: float | None = None
    labels: tuple[str, ...] = ()


@dataclass(slots=True)
class StickerAssetDraft:
    group_name: str
    storage_key: str
    original_name: str
    mime_hint: str = ""
    description: str = ""
    source: str = ""
    labels: tuple[str, ...] = ()


@dataclass(slots=True)
class StickerUsageEvent:
    asset_id: str
    scope_key: str
    created_at: float


@dataclass(slots=True)
class InspectItem:
    asset_id: str
    group_name: str
    original_name: str
    description: str = ""
    usage_count: int = 0


@dataclass(slots=True)
class DecoratedSegment:
    kind: str
    value: str


@dataclass(slots=True)
class DecoratedContent:
    segments: list[DecoratedSegment] = field(default_factory=list)


@dataclass(slots=True)
class IngestResult:
    ok: bool
    message: str
    asset: StickerAsset | None = None
    duplicate_of: str | None = None


@dataclass(slots=True)
class DeleteResult:
    ok: bool
    message: str
    asset: StickerAsset | None = None


@dataclass(slots=True)
class ParsedMarker:
    raw_text: str
    tags: tuple[str, ...]
    start: int
    end: int
