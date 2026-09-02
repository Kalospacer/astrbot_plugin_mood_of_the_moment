from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlparse


def is_remote_http_url(source: str | None) -> bool:
    """判定字符串是否为 http/https 远程地址（大小写不敏感，供多模块共用）。"""
    parsed = urlparse(str(source or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalize_meme_def(value: str | None) -> str:
    text = Path(str(value or "").strip()).stem
    text = re.sub(r"[\s\-]+", "_", text)
    text = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]", "", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:120]


def normalize_tag_display_name(tag: str | None) -> str:
    text = (tag or "").strip()
    text = re.sub(r"[\r\n:：]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text[:80]


def normalize_tags(values: object, *, max_items: int = 12) -> tuple[str, ...]:
    if isinstance(values, str):
        raw_items = values.replace("，", ",").replace("、", ",").split(",")
    elif isinstance(values, (list, tuple, set)):
        raw_items = list(values)
    else:
        raw_items = []
    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        tag = normalize_tag_display_name(str(raw or ""))
        identity = tag.casefold()
        if not tag or identity in seen:
            continue
        seen.add(identity)
        result.append(tag)
        if len(result) >= max_items:
            break
    return tuple(result)


def safe_filename(name: str | None, suffix: str) -> str:
    base = Path(str(name or "").strip()).name
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base)
    stem = Path(base).stem.strip() or f"meme_{int(time.time())}"
    ext = Path(base).suffix or suffix
    return f"{stem}{ext.lower()}"


def resolve_user_path(raw_path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(raw_path))).resolve()


def get_allowed_image_roots(
    data_dir: Path,
    extra_roots: Iterable[Path] | None = None,
) -> tuple[Path, ...]:
    roots = {data_dir.resolve(), Path.cwd().resolve()}
    if extra_roots:
        roots.update(path.resolve() for path in extra_roots)
    return tuple(sorted(roots))


def is_path_within_roots(target_path: Path, roots: Iterable[Path]) -> bool:
    resolved_target = target_path.resolve()
    for root in roots:
        resolved_root = root.resolve()
        if resolved_target == resolved_root or resolved_root in resolved_target.parents:
            return True
    return False
