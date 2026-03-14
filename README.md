# 此刻的心情

纯净重写版 AstrBot 表情包插件。

## 兼容使用方式

- `:标签:`
- `:标签1:标签2:`
- `：标签：`
- `：标签1：标签2：`

## 命令

- `smile_check`
- `smile_delete <asset_id>`

## 关键配置

- `enable_auto_steal`: 是否启用自动偷图
- `steal_all_images`: 自动偷图时是否审查所有图片，否则仅处理带 QQ 表情特征字段的图片
- `enable_auto_cleanup`: 是否按周期自动删除低使用表情包

## LLM 工具

- `steal_memes`

参数：
- `image_path`
- `category`
- `description`
- `save_name`
