SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
DEFAULT_CATEGORY = "unsorted"
DEFAULT_CATEGORY_DESCRIPTION = "未分类图片素材，等待后续整理"
PLUGIN_NAME = "此刻的心情"
PLUGIN_PACKAGE_NAME = "astrbot_plugin_mood_of_the_moment"
PLUGIN_VERSION = "1.0.0"
STEAL_TOOL_NAME = "steal_memes"
DEFAULT_REVIEW_SYSTEM_PROMPT = """你是一个表情包审查助手。请审查用户发送的图片，判断是否应该保存为表情包。\n\n审查标准：\n1. 必须是表情包、梗图、二次元表情或可爱的插画\n2. 适合 AI 助手在聊天中使用\n3. 不能是隐私照片、普通照片、截图、证件照等\n\n请返回以下 JSON 格式：\n{\n  \"should_steal\": true/false,\n  \"reason\": \"简要说明原因\",\n  \"tags\": [\"标签1\", \"标签2\", \"标签3\"]\n}\n\n标签应该描述表情的内容、情绪、角色特征等，方便后续检索使用。"""
FALLBACK_REVIEW_NEGATIVE_MARKERS = (
    "不适合",
    "不应该偷",
    "不建议偷",
    "不是表情包",
    "不是梗图",
    "不是二次元",
    'should_steal": false',
    "should_steal:false",
    '"should_steal": false',
)
FALLBACK_REVIEW_POSITIVE_MARKERS = (
    "是表情包",
    "是梗图",
    "是二次元",
    "适合作为聊天表情包",
    "适合做表情包",
    "适合作为表情包",
    "建议偷",
    "应该偷",
    'should_steal": true',
    "should_steal:true",
    '"should_steal": true',
)
