<div align="center">
  <h1>AstrBot Plugin Mood of the Moment</h1>
  <i>—— 此刻的心情 v2 ——</i>
</div>

<p align="center">
  <strong>meme_def 精确选图、tag 分组兜底的表情包智能管理与发送插件</strong>
</p>

---

## v2 核心概念

- **`meme_def`**：单张图片的全局唯一名称，一对一精确选图，例如 `:真冬_低头:`。
- **`tags`**：多张图片共享的分组标签，同时承担旧的兜底匹配，例如 `:二次元:`、`:二次元:疲惫:`。
- v2 为不兼容重构：`group_name`、`original_name`、`labels` 等旧字段已全部移除，正常运行不读取旧数据库。

1. **精确选图**：大模型输出单个 `:meme_def:` 时，插件直接发送唯一对应的图片。
2. **tag 分组兜底**：输出单个或组合 `:tag:` 时，沿用旧的组合降级与评分逻辑选图。
3. **启发式自动偷图**：聊天中出现图片时，异步调用多模态模型识图，生成 `filename`（规范化为 `meme_def`）、描述和 tags 后入库。

## ✨ 快速开始

启用插件并配置至少一个可用的视觉理解 LLM 供应商后，即可在聊天中测试。
在人格提示词里加一行类似提醒：“每次发言之前可以发一张能够表达自己心情的表情包。”

## 🧩 核心机制

大模型发图不需要复杂的 Function Calling，只需在文本中内嵌 **冒号标签**，插件在渲染链路拦截并替换为 `Image` 组件。

**每轮 Prompt 注入（动态、稳定排序、不随机）：**
- 精确表情定义：当前库内全部 `meme_def`（受 `max_prompt_meme_defs` 限制）
- 分组标签：当前库内全部 `tags`（受 `max_prompt_tags` 限制）
- 使用规则提示

**发送解析顺序：**
1. marker 单 token 且命中 `meme_def` → 精确发送唯一图片；
2. 未命中 `meme_def` → 解释为 tag，单 tag 或组合 tag 走旧的降级评分；
3. 同分时按 `meme_def` 字典序稳定决胜（不引入随机）；
4. 完全未命中 → 静默删除 marker。

## 🔧 LLM 工具

- `mood_check_memes_def(meme_def)` —— 按 `meme_def` 精确查询一张图的完整描述、tags 和发送 marker。
- `mood_rough_search_memes(query, limit=8)` —— 在 `meme_def`、描述、tags、来源中模糊搜索候选。
- `mood_steal_memes(image_path, meme_def, tags, description)` —— 手动导入一张图片。

旧工具名（`steal_memes`、`check_memes`、`rough_search`、`mood_of_the_moment_*`）已移除。

## 📦 自动偷图与清理机制

- **咕嘎严选**：默认识别“表情类型图片”触发审查，读取 `emoji_id`、`emoji_package_id`、`key`、`sub_type/subType`、`summary`、`type` 等特征识别 QQ 商城表情；`steal_all_images` 审查所有图片；`only_store_emojis` 只处理商城表情。
- **视觉识图协议**：模型只输出一次 `filename`（规范化为 `meme_def`）、`description`、`tags`。`meme_def` 冲突自动追加 `_2`；不得与任何 tag 同名；缺字段拒绝入库。
- **自动清理**：达到存储上限后周期性删除使用次数最少的表情包。

## 🗂️ 旧库格式化（WebUI 一次性工具）

v2 正常运行不读取旧数据库。WebUI 提供一次性“格式化旧库”流程，使用视觉模型重建新库：

1. **预扫描与视觉分析**：只读旧 `stickers.sqlite3`，逐张调用视觉模型重新生成 `meme_def`、描述、tags，写入 staging manifest（不动正式新库），输出成功/失败/冲突报告。旧 tags 仅作提示，不直接沿用。
2. **确认提交**：仅写入识图成功项；复制重命名图片、重建 dHash 索引、原子切换到新库；随后删除旧数据库、旧图片目录和 staging 文件。**失败项将被永久删除**，确认页会明确提示。提交失败则旧库保持不变。

前提：新库必须为空；同一时间仅一个格式化任务；格式化期间暂停自动采集、手动导入和自动清理。

## ⚠️ 平台兼容性说明

- NapCat / aiocqhttp 场景下，QQ 官方表情通常以带扩展字段的图片段上报，插件可据此识别并自动审查。
- `qq_official` 适配器下的“超大表情”等特殊消息，AstrBot 通常会解析成纯文本占位而非 `Image` 组件，插件拿不到真实图片，无法自动偷图或入库。这属于上游适配层限制。

## 🛠️ 管理指令与配置

- `mood_check [数量]` —— 展示当前会话最近触发的表情包资产记录（含 asset_id、meme_def、tags）。
- `mood_delete <asset_id>` —— 按 ID 彻底删除图片资产。

配置面板关键字段：

```yaml
meme_review_provider_id: ""      # 用于识图审查与命名的 LLM Provider，留空用默认
enable_auto_steal: true          # 是否启用自动偷取表情包
steal_all_images: false          # true 审查所有图片；false 仅审查表情类型图片
only_store_emojis: false         # true 只审查商城表情，优先级高于 steal_all_images
enable_auto_cleanup: true        # 开启自动清理
max_stickers_per_message: 1      # 每条消息最多替换出几张表情包
max_prompt_meme_defs: 30         # 每轮注入的 meme_def 数量上限
max_prompt_tags: 30              # 每轮注入的 tag 数量上限
```
