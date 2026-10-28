"""Runner contract tests: upstream modules are mocked; PDF/image I/O is real.

Run: python -m unittest discover -s tests -p test_engine_runner.py -v
No engine, model endpoint, OCR download, AstrBot import or credentials required.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

try:
    import fitz
    from PIL import Image
except ImportError as exc:
    raise unittest.SkipTest("Optional runner tests require PyMuPDF and Pillow") from exc


ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("isolated_engine_runner", ROOT / "engine_runner.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


class Model:
    allowed = None

    def __init__(self, **kwargs):
        if self.allowed is not None and not set(kwargs) <= self.allowed:
            raise AssertionError(f"Unknown upstream settings: {set(kwargs) - self.allowed}")
        self.__dict__.update(kwargs)


class Basic(Model):
    allowed = {"debug"}


class Translation(Model):
    allowed = {
        "lang_in",
        "lang_out",
        "output",
        "qps",
        "pool_max_workers",
        "term_qps",
        "term_pool_max_workers",
        "ignore_cache",
        "no_auto_extract_glossary",
    }


class PDF(Model):
    allowed = {
        "no_dual",
        "no_mono",
        "translate_table_text",
        "ocr_workaround",
        "skip_scanned_detection",
        "auto_enable_ocr_workaround",
        "watermark_output_mode",
    }


class OpenAI(Model):
    allowed = {"openai_model", "openai_base_url", "openai_api_key"}


class Settings(Model):
    allowed = {"basic", "translation", "pdf", "translate_engine_settings"}

    def validate_settings(self):
        self.validated = True


def make_pdf(
    path, text="English document for translation", pages=1, hidden=False, width=300, height=200
):
    with fitz.open() as document:
        for _ in range(pages):
            page = document.new_page(width=width, height=height)
            if text:
                page.insert_text((20, 40), text, render_mode=3 if hidden else 0)
        document.save(path)


def image_png(width=400, height=200):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


@contextmanager
def upstream(stream):
    modules = {
        name: types.ModuleType(name)
        for name in (
            "pdf2zh_next",
            "pdf2zh_next.config",
            "pdf2zh_next.config.model",
            "pdf2zh_next.config.translate_engine_model",
            "pdf2zh_next.high_level",
        )
    }
    model = modules["pdf2zh_next.config.model"]
    for key, value in (
        ("SettingsModel", Settings),
        ("BasicSettings", Basic),
        ("TranslationSettings", Translation),
        ("PDFSettings", PDF),
    ):
        setattr(model, key, value)
    modules["pdf2zh_next.config.translate_engine_model"].OpenAISettings = OpenAI
    modules["pdf2zh_next.high_level"].do_translate_async_stream = stream
    with (
        patch.dict(sys.modules, modules),
        patch.object(
            runner.metadata,
            "version",
            side_effect=lambda name: "2.9.0" if name == "pdf2zh-next" else "0.6.2",
        ),
    ):
        yield


class EngineRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.job = self.root / "job"
        self.job.mkdir()
        self.source = self.root / "source.pdf"
        make_pdf(self.source)
        self.request = {
            "source": str(self.source),
            "base_url": "http://127.0.0.1:32123/v1",
            "api_key": "temporary-test-token",
        }
        self.captured = []
        self.closed = []

    def write_request(self):
        (self.job / "request.json").write_text(json.dumps(self.request), encoding="utf-8")

    def success_stream(self):
        async def stream(settings, source):
            self.captured.append((settings, source))
            self.assertTrue(settings.validated)
            try:
                yield {
                    "type": "progress_update",
                    "overall_progress": 42,
                    "stage": "MODEL SECRET temporary-test-token",
                }
                self.assertEqual(
                    json.loads((self.job / "status.json").read_text()),
                    {"state": "translating", "progress": 42},
                )
                output = Path(settings.translation.output)
                mono, dual = output / "mono.pdf", output / "dual.pdf"
                make_pdf(mono, "Translated output")
                make_pdf(dual, "Bilingual output", pages=2)
                yield {
                    "type": "finish",
                    "translate_result": types.SimpleNamespace(
                        mono_pdf_path=mono, dual_pdf_path=dual, total_seconds=1.0
                    ),
                }
                self.fail("runner consumed past finish")
            finally:
                self.closed.append(True)

        return stream

    def invoke(self, stream=None):
        self.write_request()
        out, err = io.StringIO(), io.StringIO()
        with upstream(stream or self.success_stream()), redirect_stdout(out), redirect_stderr(err):
            code = runner.main([str(self.job)])
        text = out.getvalue()
        self.assertEqual(len(text.splitlines()), 1)
        result = json.loads(text)
        self.assertEqual(result, json.loads((self.job / "result.json").read_text(encoding="utf-8")))
        self.assertNotIn("temporary-test-token", text)
        self.assertNotIn("MODEL SECRET", text)
        self.assertFalse(list(self.job.glob(".runner-*.tmp")))
        return code, result, err.getvalue()

    def assert_error(self, expected, stream=None):
        code, result, _ = self.invoke(stream)
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], expected)
        self.assertEqual(json.loads((self.job / "status.json").read_text())["state"], "failed")
        return result

    def test_both_outputs_settings_and_original_untouched(self):
        before = self.source.read_bytes()
        code, result, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual([o["kind"] for o in result["outputs"]], ["mono", "dual"])
        self.assertEqual(result["pages"], 1)
        self.assertEqual(result["warnings"], [])
        self.assertGreaterEqual(result["seconds"], 0)
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual(self.closed, [True])
        settings, staged = self.captured[0]
        self.assertNotEqual(staged, self.source)
        self.assertTrue(staged.is_relative_to(self.job))
        self.assertEqual(staged.read_bytes(), before)
        self.assertFalse(settings.basic.debug)
        self.assertTrue(settings.translation.ignore_cache)
        self.assertTrue(settings.translation.no_auto_extract_glossary)
        self.assertEqual(settings.translation.qps, 1)
        self.assertFalse(settings.pdf.translate_table_text)
        self.assertFalse(settings.pdf.ocr_workaround)
        self.assertFalse(settings.pdf.skip_scanned_detection)
        self.assertEqual(
            settings.translate_engine_settings.openai_base_url, self.request["base_url"]
        )
        self.assertEqual(settings.translate_engine_settings.openai_api_key, self.request["api_key"])
        self.assertEqual(settings.translate_engine_settings.openai_model, "astrbot")

    def test_output_modes(self):
        for mode in ("mono", "dual"):
            with self.subTest(mode=mode):
                self.request["output_mode"] = mode
                code, result, _ = self.invoke()
                self.assertEqual(code, 0)
                self.assertEqual([o["kind"] for o in result["outputs"]], [mode])
                settings = self.captured[-1][0]
                self.assertEqual(settings.pdf.no_dual, mode == "mono")
                self.assertEqual(settings.pdf.no_mono, mode == "dual")

    def test_error_event_is_generic_and_stream_closed(self):
        async def stream(settings, source):
            try:
                print("private document on stdout temporary-test-token")
                yield {
                    "type": "error",
                    "error": "MODEL SECRET temporary-test-token",
                    "error_type": "MODEL SECRET",
                    "details": "private document",
                }
            finally:
                self.closed.append(True)

        code, result, err = self.invoke(stream)
        self.assertEqual(code, 1)
        self.assertEqual(result["error_type"], "TranslationError")
        self.assertNotIn("private document", json.dumps(result))
        self.assertIn("private document", err)
        self.assertEqual(self.closed, [True])

    def test_raised_exception_is_generic(self):
        async def stream(settings, source):
            raise RuntimeError("MODEL SECRET temporary-test-token")
            yield

        self.assert_error("EngineError", stream)

    def test_no_finish_rejected(self):
        async def stream(settings, source):
            yield {"type": "stage_summary", "stages": ["MODEL SECRET"]}

        self.assert_error("EngineProtocolError", stream)

    def test_malformed_event_rejected(self):
        async def stream(settings, source):
            yield "MODEL SECRET"

        self.assert_error("EngineProtocolError", stream)

    def test_missing_output_rejected(self):
        async def stream(settings, source):
            yield {"type": "finish", "translate_result": types.SimpleNamespace(mono_pdf_path=None)}

        self.assert_error("InvalidOutput", stream)

    def test_output_outside_job_rejected(self):
        outside = self.root / "outside.pdf"
        make_pdf(outside)

        async def stream(settings, source):
            yield {
                "type": "finish",
                "translate_result": types.SimpleNamespace(mono_pdf_path=outside),
            }

        self.assert_error("InvalidOutput", stream)

    def test_input_returned_as_output_rejected(self):
        async def stream(settings, source):
            yield {
                "type": "finish",
                "translate_result": types.SimpleNamespace(mono_pdf_path=source),
            }

        self.assert_error("InvalidOutput", stream)

    def test_original_returned_as_output_rejected(self):
        async def stream(settings, source):
            yield {
                "type": "finish",
                "translate_result": types.SimpleNamespace(mono_pdf_path=self.source),
            }

        self.assert_error("InvalidOutput", stream)

    def test_corrupt_and_empty_outputs_rejected(self):
        for data in (b"", b"not a PDF"):
            with self.subTest(data=data):

                async def stream(settings, source):
                    path = Path(settings.translation.output) / "bad.pdf"
                    path.write_bytes(data)
                    yield {
                        "type": "finish",
                        "translate_result": types.SimpleNamespace(mono_pdf_path=path),
                    }

                self.assert_error("InvalidOutput", stream)

    def test_duplicate_outputs_rejected(self):
        async def stream(settings, source):
            path = Path(settings.translation.output) / "same.pdf"
            make_pdf(path)
            yield {
                "type": "finish",
                "translate_result": types.SimpleNamespace(mono_pdf_path=path, dual_pdf_path=path),
            }

        self.assert_error("InvalidOutput", stream)

    def test_hardlinked_input_rejected(self):
        async def stream(settings, source):
            path = Path(settings.translation.output) / "hardlink.pdf"
            path.hardlink_to(source)
            yield {"type": "finish", "translate_result": types.SimpleNamespace(mono_pdf_path=path)}

        self.assert_error("InvalidOutput", stream)

    def test_page_limit(self):
        self.source.unlink()
        make_pdf(self.source, pages=2)
        self.request["max_pages"] = 1
        self.assert_error("PageLimit")

    def test_dimension_limit(self):
        self.source.unlink()
        make_pdf(self.source, width=5000, height=5000)
        self.assert_error("PixelLimit")

    def test_embedded_image_limit(self):
        self.source.unlink()
        with fitz.open() as document:
            page = document.new_page(width=30, height=30)
            page.insert_image(page.rect, stream=image_png(100, 100))
            document.save(self.source)
        self.request["max_pixels"] = 5000
        self.assert_error("PixelLimit")

    def test_encrypted_pdf(self):
        self.source.unlink()
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((20, 40), "Encrypted English text")
            document.save(
                self.source, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="test", user_pw="test"
            )
        self.assert_error("EncryptedPDF")

    def test_invalid_pdf(self):
        self.source.write_bytes(b"not a PDF")
        self.assert_error("InvalidPDF")

    def test_blank_pdf(self):
        self.source.unlink()
        make_pdf(self.source, text="")
        self.assert_error("NoSearchableText")

    def make_scan(self, rotated=False, mixed=False):
        self.source.unlink()
        with fitz.open() as document:
            if mixed:
                page = document.new_page(width=200, height=100)
                page.insert_text((10, 30), "Keep native English text")
            page = document.new_page(width=200, height=100)
            page.insert_image(page.rect, stream=image_png())
            if rotated:
                page.set_rotation(90)
            document.save(self.source)

    def fake_ocr(self):
        def engine(png):
            with Image.open(io.BytesIO(png)) as image:
                w, h = image.size
            return [
                [
                    [[10, 10], [w - 10, 10], [w - 10, h / 2], [10, h / 2]],
                    "Recognized English 中文",
                    0.99,
                ]
            ], [0.01]

        return engine

    def test_scan_default_off_actionable(self):
        self.make_scan()
        result = self.assert_error("OCRRequired")
        self.assertIn("requirements-engine-ocr.txt", result["error"])
        self.assertIn("auto", result["error"])

    def test_scan_optional_ocr_missing_actionable(self):
        self.make_scan()
        self.request["ocr_mode"] = "auto"
        with patch.dict(sys.modules, {"rapidocr_onnxruntime": None}):
            result = self.assert_error("OCRUnavailable")
        self.assertIn("3.10", result["error"])
        self.assertIn("requirements-engine-ocr.txt", result["error"])

    def test_scan_ocr_coordinates_invisible_and_mixed_native_preserved(self):
        self.make_scan(mixed=True)
        self.request["ocr_mode"] = "auto"
        before = self.source.read_bytes()
        with patch.object(runner, "_ocr_engine", return_value=self.fake_ocr()):
            code, result, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(result["pages"], 2)
        self.assertIn("白色背景", result["warnings"][0])
        settings, staged = self.captured[-1]
        self.assertTrue(settings.pdf.ocr_workaround)
        self.assertTrue(settings.pdf.skip_scanned_detection)
        self.assertFalse(settings.pdf.auto_enable_ocr_workaround)
        with fitz.open(staged) as document:
            self.assertIn("Keep native English", document[0].get_text())
            page = document[1]
            self.assertIn("Recognized English", page.get_text())
            self.assertIn("中文", page.get_text())
            self.assertTrue(all(span["type"] == 3 for span in page.get_texttrace()))
            pixmap = page.get_pixmap(alpha=False)
            self.assertEqual(set(pixmap.samples), {255})  # OCR is invisible.
            self.assertTrue(
                all(page.rect.contains(fitz.Rect(word[:4])) for word in page.get_text("words"))
            )
        self.assertEqual(before, self.source.read_bytes())

    def test_rotated_scan_visible_geometry_preserved(self):
        self.make_scan(rotated=True)
        self.request["ocr_mode"] = "auto"
        with patch.object(runner, "_ocr_engine", return_value=self.fake_ocr()):
            code, _, _ = self.invoke()
        self.assertEqual(code, 0)
        with fitz.open(self.captured[-1][1]) as document:
            self.assertEqual((document[0].rect.width, document[0].rect.height), (100, 200))
            self.assertIn("Recognized English", document[0].get_text())

    def test_existing_hidden_text_layer_needs_no_optional_ocr(self):
        self.source.unlink()
        make_pdf(self.source, hidden=True)
        with patch.object(runner, "_ocr_engine", side_effect=AssertionError("must not OCR twice")):
            code, result, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertTrue(self.captured[-1][0].pdf.ocr_workaround)
        self.assertTrue(result["warnings"])

    def test_image_default_off_actionable(self):
        self.source = self.root / "image.png"
        self.source.write_bytes(image_png())
        self.request["source"] = str(self.source)
        self.assert_error("OCRRequired")

    def test_supported_image_formats_ocr(self):
        self.request["ocr_mode"] = "auto"
        for suffix, fmt in (("png", "PNG"), ("jpg", "JPEG"), ("webp", "WEBP")):
            with self.subTest(format=fmt):
                self.source = self.root / f"image.{suffix}"
                Image.new("RGB", (400, 200), "white").save(self.source, format=fmt)
                self.request["source"] = str(self.source)
                before = self.source.read_bytes()
                with patch.object(runner, "_ocr_engine", return_value=self.fake_ocr()):
                    code, result, _ = self.invoke()
                self.assertEqual(code, 0)
                self.assertEqual(result["pages"], 1)
                self.assertTrue(result["warnings"])
                self.assertEqual(self.source.read_bytes(), before)

    def test_image_pixel_limit_checked_before_ocr(self):
        self.source = self.root / "image.png"
        self.source.write_bytes(image_png())
        self.request.update(source=str(self.source), ocr_mode="auto", max_pixels=100)
        with patch.object(
            runner, "_ocr_engine", side_effect=AssertionError("must preflight first")
        ):
            self.assert_error("PixelLimit")

    def test_multiframe_png_rejected(self):
        self.source = self.root / "animated.png"
        first, second = Image.new("RGB", (20, 20), "white"), Image.new("RGB", (20, 20), "black")
        first.save(self.source, save_all=True, append_images=[second], duration=100, loop=0)
        self.request.update(source=str(self.source), ocr_mode="auto")
        self.assert_error("UnsupportedInput")

    def test_no_ocr_text_rejected(self):
        self.make_scan()
        self.request["ocr_mode"] = "auto"
        with patch.object(runner, "_ocr_engine", return_value=lambda png: (None, [])):
            self.assert_error("OCRNoText")

    def test_unsupported_ocr_script_explicitly_rejected(self):
        self.make_scan()
        self.request["ocr_mode"] = "auto"

        def engine(png):
            return ([[[[10, 10], [200, 10], [200, 80], [10, 80]], "Привет", 1]], [])

        with patch.object(runner, "_ocr_engine", return_value=engine):
            self.assert_error("OCRTextUnsupported")

    def test_language_codes_pass_to_upstream(self):
        self.request.update(lang_in="fr", lang_out="ja")
        code, result, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        settings = self.captured[0][0]
        self.assertEqual(settings.translation.lang_in, "fr")
        self.assertEqual(settings.translation.lang_out, "ja")

    def test_request_restrictions(self):
        invalid = (
            ("base_url", "https://example.com/v1"),
            ("base_url", "http://localhost:1234/v1"),
            ("base_url", "http://127.0.0.1:1234/v1?key=temporary-test-token"),
            ("base_url", "http://temporary-test-token@127.0.0.1:1234/v1"),
            ("base_url", "http://127.0.0.1:1234/v1\n"),
            ("api_key", "token with spaces"),
            ("source", "relative.pdf"),
            ("max_pages", 21),
            ("max_pages", True),
            ("max_pixels", 20_000_001),
            ("qps", 2),
            ("table", True),
            ("lang_in", "French"),
            ("lang_out", "../ja"),
            ("model", "real-provider-model"),
            ("output_mode", "unknown"),
            ("ocr_mode", "on"),
        )
        original = self.request.copy()
        for key, value in invalid:
            with self.subTest(key=key, value=value):
                self.request = {**original, key: value}
                self.assert_error("InvalidRequest")

    def test_check_version_pin_and_schema(self):
        async def stream(settings, source):
            yield

        for version, expected in (("2.9.0", True), ("3.0.0", False)):
            with self.subTest(version=version):
                out = io.StringIO()
                with (
                    upstream(stream),
                    patch.object(
                        runner.metadata,
                        "version",
                        side_effect=lambda n: version if n == "pdf2zh-next" else "0.6.2",
                    ),
                    redirect_stdout(out),
                ):
                    code = runner.main(["--check"])
                result = json.loads(out.getvalue())
                self.assertEqual(set(result), {"ok", "python", "pdf2zh_next", "babeldoc", "errors"})
                self.assertEqual(result["ok"], expected)
                self.assertEqual(code, 0 if expected else 1)
                self.assertEqual(result["pdf2zh_next"], version)

    def test_missing_engine_check_does_not_install(self):
        out = io.StringIO()
        with (
            patch.object(
                runner.metadata, "version", side_effect=runner.metadata.PackageNotFoundError
            ),
            redirect_stdout(out),
        ):
            code = runner.main(["--check"])
        result = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertIsNone(result["pdf2zh_next"])
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_async_cancel_closes_stream(self):
        async def stream(settings, source):
            try:
                yield {"type": "progress_update", "overall_progress": 1}
                await asyncio.sleep(100)
            finally:
                self.closed.append(True)

        async def scenario():
            task = asyncio.create_task(
                runner._translate(None, self.source, self.job / "status.json")
            )
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with upstream(stream):
            asyncio.run(scenario())
        self.assertEqual(self.closed, [True])

    def test_check_dependency_import_failure_safe(self):
        out = io.StringIO()
        with (
            upstream(self.success_stream()),
            patch.object(
                runner.importlib, "import_module", side_effect=RuntimeError("MODEL SECRET")
            ),
            redirect_stdout(out),
        ):
            code = runner.main(["--check"])
        result = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(set(result), {"ok", "python", "pdf2zh_next", "babeldoc", "errors"})
        self.assertNotIn("MODEL SECRET", out.getvalue())

    def test_check_corrupt_metadata_preserves_schema(self):
        out = io.StringIO()
        with (
            patch.object(runner.metadata, "version", side_effect=RuntimeError("PRIVATE METADATA")),
            redirect_stdout(out),
        ):
            code = runner.main(["--check"])
        self.assertEqual(code, 1)
        result = json.loads(out.getvalue())
        self.assertEqual(set(result), {"ok", "python", "pdf2zh_next", "babeldoc", "errors"})
        self.assertNotIn("PRIVATE METADATA", out.getvalue())
        self.assertTrue(result["errors"])

    def test_malformed_request_safe(self):
        (self.job / "request.json").write_text("{bad-json", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = runner.main([str(self.job)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["error_type"], "InvalidRequest")

    def test_ocr_extra_enforces_legacy_python_range(self):
        module = types.ModuleType("rapidocr_onnxruntime")
        module.RapidOCR = lambda: self.fail("must not construct unsupported OCR")
        with (
            patch.dict(sys.modules, {"rapidocr_onnxruntime": module}),
            patch.object(runner.sys, "version_info", (3, 13, 0)),
        ):
            with self.assertRaises(runner.RunnerError) as raised:
                runner._ocr_engine()
        self.assertEqual(raised.exception.error_type, "OCRUnavailable")

    def test_atomic_write_failure_preserves_old_document(self):
        path = self.job / "atomic.json"
        path.write_text('{"old":true}', encoding="utf-8")
        with patch.object(runner.os, "replace", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                runner._atomic_json(path, {"new": True})
        self.assertEqual(json.loads(path.read_text()), {"old": True})
        self.assertFalse(list(self.job.glob(".runner-*.tmp")))

    def test_cli_starts_without_astrbot_or_site_packages(self):
        process = subprocess.run(
            [sys.executable, "-S", str(ROOT / "engine_runner.py"), "--check"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 1)
        self.assertEqual(len(process.stdout.splitlines()), 1)
        result = json.loads(process.stdout)
        self.assertEqual(set(result), {"ok", "python", "pdf2zh_next", "babeldoc", "errors"})
        self.assertFalse(result["ok"])
        self.assertNotIn("Traceback", process.stderr)

    def test_os_stdout_and_child_stdout_remain_private(self):
        script = (
            "import os,sys,subprocess;sys.path.insert(0,sys.argv[1]);import engine_runner as e\n"
            "with e._private_output():\n"
            " print('PRIVATE_PYTHON');os.write(1,b'PRIVATE_OS\\n');"
            "subprocess.run([sys.executable,'-c',\"print('PRIVATE_CHILD')\"],check=True)\n"
            "print('PUBLIC_JSON')\n"
        )
        process = subprocess.run(
            [sys.executable, "-c", script, str(ROOT)], capture_output=True, text=True, check=False
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), "PUBLIC_JSON")
        self.assertIn("PRIVATE_PYTHON", process.stderr)
        self.assertIn("PRIVATE_OS", process.stderr)
        self.assertIn("PRIVATE_CHILD", process.stderr)

    def test_atomic_status_has_no_model_text_and_invalid_progress_ignored(self):
        async def stream(settings, source):
            for value in (float("nan"), float("inf"), "MODEL SECRET", True):
                yield {"type": "progress_update", "overall_progress": value}
            yield {"type": "progress_update", "overall_progress": 999}
            self.assertEqual(
                json.loads((self.job / "status.json").read_text()),
                {"state": "translating", "progress": 100},
            )
            yield {"type": "error", "error": "MODEL SECRET"}

        self.assert_error("TranslationError", stream)


if __name__ == "__main__":
    unittest.main()
