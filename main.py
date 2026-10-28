"""AstrBot file/LLM-tool entry points; layout is delegated to upstream, not rewritten."""

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .bridge import ModelBridge
from .download import download, safe_name
from .errors import TranslationError
from .process import check_engine, python_path, run_engine

try:
    from asyncio import timeout
except ImportError:  # Python 3.10; installed by aiohttp on this Python version.
    from async_timeout import timeout

FORMATS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
HELP = """沉浸式文档翻译 · BabelDOC 开源引擎
私聊直接发送一个 PDF 即可翻译；或先发 /沉浸翻译，再发送附件。
/沉浸翻译 帮助 · /沉浸翻译 状态 · /沉浸翻译 取消
支持模型工具 immersive_translate，使用当前或同一用户最近发送的附件，不接受任意路径。
默认英→中，返回译文及双语 PDF。图片和扫描件需开启可选 OCR，仅适合清晰黑字白底，图片输出为 PDF。
使用 AstrBot 当前文本模型，会产生模型费用；原文发送至该模型服务。原文件不覆盖，不保证无损排版。
首次使用请先按 README 安装独立引擎。与沉浸式翻译官方无隶属关系，不使用会员账号/额度。"""


def integer(config, key, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(config.get(key, default))))
    except (ValueError, TypeError):
        return default


class ImmersiveTranslationPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.pending: dict[tuple[str, str], float] = {}
        self.pending_languages: dict[tuple[str, str], str] = {}
        self.recent: dict[tuple[str, str], tuple[float, object]] = {}
        self.jobs: dict[tuple[str, str], asyncio.Task] = {}
        self.active_directories: set[Path] = set()
        self.root = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "astrbot_plugin_immersivetranslation"
            / "jobs"
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self.cleaner = None
        self.closing = False
        self.check_lock = asyncio.Lock()

    def _key(self, event):
        return event.unified_msg_origin, str(event.get_sender_id())

    def _allowed(self, event, explicit=False):
        allowed = {str(user) for user in self.config.get("allowed_users", [])}
        if allowed and str(event.get_sender_id()) not in allowed:
            return False
        if event.get_group_id():
            if not self.config.get("enable_group", False):
                return False
            if (
                self.config.get("group_require_at", True)
                and not explicit
                and self._key(event) not in self.pending
            ):
                return bool(getattr(event, "is_at_or_wake_command", False))
        return True

    def _attachments(self, event):
        return [
            part
            for part in event.get_messages()
            if isinstance(part, Image)
            or (
                isinstance(part, File)
                and Path(safe_name(part.name or "")).suffix.lower() in FORMATS
            )
        ]

    def _prune(self):
        now = time.monotonic()
        self.pending = {key: expiry for key, expiry in self.pending.items() if expiry > now}
        self.pending_languages = {
            key: language for key, language in self.pending_languages.items() if key in self.pending
        }
        self.recent = {key: item for key, item in self.recent.items() if item[0] > now}

    def _start_cleaner(self):
        if self.cleaner is None:
            self.cleaner = asyncio.create_task(self._maintenance())

    async def _maintenance(self):
        while True:
            try:
                self._prune()
                retention = integer(self.config, "cache_minutes", 60, 5, 1440) * 60
                for path in self.root.iterdir():
                    if (
                        path in self.active_directories
                        or path.is_symlink()
                        or not path.is_dir()
                        or not re.fullmatch(r"job_[0-9a-f]{32}", path.name)
                    ):
                        continue
                    if time.time() - path.stat().st_mtime > retention:
                        await asyncio.to_thread(shutil.rmtree, path)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("沉浸翻译缓存清理失败：%s", type(exc).__name__)
            await asyncio.sleep(60)

    async def _source(self, component, directory):
        if isinstance(component, File):
            suffix = Path(safe_name(component.name or "document.pdf")).suffix.lower()
            source = component.url or await component.get_file(allow_return_url=True)
            if suffix not in FORMATS:
                raise TranslationError("仅支持 PDF、PNG、JPEG 和 WebP。")
        else:
            suffix = ".png"
            source = component.url or component.file
        if not isinstance(source, str) or not source:
            raise TranslationError("平台没有提供可读取的附件，请重新发送原文件。")
        destination = directory / ("source" + suffix)
        maximum = integer(self.config, "max_file_mb", 20, 1, 100) * 1024 * 1024
        if source.startswith(("http://", "https://")):
            await download(
                source,
                destination,
                maximum,
                bool(self.config.get("allow_private_downloads", False)),
            )
        else:
            if source.startswith("file://"):
                parsed = urlsplit(source)
                if parsed.netloc not in {"", "localhost"}:
                    raise TranslationError("不支持网络共享路径。")
                source = unquote(parsed.path)
                if os.name == "nt" and re.match(r"^/[A-Za-z]:", source):
                    source = source[1:]
            if source.startswith(("base64://", "\\\\", "//")):
                raise TranslationError("请将图片按文件发送；不支持 base64 或网络共享路径。")
            path = Path(source)
            if not path.is_file() or path.stat().st_size > maximum:
                raise TranslationError("无法读取附件或文件超过大小限制。")
            await asyncio.to_thread(shutil.copyfile, path, destination)
            if destination.stat().st_size > maximum:
                raise TranslationError("文件超过大小限制。")
        return destination

    async def _job(self, event, component, target, directory):
        try:
            async with timeout(integer(self.config, "job_timeout", 1800, 60, 7200)):
                source = await self._source(component, directory)
                provider = str(self.config.get("provider_id", "")).strip()
                if not provider:
                    provider = await self.context.get_current_chat_provider_id(
                        event.unified_msg_origin
                    )
                if not provider:
                    raise TranslationError(
                        "当前会话未配置文本模型，请设置 provider_id 或选择 AstrBot 模型。"
                    )
                async with ModelBridge(
                    self.context,
                    provider,
                    integer(self.config, "max_model_calls", 200, 1, 2000),
                    integer(self.config, "model_timeout", 120, 10, 600),
                ) as bridge:
                    request = {
                        "source": str(source.resolve()),
                        "lang_in": str(self.config.get("source_language", "en")),
                        "lang_out": target,
                        "output_mode": str(self.config.get("output_mode", "both")),
                        "base_url": bridge.url,
                        "api_key": bridge.token,
                        "model": "astrbot",
                        "max_pages": integer(self.config, "max_pages", 20, 1, 20),
                        "max_pixels": integer(self.config, "max_pixels_million", 20, 1, 20)
                        * 1000000,
                        "ocr_mode": str(self.config.get("ocr_mode", "off")),
                        "qps": 1,
                        "table": False,
                    }
                    python = python_path(self.config)
                    request_path = directory / "request.json"
                    request_path.write_text(json.dumps(request, ensure_ascii=False), "utf-8")
                    if os.name != "nt":
                        request_path.chmod(0o600)
                    try:
                        result = await run_engine(python, directory, bridge)
                    finally:
                        request_path.unlink(missing_ok=True)
                    result["model_bridge_requests"] = bridge.calls
                outputs = result.get("outputs", [])
                if not outputs:
                    raise TranslationError("引擎未生成译本。")
                for output in outputs:
                    path = Path(output["path"]).resolve()
                    if (
                        not path.is_relative_to(directory.resolve())
                        or path.suffix.lower() != ".pdf"
                        or not path.is_file()
                    ):
                        raise TranslationError("引擎输出路径无效。")
                    name = "双语对照.pdf" if output.get("kind") == "dual" else "译文.pdf"
                    await event.send(event.chain_result([File(name=name, file=str(path))]))
                report = directory / "report.json"
                report.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
                await event.send(
                    event.chain_result([File(name="翻译报告.json", file=str(report.resolve()))])
                )
                notices = "\n".join(str(value)[:250] for value in result.get("warnings", [])[:3])
                await event.send(
                    event.plain_result(
                        "翻译完成，原文件未覆盖。请核对公式、表格和排版。"
                        + ("\n" + notices if notices else "")
                    )
                )
        except TimeoutError:
            await event.send(
                event.plain_result("翻译超过总时限，已终止引擎；已发生的模型调用可能计费。")
            )
        except asyncio.CancelledError:
            raise
        except TranslationError as exc:
            await event.send(event.plain_result(str(exc)))
        except Exception as exc:
            logger.error("沉浸翻译失败：%s", type(exc).__name__)
            try:
                await event.send(
                    event.plain_result(
                        "翻译或附件发送失败，请检查引擎环境、模型配置及平台文件发送能力。原文件未覆盖。"
                    )
                )
            except Exception:
                logger.warning("沉浸翻译失败通知未能发送。")
        finally:
            (directory / "request.json").unlink(missing_ok=True)
            self.jobs.pop(self._key(event), None)
            self.active_directories.discard(directory)
            os.utime(directory, None)

    async def _launch(self, event, target=""):
        if self.closing:
            return "插件正在卸载。"
        self._prune()
        self._start_cleaner()
        if self.jobs:
            return "已有翻译任务正在运行，请稍后重试（不排队）。"
        attachments = self._attachments(event)
        key = self._key(event)
        language = (
            target
            or self.pending_languages.get(key)
            or str(self.config.get("target_language", "zh"))
        )
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:[-_][A-Za-z]{2,8})?", language):
            return "目标语言请使用语言代码，例如 zh、en、ja、zh-CN。"
        if not attachments and key in self.recent:
            attachments = [self.recent[key][1]]
        if len(attachments) > 1:
            return "请一次只发送一个 PDF 或图片附件。"
        if not attachments:
            if len(self.pending) >= 256 and key not in self.pending:
                return "等待请求过多，请稍后重试。"
            self.pending[key] = time.monotonic() + 120
            self.pending_languages[key] = language
            return "请在120秒内发送一个 PDF 或图片附件。原文将发送到配置的模型，可能产生费用。"
        directory = self.root / ("job_" + uuid.uuid4().hex)
        directory.mkdir(mode=0o700)
        self.pending.pop(key, None)
        self.pending_languages.pop(key, None)
        self.recent.pop(key, None)
        self.active_directories.add(directory)
        task = asyncio.create_task(self._job(event, attachments[0], language, directory))
        self.jobs[key] = task
        task.add_done_callback(lambda finished: self._task_done(key, directory, finished))
        return "翻译任务已启动，完成后会自动发送译本；/沉浸翻译 取消 可中止。原文会发送至配置的模型并可能计费。"

    def _task_done(self, key, directory, task):
        if self.jobs.get(key) is task:
            self.jobs.pop(key, None)
        self.active_directories.discard(directory)
        if directory.is_dir():
            (directory / "request.json").unlink(missing_ok=True)
            os.utime(directory, None)
        if not task.cancelled():
            error = task.exception()
            if error:
                logger.error("沉浸翻译后台任务异常：%s", type(error).__name__)

    @filter.llm_tool(name="immersive_translate")
    async def immersive_translate(self, event: AstrMessageEvent, target_language: str = "") -> str:
        """Translate the user's current or recent attached PDF/image while preserving layout using BabelDOC. Starts a background task and sends PDF results back to this chat. Requires a user translation request; source text goes to the configured model and may incur costs. No arbitrary paths or URLs.

        Args:
            target_language(string): Target language code, for example zh, en or ja. Empty uses plugin configuration.
        """
        self._prune()
        if not self._allowed(event):
            return "当前用户或群聊不允许使用此工具。"
        return await self._launch(event, target_language)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_message(self, event: AstrMessageEvent):
        self._prune()
        text = "".join(
            part.text for part in event.get_messages() if isinstance(part, Plain)
        ).strip()
        command = re.fullmatch(r"/?沉浸翻译(?:\s+(.*))?", text)
        if not self._allowed(event, explicit=command is not None):
            return
        key = self._key(event)
        if command:
            event.stop_event()
            argument = (command[1] or "").strip()
            if argument in {"帮助", "help"}:
                await event.send(event.plain_result(HELP))
            elif argument in {"取消", "cancel"}:
                self.pending.pop(key, None)
                self.pending_languages.pop(key, None)
                self.recent.pop(key, None)
                task = self.jobs.get(key)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await event.send(
                    event.plain_result(
                        "已取消等待或当前任务。已发出的文件不能撤回；模型调用可能已计费。"
                    )
                )
            elif argument in {"状态", "status"}:
                if self.check_lock.locked():
                    await event.send(event.plain_result("引擎自检正在进行。"))
                    return
                try:
                    async with self.check_lock:
                        result = await check_engine(
                            python_path(self.config), self.root.parent / "engine_home"
                        )
                    message = f"引擎{'可用' if result.get('ok') else '未就绪'}；pdf2zh-next：{result.get('pdf2zh_next') or '未安装'}；运行任务：{len(self.jobs)}。"
                    if result.get("errors"):
                        message += "\n" + "\n".join(
                            str(item)[:300] for item in result["errors"][:4]
                        )
                    await event.send(event.plain_result(message))
                except Exception as exc:
                    await event.send(
                        event.plain_result(
                            str(exc)
                            if isinstance(exc, TranslationError)
                            else "引擎自检失败，请检查 engine_python。"
                        )
                    )
            elif argument in {"", "翻译", "开始"}:
                await event.send(event.plain_result(await self._launch(event)))
            else:
                await event.send(event.plain_result(HELP))
            return
        attachments = self._attachments(event)
        if not attachments:
            return
        if len(attachments) == 1 and (key in self.recent or len(self.recent) < 256):
            self.recent[key] = (
                time.monotonic() + integer(self.config, "recent_timeout", 120, 15, 600),
                attachments[0],
            )
        if key in self.pending or self.config.get("auto_translate_attachment", True):
            event.stop_event()
            await event.send(event.plain_result(await self._launch(event)))

    async def terminate(self):
        self.closing = True
        tasks = list(self.jobs.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.cleaner:
            self.cleaner.cancel()
            await asyncio.gather(self.cleaner, return_exceptions=True)
        self.pending.clear()
        self.pending_languages.clear()
        self.recent.clear()
