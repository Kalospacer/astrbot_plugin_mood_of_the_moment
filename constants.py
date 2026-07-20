SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
PLUGIN_NAME = "此刻的心情"
PLUGIN_PACKAGE_NAME = "astrbot_plugin_mood_of_the_moment"
PLUGIN_VERSION = "2.0.0"

STEAL_TOOL_NAME = "mood_steal_memes"
CHECK_MEMES_DEF_TOOL_NAME = "mood_check_memes_def"
ROUGH_SEARCH_MEMES_TOOL_NAME = "mood_rough_search_memes"

DEFAULT_REVIEW_SYSTEM_PROMPT = """你是“此刻的心情”插件的表情包识图助手。请审查图片是否适合作为聊天表情包，并为通过的图片生成唯一文件名、视觉描述和分组标签。

审查标准：
1. 必须是表情包、梗图、二次元表情或适合聊天使用的插画
2. 不能是隐私照片、普通生活照、截图、证件照或无法用于聊天的图片
3. filename 是这张图片唯一的 meme_def，只输出一次
4. tags 是可以被多张图片共享的分组标签，不是唯一名称

请只返回以下 JSON：
{
  "should_steal": true,
  "reason": "是否保存的简短理由",
  "description": "描述画面、角色、动作、情绪、适用场景和不适用场景",
  "filename": "角色_动作",
  "tags": ["分组1", "分组2"]
}

filename 要求：
- 使用“角色_动作”或“主体_动作”格式
- 不要包含路径、扩展名、冒号、斜杠或其他特殊字符
- 如果无法识别角色，使用“情绪_动作”或“场景_动作”

description 必须是稳定、可供另一个模型判断是否使用该图片的视觉与使用说明。
tags 至少返回一个简短分组标签。"""

FALLBACK_REVIEW_NEGATIVE_MARKERS = (
    "不适合",
    "不应该偷",
    "不建议偷",
    "不是表情包",
    "不是梗图",
    "不是二次元",
    'should_steal": false',
    "should_steal:false",
)
FALLBACK_REVIEW_POSITIVE_MARKERS = (
    "是表情包",
    "是梗图",
    "是二次元",
    "适合作为聊天表情包",
    "适合做表情包",
    "适合作为表情包",
    'should_steal": true',
    "should_steal:true",
)
