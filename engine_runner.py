"""Isolated pdf2zh-next 2.9.0 runner (never imported by AstrBot).

Run with the dedicated engine interpreter: ``python engine_runner.py JOBDIR``.
Only the final, user-safe JSON object is written to stdout. Third-party output
is routed to stderr; that stream may contain document text and must stay local.
The caller owns process-group cancellation, timeouts and job-directory retention.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager, redirect_stdout
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit

ENGINE_VERSION = "2.9.0"
MAX_PAGES = 20
MAX_PIXELS = 20_000_000
OCR_WARNING = (
    "已使用本地 OCR（仅英文/中文）及黑字白底兼容模式；扫描页会栅格化，"
    "白色背景可能遮盖图片、底色或细节，不保证照片保真、复杂排版或公式识别准确。"
)


class RunnerError(Exception):
    """A deliberately user-safe error; never wrap an upstream exception's text."""

    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


def _fail(kind: str, message: str):
    raise RunnerError(kind, message)


def _atomic_json(path: Path, value: dict) -> None:
    # A same-directory unique temporary prevents readers seeing partial JSON.
    fd, temporary = tempfile.mkstemp(prefix=".runner-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _private_output():
    """Also redirect OS stdout inherited by pdf2zh's spawned worker processes."""
    saved_fd = None
    try:
        sys.stdout.flush()
        try:
            stdout_fd, stderr_fd = sys.stdout.fileno(), sys.stderr.fileno()
            saved_fd = os.dup(stdout_fd)
            os.dup2(stderr_fd, stdout_fd)
        except (AttributeError, OSError, io.UnsupportedOperation):
            pass  # StringIO/test embedding: Python-level redirection still works.
        with redirect_stdout(sys.stderr):
            yield
    finally:
        if saved_fd is not None:
            os.dup2(saved_fd, stdout_fd)
            os.close(saved_fd)


def _dependency_report() -> dict:
    report = {
        "ok": False,
        "python": sys.executable,
        "pdf2zh_next": None,
        "babeldoc": None,
        "errors": [],
    }
    for distribution, key in (("pdf2zh-next", "pdf2zh_next"), ("babeldoc", "babeldoc")):
        try:
            report[key] = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            report["errors"].append(
                f"缺少 {distribution}，请在独立引擎环境安装 requirements-engine.txt。"
            )
        except Exception:
            report["errors"].append(f"无法读取 {distribution} 版本，请修复独立引擎环境。")
    if report["pdf2zh_next"] is not None and report["pdf2zh_next"] != ENGINE_VERSION:
        report["errors"].append("不支持此 pdf2zh-next 版本，请使用 pdf2zh-next==2.9.0。")
    # Imports, not metadata alone, catch missing native libraries/transitive deps.
    if not report["errors"]:
        for module in (
            "fitz",
            "PIL.Image",
            "pdf2zh_next.config.model",
            "pdf2zh_next.config.translate_engine_model",
            "pdf2zh_next.high_level",
        ):
            try:
                importlib.import_module(module)
            except Exception:
                report["errors"].append(
                    "引擎依赖无法导入，请修复独立 Python 环境并重新安装 requirements-engine.txt。"
                )
                break
    report["ok"] = not report["errors"]
    return report


def _request(jobdir: Path) -> dict:
    try:
        path = jobdir / "request.json"
        if path.stat().st_size > 64 * 1024:
            _fail("InvalidRequest", "翻译请求过大。")
        request = json.loads(path.read_text(encoding="utf-8"))
    except RunnerError:
        raise
    except Exception:
        _fail("InvalidRequest", "无法读取有效的 request.json 翻译请求。")
    if not isinstance(request, dict):
        _fail("InvalidRequest", "翻译请求必须是 JSON 对象。")
    defaults = {
        "lang_in": "en",
        "lang_out": "zh",
        "output_mode": "both",
        "model": "astrbot",
        "max_pages": MAX_PAGES,
        "max_pixels": MAX_PIXELS,
        "ocr_mode": "off",
        "qps": 1,
        "table": False,
    }
    result = {**defaults, **request}
    if result["model"] != "astrbot":
        _fail("InvalidRequest", "仅支持 AstrBot 本地模型桥。")
    for field in ("lang_in", "lang_out"):
        if not isinstance(result[field], str) or not re.fullmatch(
            r"[A-Za-z]{2,3}(?:[-_][A-Za-z]{2,8})?", result[field]
        ):
            _fail("InvalidRequest", "源语言和目标语言应为语言代码，例如 en、zh、ja。")
    if result["output_mode"] not in ("both", "mono", "dual") or result["ocr_mode"] not in (
        "off",
        "auto",
    ):
        _fail("InvalidRequest", "输出模式或 OCR 模式无效。")
    for field, maximum in (("max_pages", MAX_PAGES), ("max_pixels", MAX_PIXELS)):
        if type(result[field]) is not int or not 1 <= result[field] <= maximum:
            _fail("InvalidRequest", "页数或像素限制无效。")
    if type(result["qps"]) is not int or result["qps"] != 1 or result["table"] is not False:
        _fail("InvalidRequest", "当前引擎仅支持 qps=1 且关闭实验性表格识别。")
    if not isinstance(result.get("source"), str) or not Path(result["source"]).is_absolute():
        _fail("InvalidRequest", "源文件必须是绝对路径。")
    source = Path(result["source"]).resolve()
    if not source.is_file():
        _fail("InvalidInput", "源文件不存在或不是普通文件。")
    result["source"] = source
    try:
        base_url = result.get("base_url")
        if not isinstance(base_url, str):
            raise ValueError
        url = urlsplit(base_url)
        if (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or not url.port
            or url.path != "/v1"
            or url.query
            or url.fragment
            or url.username
            or url.password
            or base_url != f"http://127.0.0.1:{url.port}/v1"
        ):
            raise ValueError
    except (ValueError, TypeError):
        _fail("InvalidRequest", "模型桥地址必须是 http://127.0.0.1:端口/v1。")
    token = result.get("api_key")
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 4096
        or any(c.isspace() for c in token)
    ):
        _fail("InvalidRequest", "缺少有效的临时模型桥令牌。")
    return result


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _ocr_engine():
    try:
        if not (3, 10) <= sys.version_info[:2] <= (3, 12):
            raise RuntimeError("Unsupported legacy OCR Python version")
        from rapidocr_onnxruntime import RapidOCR

        return RapidOCR()
    except Exception:
        _fail(
            "OCRUnavailable",
            "需要本地 OCR；请使用 Python 3.10–3.12 的独立引擎环境安装 requirements-engine-ocr.txt，并启用 ocr_mode=auto。",
        )


def _ocr_layer(page, png: bytes, pixel_width: int, pixel_height: int, engine, fitz) -> None:
    try:
        lines, _elapsed = engine(png)
    except Exception:
        _fail("OCRFailed", "本地 OCR 识别失败，请使用清晰的英文/中文文档重试。")
    inserted = 0
    for entry in lines or []:
        try:
            quad, text, confidence = entry
            if not isinstance(text, str) or not text.strip() or float(confidence) < 0.3:
                continue
            text = " ".join(text.split())
            if any(
                not (
                    32 <= ord(c) <= 126
                    or 0xA0 <= ord(c) <= 0x24F
                    or 0x2000 <= ord(c) <= 0x206F
                    or 0x3000 <= ord(c) <= 0x303F
                    or 0x3400 <= ord(c) <= 0x9FFF
                    or 0xFF00 <= ord(c) <= 0xFFEF
                )
                for c in text
            ):
                _fail("OCRTextUnsupported", "本地 OCR 文本层目前仅支持英文和中文。")
            xs = [float(point[0]) * page.rect.width / pixel_width for point in quad]
            ys = [float(point[1]) * page.rect.height / pixel_height for point in quad]
            rect = fitz.Rect(min(xs), min(ys), max(xs), max(ys)) & page.rect
            if rect.is_empty or not all(math.isfinite(v) for v in rect):
                continue
            fontname = "china-s" if any(ord(c) > 126 for c in text) else "helv"
            font = fitz.Font(fontname=fontname)
            # Reserved CJK fonts may use full-em Latin advances in PDF even
            # when Font.text_length reports proportional Latin metrics.
            advance = max(
                font.text_length(text, fontsize=1), len(text) if fontname == "china-s" else 0, 0.01
            )
            size = min(rect.height * 0.7, rect.width / advance) * 0.95
            # Negative return means nothing was inserted: shrink until it fits.
            for _ in range(12):
                if (
                    page.insert_textbox(
                        rect, text, fontname=fontname, fontsize=size, render_mode=3, overlay=True
                    )
                    >= 0
                ):
                    inserted += 1
                    break
                size *= 0.8
        except RunnerError:
            raise
        except Exception:
            _fail("OCRFailed", "OCR 坐标或文本层无效，请换用清晰的英文/中文文档。")
    if not inserted or not page.get_text("text").strip():
        _fail("OCRNoText", "未识别到可翻译的英文/中文文本，请使用更清晰的扫描件。")


def _check_page(page, max_pixels: int) -> None:
    width, height = page.rect.width, page.rect.height
    # Scan OCR uses 144 dpi: preflight the actual raster allocation, not just points.
    if (
        not math.isfinite(width)
        or not math.isfinite(height)
        or width <= 0
        or height <= 0
        or math.ceil(width * 2) * math.ceil(height * 2) > max_pixels
    ):
        _fail("PixelLimit", "PDF 页面尺寸过大，超过像素限制（按 144 DPI 检查）。")
    for image in page.get_images(full=True):
        if image[2] <= 0 or image[3] <= 0 or image[2] * image[3] > max_pixels:
            _fail("PixelLimit", "PDF 内嵌图片超过像素限制，请先压缩文件。")


def _prepare_pdf(request: dict, work: Path):
    import fitz  # PyMuPDF 1.25.2's supported legacy import; do not use pymupdf.

    source = request["source"]
    destination = work / "input.pdf"
    if source.suffix.lower() != ".pdf":
        if source.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            _fail("UnsupportedInput", "仅支持 PDF、PNG、JPEG 或 WebP 文件。")
        if request["ocr_mode"] != "auto":
            _fail(
                "OCRRequired",
                "图片翻译需要启用 ocr_mode=auto，并安装 requirements-engine-ocr.txt。",
            )
        from PIL import Image, ImageOps

        try:
            with Image.open(source) as image:
                if (
                    image.format not in ("PNG", "JPEG", "WEBP")
                    or getattr(image, "n_frames", 1) != 1
                ):
                    _fail("UnsupportedInput", "仅支持单帧 PNG、JPEG 或 WebP 图片。")
                if image.width * image.height > request["max_pixels"]:
                    _fail("PixelLimit", "图片超过像素限制，请先缩小图片。")
                image = ImageOps.exif_transpose(image)
                # Composite transparency onto white, matching the OCR workaround.
                rgba = image.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                background.alpha_composite(rgba)
                rgb = background.convert("RGB")
                data = io.BytesIO()
                rgb.save(data, format="PNG")
                png, width, height = data.getvalue(), rgb.width, rgb.height
        except RunnerError:
            raise
        except Exception:
            _fail("InvalidInput", "无法读取图片；请提供有效的单帧 PNG、JPEG 或 WebP。")
        engine = _ocr_engine()
        with fitz.open() as document:
            page = document.new_page(width=width / 2, height=height / 2)
            page.insert_image(page.rect, stream=png)
            _ocr_layer(page, png, width, height, engine, fitz)
            document.save(destination)
        return destination, 1, True, [OCR_WARNING]
    try:
        document = fitz.open(source)
    except Exception:
        _fail("InvalidPDF", "无法读取 PDF，请检查文件是否完整。")
    with document:
        if not document.is_pdf:
            _fail("InvalidPDF", "源文件不是真正的 PDF。")
        if document.is_encrypted or document.needs_pass:
            _fail("EncryptedPDF", "不支持加密 PDF，请先解密再提交。")
        pages = len(document)
        if not 1 <= pages <= request["max_pages"]:
            _fail("PageLimit", "PDF 页数为空或超过页数上限，请拆分后重试。")
        missing = []
        hidden_layer = False
        any_text = False
        for index, page in enumerate(document):
            _check_page(page, request["max_pixels"])
            text = page.get_text("text").strip()
            if text:
                any_text = True
                visible = any(
                    span.get("type") != 3 and span.get("opacity", 1) > 0
                    for span in page.get_texttrace()
                )
                hidden_layer = hidden_layer or not visible
            elif page.get_images() or page.get_drawings():
                missing.append(index)
        if not any_text and not missing:
            _fail("NoSearchableText", "PDF 没有可翻译的文本。")
        if missing and request["ocr_mode"] != "auto":
            _fail(
                "OCRRequired",
                "PDF 含无文本层的扫描页；默认关闭 OCR。请启用 ocr_mode=auto 并安装 requirements-engine-ocr.txt，或提交可搜索 PDF。",
            )
        if missing:
            engine = _ocr_engine()
            with fitz.open() as prepared:
                for index, original in enumerate(document):
                    if index not in missing:
                        prepared.insert_pdf(document, from_page=index, to_page=index)
                        continue
                    pixmap = original.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    if pixmap.width * pixmap.height > request["max_pixels"]:
                        _fail("PixelLimit", "扫描页超过 OCR 像素限制。")
                    png = pixmap.tobytes("png")
                    page = prepared.new_page(width=original.rect.width, height=original.rect.height)
                    page.insert_image(page.rect, stream=png)
                    _ocr_layer(page, png, pixmap.width, pixmap.height, engine, fitz)
                prepared.save(destination)
        else:
            shutil.copyfile(source, destination)
    converted = bool(missing or hidden_layer)
    return destination, pages, converted, [OCR_WARNING] if converted else []


def _settings(request: dict, output: Path, ocr: bool):
    from pdf2zh_next.config.model import (
        BasicSettings,
        PDFSettings,
        SettingsModel,
        TranslationSettings,
    )
    from pdf2zh_next.config.translate_engine_model import OpenAISettings

    settings = SettingsModel(
        basic=BasicSettings(debug=False),
        translation=TranslationSettings(
            lang_in=request["lang_in"],
            lang_out=request["lang_out"],
            output=str(output),
            qps=1,
            pool_max_workers=1,
            term_qps=1,
            term_pool_max_workers=1,
            ignore_cache=True,
            no_auto_extract_glossary=True,
        ),
        pdf=PDFSettings(
            no_dual=request["output_mode"] == "mono",
            no_mono=request["output_mode"] == "dual",
            translate_table_text=False,
            ocr_workaround=ocr,
            skip_scanned_detection=ocr,
            auto_enable_ocr_workaround=False,
            watermark_output_mode="no_watermark",
        ),
        translate_engine_settings=OpenAISettings(
            openai_model="astrbot",
            openai_base_url=request["base_url"],
            openai_api_key=request["api_key"],
        ),
    )
    settings.validate_settings()
    return settings


async def _translate(settings, source: Path, status: Path):
    from pdf2zh_next.high_level import do_translate_async_stream

    # Explicit aclose propagates cancellation/cleanup on early finish/error too.
    stream = do_translate_async_stream(settings, source)
    try:
        async for event in stream:
            if not isinstance(event, dict):
                _fail("EngineProtocolError", "翻译引擎返回了无效状态。")
            event_type = event.get("type")
            if event_type == "error":
                _fail(
                    "TranslationError", "翻译引擎处理失败，请检查本地引擎日志或换用其他文件重试。"
                )
            if event_type == "finish":
                result = event.get("translate_result")
                if result is None:
                    _fail("EngineProtocolError", "翻译引擎没有返回结果。")
                return result
            if event_type in ("progress_start", "progress_update", "progress_end"):
                progress = event.get("overall_progress")
                if (
                    isinstance(progress, (int, float))
                    and not isinstance(progress, bool)
                    and math.isfinite(progress)
                ):
                    _atomic_json(
                        status, {"state": "translating", "progress": max(0, min(100, progress))}
                    )
        _fail("EngineProtocolError", "翻译引擎提前结束，未生成结果。")
    finally:
        await stream.aclose()


def _outputs(result, request: dict, jobdir: Path, source: Path, staged: Path) -> list[dict]:
    import fitz

    outputs = []
    for kind in ("mono", "dual"):
        if request["output_mode"] not in ("both", kind):
            continue
        value = getattr(result, f"{kind}_pdf_path", None)
        if value is None:
            _fail("InvalidOutput", "翻译引擎未生成所请求的 PDF 输出。")
        try:
            path = Path(value)
            if not path.is_absolute():
                _fail("InvalidOutput", "翻译输出不是有效的绝对路径。")
            path = path.resolve(strict=True)
            if (
                not path.is_relative_to(jobdir)
                or path in (source, staged)
                or path.suffix.lower() != ".pdf"
                or not path.is_file()
                or path.stat().st_size == 0
                or os.path.samefile(path, source)
                or os.path.samefile(path, staged)
            ):
                _fail("InvalidOutput", "翻译输出路径不安全或文件无效。")
            if any(
                path == Path(item["path"]) or os.path.samefile(path, item["path"])
                for item in outputs
            ):
                _fail("InvalidOutput", "翻译引擎返回了重复的输出文件。")
            with fitz.open(path) as document:
                if (
                    not document.is_pdf
                    or document.needs_pass
                    or document.is_encrypted
                    or not len(document)
                ):
                    _fail("InvalidOutput", "翻译输出不是有效的可读 PDF。")
                for page in document:
                    _ = page.rect
        except RunnerError:
            raise
        except Exception:
            _fail("InvalidOutput", "翻译输出缺失、损坏或无法读取。")
        outputs.append({"kind": kind, "path": str(path)})
    return outputs


def run_job(jobdir: Path) -> dict:
    started = time.monotonic()
    jobdir = jobdir.resolve(strict=True)
    if not jobdir.is_dir():
        _fail("InvalidRequest", "任务目录不存在。")
    status_path = jobdir / "status.json"
    _atomic_json(status_path, {"state": "preparing", "progress": 0})
    request = _request(jobdir)
    report = _dependency_report()
    if not report["ok"]:
        _fail("EngineUnavailable", " ".join(report["errors"]))
    original_hash = _digest(request["source"])
    # Fresh directories also prevent stale/symlinked outputs from a previous run.
    work = Path(tempfile.mkdtemp(prefix="runner-", dir=jobdir))
    output = work / "output"
    output.mkdir()
    staged, pages, ocr, warnings = _prepare_pdf(request, work)
    settings = _settings(request, output, ocr)
    _atomic_json(status_path, {"state": "translating", "progress": 0})
    result = asyncio.run(_translate(settings, staged, status_path))
    outputs = _outputs(result, request, jobdir, request["source"], staged)
    if _digest(request["source"]) != original_hash:
        _fail("OriginalChanged", "源文件在翻译过程中发生变化，请重新提交原文件。")
    return {
        "ok": True,
        "outputs": outputs,
        "warnings": warnings,
        "pages": pages,
        "seconds": round(time.monotonic() - started, 3),
    }


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check = args == ["--check"]
    jobdir = None
    try:
        with _private_output():
            if check:
                result = _dependency_report()
            else:
                if len(args) != 1 or args[0].startswith("--"):
                    _fail("InvalidRequest", "用法：python engine_runner.py 任务目录，或 --check。")
                jobdir = Path(args[0]).resolve(strict=True)
                result = run_job(jobdir)
    except RunnerError as exc:
        result = {"ok": False, "error": str(exc), "error_type": exc.error_type}
    except KeyboardInterrupt:
        result = {"ok": False, "error": "翻译已取消。", "error_type": "Cancelled"}
    except Exception:
        result = {
            "ok": False,
            "error": "翻译失败，请检查独立引擎环境和本地日志。",
            "error_type": "EngineError",
        }
    if jobdir is not None and jobdir.is_dir():
        try:
            _atomic_json(jobdir / "result.json", result)
            _atomic_json(
                jobdir / "status.json",
                {
                    "state": "completed" if result["ok"] else "failed",
                    "progress": 100 if result["ok"] else 0,
                },
            )
        except Exception:
            result = {
                "ok": False,
                "error": "无法保存任务结果，请检查任务目录权限。",
                "error_type": "ResultWriteError",
            }
    print(json.dumps(result, ensure_ascii=True, allow_nan=False), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
