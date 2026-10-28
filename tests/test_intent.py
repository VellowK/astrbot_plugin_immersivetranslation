"""Conservative document-request consent; no model or AstrBot dependencies."""

import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "_intent_tests", Path(__file__).resolve().parents[1] / "intent.py"
)
intent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(intent)


class IntentTests(unittest.TestCase):
    def test_clear_document_requests(self):
        for text, expected in (
            ("翻译", ""),
            ("翻译一下", ""),
            ("请帮我翻译一下这个文件", ""),
            ("帮我翻译这个PDF", ""),
            ("请把刚才的文件翻译一下", ""),
            ("把这份PDF翻译成中文", "zh"),
            ("帮我翻译成日语", "ja"),
            ("把它译成中文", "zh"),
            ("翻译成繁体中文", "zh-TW"),
            ("/沉浸翻译", ""),
            ("沉浸翻译 开始", ""),
            ("翻译成zh-CN", "zh-CN"),
            ("帮我翻译这个文件成日语", "ja"),
            ("翻译这个PDF成法语", "fr"),
        ):
            with self.subTest(text=text):
                self.assertEqual(intent.requested_language(text), expected)

    def test_no_request_or_ambiguous_discussion(self):
        for text in (
            "",
            "paper.pdf",
            "这是PDF",
            "帮我总结一下这个文件",
            "看看这个文档",
            "翻译插件怎么用",
            "翻译工具是什么",
            "如何翻译PDF",
            "你能翻译PDF吗？",
            "他说翻译一下",
            "文档上写着“帮我翻译一下这个文件”",
            "翻译hello",
            "把hello翻译成中文",
            "帮我把你好翻译成英语",
            "翻译这句话",
            "翻译这段文字",
            "/沉浸翻译 状态",
            "/沉浸翻译 帮助",
            "/沉浸翻译 取消",
            "翻译成不存在的语言",
            "翻译这个PDF成不存在的语言",
            "x" * 3000,
            "可以翻译这个PDF吗？",
            "翻译一下这个文件需要付费吗？",
            "翻译这个PDF，仅讨论可行性",
        ):
            with self.subTest(text=text):
                self.assertIsNone(intent.requested_language(text))

    def test_negative_requests_never_authorize_translation(self):
        for text in (
            "不翻译",
            "先别翻译",
            "这个不用翻译",
            "不要翻译，帮我总结",
            "请不要翻译这份PDF",
            "帮我翻译一下，不要翻译",
            "取消翻译",
            "翻译一下这个文件，但不要开始",
            "翻译一下这个文件，不用了",
            "帮我翻译这个文件，算了不用了",
        ):
            with self.subTest(text=text):
                self.assertTrue(intent.refuses_translation(text))
                self.assertIsNone(intent.requested_language(text))


if __name__ == "__main__":
    unittest.main()
