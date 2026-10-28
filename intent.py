"""Conservative, message-text-only consent for document translation.

Attachments, filenames, quoted content and model tool arguments are not consent.
Unknown/ambiguous wording must use the explicit command instead of guessing.
"""

import re

_LANGUAGES = {
    "简体中文": "zh",
    "繁体中文": "zh-TW",
    "中文": "zh",
    "汉语": "zh",
    "英语": "en",
    "英文": "en",
    "日语": "ja",
    "日文": "ja",
    "韩语": "ko",
    "韩文": "ko",
    "法语": "fr",
    "德语": "de",
    "俄语": "ru",
    "西班牙语": "es",
    "意大利语": "it",
    "葡萄牙语": "pt",
    "阿拉伯语": "ar",
}
_REFUSAL = re.compile(
    r"(?:不要|不用|无需|不必|不想|不需要|暂不|暂时不|先不|先别|别|不|取消|停止)"
    r"[^，,。；;！？!?\n]{0,20}?(?:翻译|译成|译为|开始|启动|处理|执行|安装|下载|发送|上传|运行)"
    r"|(?:算了|不用了|暂缓|取消了?|先等等)",
    re.I,
)
_OBJECT = (
    r"(?:(?:这个|这份|这张|那个|那份|那张|"
    r"(?:刚才|刚刚|上面|之前)(?:发的|发送的|的|那份|那张|那个)?)?"
    r"(?:pdf|附件|文件|文档|图片)|这个|那个|它)"
)
_REQUEST = re.compile(
    r"^(?:(?:请你?|麻烦你?|劳驾)\s*)?"
    r"(?:(?:帮我|给我|帮忙)\s*)?"
    r"(?:把\s*" + _OBJECT + r"\s*)?"
    r"(?P<verb>翻译|译成|译为)(?P<tail>.*)$",
    re.I,
)
_TARGET = "(?:" + "|".join(map(re.escape, _LANGUAGES)) + r"|[A-Za-z]{2,3}(?:[-_][A-Za-z]{2,8})?)"
_TAIL = re.compile(
    r"^(?:一下|下)?(?:" + _OBJECT + r")?(?:一下|下)?"
    r"(?:(?:成|为)(?P<target>" + _TARGET + r"))?(?:吧|谢谢你?)?$",
    re.I,
)


def refuses_translation(text: str) -> bool:
    return bool(_REFUSAL.search(text[:2000]))


def requested_language(text: str) -> str | None:
    """None means no clear request; empty means the configured target language."""
    text = text.strip()
    if not text or len(text) > 2000 or refuses_translation(text):
        return None
    if re.fullmatch(r"/?沉浸翻译(?:\s+(?:翻译|开始))?", text):
        return ""
    # Questions/discussion are not consent, even when a recent PDF exists.
    if re.search(
        r"(?:怎么|如何|教程|原理|功能|设置|配置|工具|插件|软件|是什么|什么意思|"
        r"这句话|这段话|这段文字|单词|句子|是否|要不要|需不需要|"
        r"收费|付费|花费|费用|免费|支持|吗|么|嘛|[？?])",
        text,
    ):
        return None
    match = _REQUEST.fullmatch(text)
    if match is None:
        return None
    tail = match["tail"].strip().strip("。！! ")
    if match["verb"] in {"译成", "译为"}:
        tail = match["verb"][-1] + tail
    # Validate the WHOLE tail. Recognizing a verb prefix cannot authorize an
    # arbitrary suffix such as '仅讨论可行性' or a later withdrawal of consent.
    tail_match = _TAIL.fullmatch(re.sub(r"\s+", "", tail))
    if tail_match is None:
        return None
    label = tail_match["target"]
    return _LANGUAGES.get(label, label) if label else ""
