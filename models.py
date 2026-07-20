from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class PluginPaths:
    plugin_dir: Path
    data_dir: Path
    stickers_dir: Path
    metadata_db: Path


@dataclass(slots=True)
class StickerAsset:
    asset_id: str
    meme_def: str
    storage_key: str
    mime_hint: str = ""
    description: str = ""
    source: str = ""
    created_at: float = 0.0
    usage_count: int = 0
    last_used_at: float | None = None
    tags: tuple[str, ...] = ()


@dataclass(slots=True)
class StickerAssetDraft:
    meme_def: str
    storage_key: str
    mime_hint: str = ""
    description: str = ""
    source: str = ""
    tags: tuple[str, ...] = ()
    usage_count: int = 0
    last_used_at: float | None = None


@dataclass(slots=True)
class StickerUsageEvent:
    asset_id: str
    scope_key: str
    created_at: float


@dataclass(slots=True)
class InspectItem:
    asset_id: str
    meme_def: str
    description: str = ""
    tags: tuple[str, ...] = ()
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
    tokens: tuple[str, ...]
    start: int
    end: int
