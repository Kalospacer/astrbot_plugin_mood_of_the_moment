"""此刻的心情 v2 测试套件。

运行方式（仓库根目录）：
    python -m pytest astrbot_plugin_mood_of_the_moment/tests/test_v2.py -v
或直接：
    python astrbot_plugin_mood_of_the_moment/tests/test_v2.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ---- stub astrbot.api，使插件模块可在测试环境导入 ----
PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _install_astrbot_stub() -> None:
    logger = MagicMock(name="logger")

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logger

    class FunctionTool:  # noqa: D401 - 测试桩
        pass

    api.FunctionTool = FunctionTool

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = object

    mc_mod = types.ModuleType("astrbot.api.message_components")

    class Image:
        @staticmethod
        def fromFileSystem(path):
            return ("image", path)

    class Plain:
        def __init__(self, text):
            self.text = text

    mc_mod.Image = Image
    mc_mod.Plain = Plain

    sys.modules.setdefault("astrbot", astrbot)
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = mc_mod


_install_astrbot_stub()

# 绕过包 __init__（其会导入 main.py 并依赖完整 AstrBot 运行时），
# 手工注册包对象后直接按全名加载子模块。
import importlib

_pkg_name = "astrbot_plugin_mood_of_the_moment"
_pkg = types.ModuleType(_pkg_name)
_pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules[_pkg_name] = _pkg

facade_mod = importlib.import_module(f"{_pkg_name}.facade")
formatter_mod = importlib.import_module(f"{_pkg_name}.legacy_formatter")
models_mod = importlib.import_module(f"{_pkg_name}.models")
render_mod = importlib.import_module(f"{_pkg_name}.render")
review_mod = importlib.import_module(f"{_pkg_name}.review")
storage_mod = importlib.import_module(f"{_pkg_name}.storage")
utils_mod = importlib.import_module(f"{_pkg_name}.utils")
constants_mod = importlib.import_module(f"{_pkg_name}.constants")

PluginPaths = models_mod.PluginPaths
StickerAssetDraft = models_mod.StickerAssetDraft
StickerRenderer = render_mod.StickerRenderer
ReviewService = review_mod.ReviewService
StickerStorage = storage_mod.StickerStorage
normalize_meme_def = utils_mod.normalize_meme_def


def _make_png(path: Path, color=(120, 40, 200)) -> None:
    from PIL import Image as PILImage

    path.parent.mkdir(parents=True, exist_ok=True)
    PILImage.new("RGB", (16, 16), color).save(path)


def _make_paths(root: Path) -> PluginPaths:
    return PluginPaths(
        plugin_dir=root,
        data_dir=root / "data",
        stickers_dir=root / "data" / "meme_defs",
        metadata_db=root / "data" / "meme_defs.sqlite3",
    )


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mood_v2_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def run(coro):
    return asyncio.run(coro)


class TestNormalize(TempDirCase):
    def test_filename_to_meme_def_normalization(self):
        self.assertEqual(normalize_meme_def("真冬_低头"), "真冬_低头")
        self.assertEqual(normalize_meme_def(" 猫猫 捂脸.png"), "猫猫_捂脸")
        self.assertEqual(normalize_meme_def("a/b\\c:d"), "cd")
        self.assertEqual(normalize_meme_def("miko--smile"), "miko_smile")
        self.assertEqual(normalize_meme_def(":::"), "")


class TestStorage(TempDirCase):
    def _storage(self) -> StickerStorage:
        paths = _make_paths(self.tmp)
        storage = StickerStorage(paths)
        run(storage.initialize())
        return storage

    def test_schema_has_no_legacy_columns(self):
        storage = self._storage()
        with sqlite3.connect(str(storage.paths.metadata_db)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(sticker_assets)")}
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("meme_def", columns)
        self.assertNotIn("group_name", columns)
        self.assertNotIn("labels_json", columns)
        self.assertNotIn("original_name", columns)
        self.assertNotIn("sticker_groups", tables)

    def test_add_and_get_by_meme_def(self):
        storage = self._storage()
        asset = run(storage.add_asset(StickerAssetDraft(
            meme_def="真冬_低头", storage_key="a.png",
            description="低头疲惫", tags=("二次元", "疲惫"),
        )))
        found = run(storage.get_asset_by_meme_def("真冬_低头"))
        self.assertIsNotNone(found)
        self.assertEqual(found.asset_id, asset.asset_id)
        self.assertIsNone(run(storage.get_asset_by_meme_def("不存在")))

    def test_meme_def_unique(self):
        storage = self._storage()
        run(storage.add_asset(StickerAssetDraft(
            meme_def="猫", storage_key="a.png", description="d", tags=("t",),
        )))
        with self.assertRaises(sqlite3.IntegrityError):
            run(storage.add_asset(StickerAssetDraft(
                meme_def="猫", storage_key="b.png", description="d", tags=("t",),
            )))

    def test_required_fields(self):
        storage = self._storage()
        with self.assertRaises(ValueError):
            run(storage.add_asset(StickerAssetDraft(
                meme_def="", storage_key="a.png", description="d", tags=("t",),
            )))
        with self.assertRaises(ValueError):
            run(storage.add_asset(StickerAssetDraft(
                meme_def="x", storage_key="a.png", description="", tags=("t",),
            )))
        with self.assertRaises(ValueError):
            run(storage.add_asset(StickerAssetDraft(
                meme_def="x", storage_key="a.png", description="d", tags=(),
            )))

    def test_query_by_tags_match_all(self):
        storage = self._storage()
        run(storage.add_asset(StickerAssetDraft(
            meme_def="a", storage_key="a.png", description="d", tags=("二次元", "疲惫"),
        )))
        run(storage.add_asset(StickerAssetDraft(
            meme_def="b", storage_key="b.png", description="d", tags=("二次元",),
        )))
        both = run(storage.query_assets(tags=("二次元", "疲惫"), match_all=True))
        self.assertEqual([a.meme_def for a in both], ["a"])
        any_tag = run(storage.query_assets(tags=("二次元", "疲惫"), match_all=False))
        self.assertEqual(len(any_tag), 2)


class _FakeFacadeStorage:
    """为 renderer 测试提供最小 storage 接口。"""

    def __init__(self, memes: list[dict]):
        self._memes = memes
        self.usage_calls: list[tuple[str, str]] = []

    async def get_meme_by_def(self, meme_def):
        for m in self._memes:
            if m["meme_def"] == meme_def:
                return m
        return None

    async def get_memes_by_tags(self, tags, match_all=True):
        wanted = {t.casefold() for t in tags}
        result = []
        for m in self._memes:
            have = {t.casefold() for t in m["tags"]}
            if (match_all and wanted <= have) or (not match_all and wanted & have):
                result.append(m)
        return result

    async def get_all_meme_defs(self, limit=None):
        defs = sorted({m["meme_def"] for m in self._memes}, key=str.casefold)
        return defs[:limit] if limit else defs

    async def get_all_tags(self):
        return sorted({t for m in self._memes for t in m["tags"]}, key=str.casefold)

    async def increment_usage_count(self, asset_id, scope_key=""):
        self.usage_calls.append((asset_id, scope_key))


def _meme(meme_def, tags, asset_id=None, usage=0, last_used=None):
    return {
        "asset_id": asset_id or f"id-{meme_def}",
        "meme_def": meme_def,
        "file_path": f"/tmp/{meme_def}.png",
        "tags": list(tags),
        "description": f"desc {meme_def}",
        "usage_count": usage,
        "last_used_at": last_used,
    }


class TestRenderer(TempDirCase):
    def test_exact_meme_def_sends_single_image(self):
        storage = _FakeFacadeStorage([
            _meme("真冬_低头", ("二次元", "疲惫")),
            _meme("真冬_微笑", ("二次元",)),
        ])
        renderer = StickerRenderer(storage)
        decorated = run(renderer.decorate_text("今天不想解释了 :真冬_低头: 就这样", "s1"))
        images = [s for s in decorated.segments if s.kind == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].value, "/tmp/真冬_低头.png")
        self.assertEqual(storage.usage_calls, [("id-真冬_低头", "s1")])

    def test_single_tag_fallback_scoring(self):
        storage = _FakeFacadeStorage([
            _meme("a", ("二次元", "疲惫")),
            _meme("b", ("二次元",)),
        ])
        renderer = StickerRenderer(storage)
        decorated = run(renderer.decorate_text(":二次元:", "s"))
        images = [s for s in decorated.segments if s.kind == "image"]
        self.assertEqual(len(images), 1)

    def test_combo_tags_prefer_match_all(self):
        storage = _FakeFacadeStorage([
            _meme("a", ("二次元",)),
            _meme("b", ("二次元", "疲惫")),
        ])
        renderer = StickerRenderer(storage)
        decorated = run(renderer.decorate_text(":二次元:疲惫:", "s"))
        images = [s for s in decorated.segments if s.kind == "image"]
        self.assertEqual(len(images), 1)
        self.assertIn("b.png", images[0].value)

    def test_unknown_marker_removed_silently(self):
        storage = _FakeFacadeStorage([_meme("a", ("x",))])
        renderer = StickerRenderer(storage)
        decorated = run(renderer.decorate_text("hello :不存在的东西: world", "s"))
        text = "".join(s.value for s in decorated.segments if s.kind == "text")
        self.assertNotIn("不存在的东西", text)
        self.assertEqual([s for s in decorated.segments if s.kind == "image"], [])

    def test_stable_tiebreak_by_meme_def(self):
        storage = _FakeFacadeStorage([
            _meme("b_same", ("二次元",)),
            _meme("a_same", ("二次元",)),
        ])
        renderer = StickerRenderer(storage)
        first = run(renderer.decorate_text(":二次元:", "s"))
        second = run(renderer.decorate_text(":二次元:", "s"))
        img1 = [s.value for s in first.segments if s.kind == "image"]
        img2 = [s.value for s in second.segments if s.kind == "image"]
        self.assertEqual(img1, img2)
        self.assertIn("a_same", img1[0])

    def test_prompt_catalog_injects_defs_and_tags_stable(self):
        storage = _FakeFacadeStorage([
            _meme("b_def", ("zzz",)),
            _meme("a_def", ("猫娘", "二次元")),
        ])
        renderer = StickerRenderer(storage, max_prompt_meme_defs=30, max_prompt_tags=30)
        catalog1 = run(renderer.build_prompt_catalog())
        catalog2 = run(renderer.build_prompt_catalog())
        self.assertEqual(catalog1, catalog2)
        self.assertIn(":a_def:", catalog1)
        self.assertIn(":b_def:", catalog1)
        self.assertIn(":二次元:", catalog1)
        self.assertIn(":猫娘:", catalog1)
        self.assertIn("mood_check_memes_def", catalog1)
        self.assertIn("mood_rough_search_memes", catalog1)

    def test_prompt_catalog_limits(self):
        memes = [_meme(f"def_{i:02d}", (f"tag_{i:02d}",)) for i in range(10)]
        storage = _FakeFacadeStorage(memes)
        renderer = StickerRenderer(storage, max_prompt_meme_defs=3, max_prompt_tags=2)
        catalog = run(renderer.build_prompt_catalog())
        self.assertIn(":def_00:", catalog)
        self.assertIn(":def_02:", catalog)
        self.assertNotIn(":def_05:", catalog)
        self.assertIn(":tag_00:", catalog)
        self.assertNotIn(":tag_05:", catalog)


class TestFacadeSearchAndAlloc(TempDirCase):
    def _facade(self) -> facade_mod.PluginFacade:
        paths = _make_paths(self.tmp)
        facade = facade_mod.PluginFacade(paths=paths, context=None, plugin_config={})
        run(facade.storage.initialize())
        return facade

    def _add(self, facade, meme_def, tags, description="d", color=(1, 2, 3)):
        src = self.tmp / f"src_{meme_def}.png"
        _make_png(src, color)
        return run(facade._ingest_resolved_file(
            resolved=src, meme_def=meme_def, tags=tags, description=description,
            source="test", skip_validation=True, skip_duplicate_check=True,
        ))

    def test_allocate_meme_def_conflict_with_def_and_tag(self):
        facade = self._facade()
        r1 = self._add(facade, "猫猫", ("疲惫",))
        self.assertTrue(r1.ok)
        # 与现有 meme_def 冲突 -> 追加 _2
        r2 = self._add(facade, "猫猫", ("x",), color=(9, 9, 9))
        self.assertTrue(r2.ok)
        self.assertEqual(r2.asset.meme_def, "猫猫_2")
        # 与现有 tag 冲突 -> 追加后缀
        r3 = self._add(facade, "疲惫", ("y",), color=(7, 7, 7))
        self.assertTrue(r3.ok)
        self.assertEqual(r3.asset.meme_def, "疲惫_2")

    def test_rough_search_by_description_and_tags(self):
        facade = self._facade()
        self._add(facade, "真冬_低头", ("二次元", "疲惫"), description="角色低头表情疲惫")
        self._add(facade, "猫猫_捂脸", ("猫娘",), description="猫娘捂脸偷笑", color=(5, 5, 5))
        results = run(facade.rough_search_memes("疲惫"))
        self.assertTrue(any(r["meme_def"] == "真冬_低头" for r in results))
        results2 = run(facade.rough_search_memes("捂脸"))
        self.assertEqual(results2[0]["meme_def"], "猫猫_捂脸")
        results3 = run(facade.rough_search_memes("完全不相关"))
        self.assertEqual(results3, [])

    def test_check_meme_def_exact_only(self):
        facade = self._facade()
        self._add(facade, "真冬_低头", ("二次元",))
        self.assertIsNotNone(run(facade.check_meme_def("真冬_低头")))
        self.assertIsNone(run(facade.check_meme_def("真冬")))
        self.assertIsNone(run(facade.check_meme_def("二次元")))


class TestReview(TempDirCase):
    def test_filename_only_generates_meme_def_payload(self):
        service = ReviewService(context=None, plugin_config={})
        response = MagicMock()
        response.completion_text = json.dumps({
            "should_steal": True,
            "reason": "适合保存",
            "description": "角色低头，表情疲惫",
            "filename": "真冬_低头",
            "tags": ["二次元", "疲惫"],
        }, ensure_ascii=False)
        provider = MagicMock()
        provider.text_chat = AsyncMock(return_value=response)
        service._get_provider = lambda: provider
        result = run(service.review_image("http://x/y.png"))
        self.assertTrue(result["should_steal"])
        self.assertEqual(result["filename"], "真冬_低头")
        self.assertEqual(result["tags"], ["二次元", "疲惫"])

    def test_missing_fields_rejected(self):
        service = ReviewService(context=None, plugin_config={})
        response = MagicMock()
        response.completion_text = json.dumps({
            "should_steal": True, "description": "", "filename": "x", "tags": [],
        })
        provider = MagicMock()
        provider.text_chat = AsyncMock(return_value=response)
        service._get_provider = lambda: provider
        result = run(service.review_image("http://x/y.png"))
        self.assertFalse(result["should_steal"])

    def test_reference_context_appended(self):
        service = ReviewService(context=None, plugin_config={"review_system_prompt": "BASE"})
        response = MagicMock()
        response.completion_text = json.dumps({
            "should_steal": True, "description": "d", "filename": "f", "tags": ["t"],
        })
        provider = MagicMock()
        provider.text_chat = AsyncMock(return_value=response)
        service._get_provider = lambda: provider
        run(service.review_image("http://x", reference_context="旧 tags: 开心"))
        prompt_used = provider.text_chat.call_args.kwargs["prompt"]
        self.assertIn("BASE", prompt_used)
        self.assertIn("旧 tags: 开心", prompt_used)


class TestLegacyFormatter(TempDirCase):
    def _make_legacy_db(self, data_dir: Path, rows: list[dict]) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        stickers = data_dir / "stickers"
        stickers.mkdir(parents=True, exist_ok=True)
        db = data_dir / "stickers.sqlite3"
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                """
                CREATE TABLE sticker_assets (
                    asset_id TEXT PRIMARY KEY,
                    storage_key TEXT,
                    group_name TEXT,
                    original_name TEXT,
                    description TEXT,
                    source TEXT,
                    created_at REAL,
                    usage_count INTEGER,
                    last_used_at REAL,
                    labels_json TEXT
                )
                """
            )
            for row in rows:
                conn.execute(
                    "INSERT INTO sticker_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["asset_id"], row["storage_key"], "grp", "orig.png",
                        row.get("description", ""), row.get("source", "old"),
                        row.get("created_at", 1.0), row.get("usage_count", 0),
                        row.get("last_used_at"), row.get("labels_json", "[]"),
                    ),
                )
            conn.commit()
        for row in rows:
            if row.get("file_exists", True):
                _make_png(stickers / row["storage_key"], color=row.get("color", (3, 3, 3)))

    def _make_plugin(self) -> MagicMock:
        data_dir = self.tmp / "data"
        paths = PluginPaths(
            plugin_dir=self.tmp,
            data_dir=data_dir,
            stickers_dir=data_dir / "meme_defs",
            metadata_db=data_dir / "meme_defs.sqlite3",
        )
        storage = StickerStorage(paths)
        run(storage.initialize())
        facade = MagicMock()
        facade.storage = storage
        facade.format_busy = False
        facade.dedup = MagicMock()
        facade.dedup.rebuild_index = AsyncMock(return_value=0)
        facade.dedup.register_file = AsyncMock(return_value=None)
        plugin = MagicMock()
        plugin.paths = paths
        plugin.facade = facade
        return plugin

    def _review_ok(self, name: str):
        async def _review(image_url, reference_context=None):
            return {
                "should_steal": True,
                "reason": "ok",
                "description": f"desc {name}",
                "filename": name,
                "tags": ["新tag"],
            }
        return _review

    @staticmethod
    def _prepare_and_wait(service):
        """在同一事件循环内 prepare 并等待后台分析 task 完成。"""

        async def _go():
            await service.prepare()
            if service._task is not None:
                await service._task

        return run(_go())

    def test_prepare_does_not_touch_new_library(self):
        plugin = self._make_plugin()
        self._make_legacy_db(plugin.paths.data_dir, [
            {"asset_id": "o1", "storage_key": "a.png", "labels_json": '["旧tag"]'},
        ])
        plugin.facade.review.review_image = self._review_ok("新名字")
        service = formatter_mod.LegacyFormatService(plugin)
        self._prepare_and_wait(service)
        # 预览阶段不写入正式新库
        self.assertEqual(run(plugin.facade.storage.count_assets()), 0)
        status = service.status()
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["succeeded"], 1)
        self.assertTrue(plugin.facade.format_busy)

    def test_prepare_rejected_when_new_library_not_empty(self):
        plugin = self._make_plugin()
        run(plugin.facade.storage.add_asset(StickerAssetDraft(
            meme_def="x", storage_key="x.png", description="d", tags=("t",),
        )))
        self._make_legacy_db(plugin.paths.data_dir, [
            {"asset_id": "o1", "storage_key": "a.png"},
        ])
        service = formatter_mod.LegacyFormatService(plugin)
        with self.assertRaises(ValueError):
            run(service.prepare())

    def test_commit_writes_success_only_and_deletes_legacy(self):
        plugin = self._make_plugin()
        self._make_legacy_db(plugin.paths.data_dir, [
            {"asset_id": "o1", "storage_key": "a.png", "labels_json": '["旧"]'},
            {"asset_id": "o2", "storage_key": "missing.png", "file_exists": False},
        ])
        plugin.facade.review.review_image = self._review_ok("唯一名")
        service = formatter_mod.LegacyFormatService(plugin)
        self._prepare_and_wait(service)
        status = service.status()
        self.assertEqual(status["succeeded"], 1)
        self.assertEqual(status["failed"], 1)  # 缺失文件被列为失败

        result = run(service.commit(status["job_id"], confirm=True, discard_failed=True))
        self.assertEqual(result["status"], "committed")
        # 只写入成功项
        self.assertEqual(run(plugin.facade.storage.count_assets()), 1)
        asset = run(plugin.facade.storage.get_asset_by_meme_def("唯一名"))
        self.assertIsNotNone(asset)
        # 旧库被删除
        self.assertFalse((plugin.paths.data_dir / "stickers.sqlite3").exists())
        self.assertFalse((plugin.paths.data_dir / "stickers").exists())
        # staging 被删除
        self.assertFalse((plugin.paths.data_dir / ".meme_format_staging").exists())
        self.assertFalse(plugin.facade.format_busy)

    def test_commit_requires_confirm_and_discard(self):
        plugin = self._make_plugin()
        self._make_legacy_db(plugin.paths.data_dir, [
            {"asset_id": "o1", "storage_key": "a.png"},
        ])
        plugin.facade.review.review_image = self._review_ok("名")
        service = formatter_mod.LegacyFormatService(plugin)
        self._prepare_and_wait(service)
        job_id = service.status()["job_id"]
        with self.assertRaises(ValueError):
            run(service.commit(job_id, confirm=False, discard_failed=True))
        with self.assertRaises(ValueError):
            run(service.commit(job_id, confirm=True, discard_failed=False))
        # 旧库仍保持不变
        self.assertTrue((plugin.paths.data_dir / "stickers.sqlite3").exists())
        self.assertEqual(run(plugin.facade.storage.count_assets()), 0)

    def test_meme_def_conflict_stable_suffix_in_commit(self):
        plugin = self._make_plugin()
        self._make_legacy_db(plugin.paths.data_dir, [
            {"asset_id": "o1", "storage_key": "a.png", "color": (1, 1, 1)},
            {"asset_id": "o2", "storage_key": "b.png", "color": (2, 2, 2)},
        ])
        plugin.facade.review.review_image = self._review_ok("同名")
        service = formatter_mod.LegacyFormatService(plugin)
        self._prepare_and_wait(service)
        job_id = service.status()["job_id"]
        run(service.commit(job_id, confirm=True, discard_failed=True))
        defs = run(plugin.facade.storage.get_all_meme_defs())
        self.assertEqual(defs, ["同名", "同名_2"])

    def test_cancel_releases_busy_and_removes_staging(self):
        plugin = self._make_plugin()
        self._make_legacy_db(plugin.paths.data_dir, [
            {"asset_id": "o1", "storage_key": "a.png"},
        ])
        plugin.facade.review.review_image = self._review_ok("名")
        service = formatter_mod.LegacyFormatService(plugin)
        self._prepare_and_wait(service)
        job_id = service.status()["job_id"]
        run(service.cancel(job_id))
        self.assertFalse(plugin.facade.format_busy)
        self.assertTrue((plugin.paths.data_dir / "stickers.sqlite3").exists())
        self.assertEqual(run(plugin.facade.storage.count_assets()), 0)

    # ---------- 断点续传 ----------

    def test_resume_continues_from_staging(self):
        plugin = self._make_plugin()
        self._make_legacy_db(plugin.paths.data_dir, [
            {"asset_id": "o1", "storage_key": "a.png", "color": (1, 1, 1)},
            {"asset_id": "o2", "storage_key": "b.png", "color": (2, 2, 2)},
            {"asset_id": "o3", "storage_key": "c.png", "color": (3, 3, 3)},
        ])
        # 第一轮：识别完第 1 张后取消任务，模拟进程中断
        calls = {"n": 0}
        service_holder = {}

        async def slow_review(image_url, reference_context=None):
            calls["n"] += 1
            if calls["n"] == 2:
                # 第 2 张识别时直接抛 CancelledError，模拟进程中断
                raise asyncio.CancelledError()
            return {
                "should_steal": True, "reason": "ok",
                "description": "d", "filename": "第一张", "tags": ["t"],
            }

        plugin.facade.review.review_image = slow_review
        service = formatter_mod.LegacyFormatService(plugin)
        service_holder["svc"] = service

        async def first_round():
            await service.prepare()
            if service._task is not None:
                try:
                    await service._task
                except asyncio.CancelledError:
                    pass
        run(first_round())
        # 中断时第 1 张已完成，第 2 张识别前被取消并放回队列
        self.assertEqual(service.status()["processed"], 1)
        self.assertEqual(service.status()["status"], "preparing")

        # 构造一个新的 service（模拟重启），修复 review 后 resume
        service2 = formatter_mod.LegacyFormatService(plugin)
        plugin.facade.review.review_image = self._review_ok("后续")

        async def do_resume():
            await service2.resume()
            if service2._task is not None:
                await service2._task
        run(do_resume())
        status = service2.status()
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["processed"], 3)
        self.assertEqual(status["succeeded"], 3)
        # meme_def 不重复
        defs = {item["meme_def"] for item in status["items"]}
        self.assertEqual(len(defs), 3)

    def test_prepare_auto_resumes_existing_staging(self):
        plugin = self._make_plugin()
        self._make_legacy_db(plugin.paths.data_dir, [
            {"asset_id": "o1", "storage_key": "a.png"},
        ])
        plugin.facade.review.review_image = self._review_ok("名")
        service = formatter_mod.LegacyFormatService(plugin)
        self._prepare_and_wait(service)
        job_id = service.status()["job_id"]
        # 新 service 直接 prepare，应恢复到同一 job 而不是新建
        service2 = formatter_mod.LegacyFormatService(plugin)
        status = run(service2.prepare())
        self.assertEqual(status["job_id"], job_id)
        self.assertEqual(status["status"], "ready")

    # ---------- 部分提交 ----------

    def test_partial_commit_then_finalize(self):
        plugin = self._make_plugin()
        self._make_legacy_db(plugin.paths.data_dir, [
            {"asset_id": "o1", "storage_key": "a.png", "color": (1, 1, 1)},
            {"asset_id": "o2", "storage_key": "b.png", "color": (2, 2, 2)},
        ])
        # 第一张识别成功后，第二张识别前取消任务（模拟中断），再部分提交第一张
        calls = {"n": 0}
        service_holder = {}

        async def staged_review(image_url, reference_context=None):
            calls["n"] += 1
            if calls["n"] == 2:
                service_holder["svc"]._task.cancel()
                raise asyncio.CancelledError()
            return {"should_steal": True, "reason": "ok", "description": "d",
                    "filename": "第一张", "tags": ["t1"]}

        plugin.facade.review.review_image = staged_review
        service = formatter_mod.LegacyFormatService(plugin)
        service_holder["svc"] = service

        async def first_round():
            await service.prepare()
            if service._task is not None:
                try:
                    await service._task
                except asyncio.CancelledError:
                    pass
        run(first_round())
        self.assertEqual(service.status()["processed"], 1)
        job_id = service.status()["job_id"]

        # 部分提交第一张
        run(service.commit(job_id, confirm=True, discard_failed=True, partial=True))
        self.assertEqual(run(plugin.facade.storage.count_assets()), 1)
        self.assertIsNotNone(run(plugin.facade.storage.get_asset_by_meme_def("第一张")))
        # 旧库此时不应删除（还有未识别项）
        self.assertTrue((plugin.paths.data_dir / "stickers.sqlite3").exists())

        # 继续识别第二张并最终完成
        plugin.facade.review.review_image = self._review_ok("第二张")
        service2 = formatter_mod.LegacyFormatService(plugin)

        async def do_resume():
            await service2.resume()
            if service2._task is not None:
                await service2._task
        run(do_resume())
        self.assertEqual(service2.status()["status"], "ready")

        # 部分提交第二张 -> 无剩余 -> 收尾删除旧库
        run(service2.commit(job_id, confirm=True, discard_failed=True, partial=True))
        self.assertEqual(run(plugin.facade.storage.count_assets()), 2)
        self.assertFalse((plugin.paths.data_dir / "stickers.sqlite3").exists())
        self.assertFalse((plugin.paths.data_dir / "stickers").exists())
        self.assertFalse((plugin.paths.data_dir / ".meme_format_staging").exists())
        self.assertFalse(plugin.facade.format_busy)

    def test_partial_commit_no_success_rejected(self):
        plugin = self._make_plugin()
        self._make_legacy_db(plugin.paths.data_dir, [
            {"asset_id": "o1", "storage_key": "missing.png", "file_exists": False},
        ])
        plugin.facade.review.review_image = self._review_ok("名")
        service = formatter_mod.LegacyFormatService(plugin)
        self._prepare_and_wait(service)
        job_id = service.status()["job_id"]
        with self.assertRaises(ValueError):
            run(service.commit(job_id, confirm=True, discard_failed=True, partial=True))


class TestToolNames(TempDirCase):
    def test_constants(self):
        constants = constants_mod
        self.assertEqual(constants.STEAL_TOOL_NAME, "mood_steal_memes")
        self.assertEqual(constants.CHECK_MEMES_DEF_TOOL_NAME, "mood_check_memes_def")
        self.assertEqual(constants.ROUGH_SEARCH_MEMES_TOOL_NAME, "mood_rough_search_memes")
        self.assertEqual(constants.PLUGIN_VERSION, "2.0.0")

    def test_legacy_modules_removed(self):
        self.assertFalse((PLUGIN_DIR / "legacy_bridge.py").exists())
        self.assertFalse((PLUGIN_DIR / "default" / "memes_data.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
