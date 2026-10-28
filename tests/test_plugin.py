"""Offline main.py tests: synthetic package imports and minimal AstrBot doubles.

Run from the repository root with:
    .engine/Scripts/python.exe -m unittest discover -s tests -p test_plugin.py -v
No AstrBot installation, model calls, engine process, or HTTP requests are used.
"""

import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]


class File:
    def __init__(self, name="document.pdf", file=None, url=None):
        self.name, self.file, self.url = name, file, url
        self.get_file = AsyncMock(return_value=file)


class Image:
    def __init__(self, file=None, url=None):
        self.file, self.url = file, url


class Plain:
    def __init__(self, text):
        self.text = text


class Star:
    def __init__(self, context):
        self.context = context


class FakeEvent:
    def __init__(self, parts=(), *, user="alice", session="private:alice", group="", at=False):
        self.parts = list(parts)
        self.user, self.group = user, group
        self.unified_msg_origin = session
        self.is_at_or_wake_command = at
        self.sent = []
        self.stopped = False

    def get_sender_id(self):
        return self.user

    def get_group_id(self):
        return self.group

    def get_messages(self):
        return self.parts

    def plain_result(self, text):
        return text

    def chain_result(self, parts):
        return parts

    async def send(self, result):
        self.sent.append(result)

    def stop_event(self):
        self.stopped = True


def load_plugin():
    """Do not import the real plugin __init__, or leak AstrBot stubs to other tests."""
    package_name = "_immersive_main_unit_tests"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    modules = {package_name: package}
    for name in (
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.message_components",
        "astrbot.api.star",
        "astrbot.core",
        "astrbot.core.utils",
        "astrbot.core.utils.astrbot_path",
    ):
        modules[name] = types.ModuleType(name)
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = logging.getLogger("immersive.tests")
    event_api = modules["astrbot.api.event"]
    event_api.AstrMessageEvent = FakeEvent
    event_api.filter = types.SimpleNamespace(
        llm_tool=lambda *args, **kwargs: lambda function: function,
        event_message_type=lambda *args, **kwargs: lambda function: function,
        EventMessageType=types.SimpleNamespace(ALL="all"),
    )
    components = modules["astrbot.api.message_components"]
    components.File, components.Image, components.Plain = File, Image, Plain
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = lambda: "."
    spec = importlib.util.spec_from_file_location(package_name + ".main", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    modules[spec.name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


plugin_module = load_plugin()
TranslationError = plugin_module.TranslationError


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        data_patch = patch.object(
            plugin_module, "get_astrbot_data_path", return_value=self.temp.name
        )
        data_patch.start()
        self.addCleanup(data_patch.stop)
        self.context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="test-provider"),
        )
        self.plugin = plugin_module.ImmersiveTranslationPlugin(self.context, {})
        self.addAsyncCleanup(self.plugin.terminate)
        # Most tests need deterministic job dispatch, not the maintenance timer.
        cleaner_patch = patch.object(self.plugin, "_start_cleaner")
        cleaner_patch.start()
        self.addCleanup(cleaner_patch.stop)
        self.attachment = File("paper.pdf", url="https://example.invalid/paper.pdf")

    def event(self, *parts, **kwargs):
        return FakeEvent(parts, **kwargs)

    async def run_message(self, *parts, **kwargs):
        event = self.event(*parts, **kwargs)
        await self.plugin.on_message(event)
        return event

    async def hold_job(self, *args):
        await asyncio.Event().wait()

    async def launch_blocked_job(self, event=None):
        event = event or self.event(self.attachment)
        job_patch = patch.object(self.plugin, "_job", side_effect=self.hold_job)
        job_patch.start()
        self.addCleanup(job_patch.stop)
        response = await self.plugin._launch(event)
        self.assertIn("已启动", response)
        return event, self.plugin.jobs[self.plugin._key(event)]

    async def test_recent_attachment_requires_same_user_and_session(self):
        self.plugin.config["auto_translate_attachment"] = False
        original = await self.run_message(self.attachment, session="group:one", user="alice")
        self.assertFalse(original.stopped)
        self.assertEqual(self.plugin.recent[self.plugin._key(original)][1], self.attachment)
        with patch.object(self.plugin, "_job", side_effect=self.hold_job) as job:
            for user, session in (("bob", "group:one"), ("alice", "group:two")):
                other = self.event(user=user, session=session)
                self.assertIn("120秒", await self.plugin.immersive_translate(other))
                job.assert_not_called()
            response = await self.plugin.immersive_translate(self.event(session="group:one"))
            self.assertIn("已启动", response)
            self.assertIs(job.call_args.args[1], self.attachment)
        self.assertNotIn(self.plugin._key(original), self.plugin.recent)

    async def test_current_attachment_takes_precedence_over_recent(self):
        event = self.event(self.attachment)
        old = File("old.pdf", url="https://example.invalid/old.pdf")
        self.plugin.recent[self.plugin._key(event)] = (float("inf"), old)
        with patch.object(self.plugin, "_job", side_effect=self.hold_job) as job:
            self.assertIn("已启动", await self.plugin._launch(event))
            self.assertIs(job.call_args.args[1], self.attachment)

    async def test_expired_recent_attachment_is_not_reused(self):
        event = self.event()
        self.plugin.recent[self.plugin._key(event)] = (0, self.attachment)
        self.assertIn("120秒", await self.plugin._launch(event))
        self.assertFalse(self.plugin.jobs)
        self.assertFalse(self.plugin.recent)

    async def test_manual_mode_records_without_launching(self):
        self.plugin.config["auto_translate_attachment"] = False
        with patch.object(self.plugin, "_launch", new_callable=AsyncMock) as launch:
            event = await self.run_message(self.attachment)
        launch.assert_not_awaited()
        self.assertFalse(event.stopped)
        self.assertFalse(event.sent)
        self.assertIn(self.plugin._key(event), self.plugin.recent)

    async def test_auto_mode_launches_supported_attachment(self):
        with patch.object(self.plugin, "_launch", AsyncMock(return_value="started")) as launch:
            event = await self.run_message(self.attachment)
        launch.assert_awaited_once_with(event)
        self.assertTrue(event.stopped)
        self.assertEqual(event.sent, ["started"])

    async def test_pending_request_launches_even_when_auto_disabled(self):
        self.plugin.config["auto_translate_attachment"] = False
        request = await self.run_message(Plain("/沉浸翻译"))
        self.assertTrue(request.stopped)
        self.assertIn(self.plugin._key(request), self.plugin.pending)
        with patch.object(self.plugin, "_job", side_effect=self.hold_job):
            event = await self.run_message(self.attachment)
        self.assertTrue(event.stopped)
        self.assertIn("已启动", event.sent[0])
        self.assertNotIn(self.plugin._key(event), self.plugin.pending)

    async def test_tool_followup_preserves_requested_language(self):
        self.plugin.config["auto_translate_attachment"] = False
        event = self.event()
        self.assertIn("120秒", await self.plugin.immersive_translate(event, "ja"))
        with patch.object(self.plugin, "_job", side_effect=self.hold_job) as job:
            await self.run_message(self.attachment)
            self.assertEqual(job.call_args.args[2], "ja")
        self.assertNotIn(self.plugin._key(event), self.plugin.pending_languages)

    async def test_expired_group_pending_does_not_bypass_tool_mention(self):
        self.plugin.config["enable_group"] = True
        event = self.event(self.attachment, group="one", at=False)
        self.plugin.pending[self.plugin._key(event)] = 0
        result = await self.plugin.immersive_translate(event)
        self.assertIn("不允许", result)
        self.assertFalse(self.plugin.jobs)

    async def test_pending_expiry_and_other_sender_do_not_trigger_manual_mode(self):
        self.plugin.config["auto_translate_attachment"] = False
        self.plugin.pending[("private:alice", "alice")] = 0
        self.plugin.pending[("private:alice", "bob")] = float("inf")
        with patch.object(self.plugin, "_launch", new_callable=AsyncMock) as launch:
            event = await self.run_message(self.attachment)
        launch.assert_not_awaited()
        self.assertFalse(event.stopped)
        self.assertNotIn(("private:alice", "alice"), self.plugin.pending)

    async def test_pending_request_is_scoped_to_same_user_and_session(self):
        self.plugin.config["auto_translate_attachment"] = False
        requester = self.event()
        self.plugin.pending[self.plugin._key(requester)] = float("inf")
        with patch.object(self.plugin, "_launch", new_callable=AsyncMock) as launch:
            for user, session in (("bob", "private:alice"), ("alice", "other-session")):
                event = await self.run_message(self.attachment, user=user, session=session)
                self.assertFalse(event.stopped)
            launch.assert_not_awaited()
        self.assertIn(self.plugin._key(requester), self.plugin.pending)

    async def test_pending_and_recent_have_capacity_limits(self):
        self.plugin.pending = {(f"s{i}", "user"): float("inf") for i in range(256)}
        event = self.event()
        self.assertIn("过多", await self.plugin._launch(event))
        self.assertEqual(len(self.plugin.pending), 256)
        self.plugin.config["auto_translate_attachment"] = False
        self.plugin.recent = {
            (f"s{i}", "user"): (float("inf"), self.attachment) for i in range(256)
        }
        await self.plugin.on_message(self.event(self.attachment))
        self.assertEqual(len(self.plugin.recent), 256)

    async def test_existing_recent_slot_is_refreshed_when_cache_is_full(self):
        self.plugin.config["auto_translate_attachment"] = False
        event = self.event(self.attachment)
        key = self.plugin._key(event)
        old = File("old.pdf", url="https://example.invalid/old.pdf")
        self.plugin.recent = {(f"s{i}", "user"): (float("inf"), old) for i in range(255)}
        self.plugin.recent[key] = (float("inf"), old)
        await self.plugin.on_message(event)
        self.assertEqual(len(self.plugin.recent), 256)
        self.assertIs(self.plugin.recent[key][1], self.attachment)

    async def test_cancel_clears_only_requesting_users_pending_and_recent(self):
        key, other = ("private:alice", "alice"), ("private:alice", "bob")
        for current in (key, other):
            self.plugin.pending[current] = float("inf")
            self.plugin.recent[current] = (float("inf"), self.attachment)
        event = await self.run_message(Plain("/沉浸翻译 取消"))
        self.assertNotIn(key, self.plugin.pending)
        self.assertNotIn(key, self.plugin.recent)
        self.assertIn(other, self.plugin.pending)
        self.assertIn(other, self.plugin.recent)
        self.assertIn("已取消", event.sent[0])

    async def test_allowlist_is_enforced_for_messages_and_tools(self):
        self.plugin.config.update(allowed_users=[123], auto_translate_attachment=False)
        denied = await self.run_message(self.attachment)
        self.assertFalse(denied.stopped)
        self.assertFalse(self.plugin.recent)
        self.assertIn("不允许", await self.plugin.immersive_translate(denied))
        self.assertTrue(self.plugin._allowed(self.event(user="123")))

    async def test_groups_are_disabled_by_default_even_for_explicit_commands(self):
        event = await self.run_message(Plain("/沉浸翻译"), group="42")
        self.assertFalse(event.stopped)
        self.assertFalse(self.plugin.pending)
        self.assertIn("不允许", await self.plugin.immersive_translate(event))

    async def test_group_requires_mention_except_explicit_command_or_pending(self):
        self.plugin.config["enable_group"] = True
        event = self.event(self.attachment, group="42")
        self.assertFalse(self.plugin._allowed(event))
        self.assertTrue(self.plugin._allowed(event, explicit=True))
        self.assertTrue(self.plugin._allowed(self.event(group="42", at=True)))
        self.plugin.pending[self.plugin._key(event)] = float("inf")
        self.assertTrue(self.plugin._allowed(event))
        self.plugin.pending[self.plugin._key(event)] = 0
        denied = await self.run_message(self.attachment, group="42")
        self.assertFalse(denied.stopped)
        self.assertFalse(self.plugin.recent)

    async def test_group_permission_cannot_be_bypassed_by_pending_or_mention(self):
        self.plugin.config.update(enable_group=True, allowed_users=["bob"])
        event = self.event(group="42", at=True)
        self.plugin.pending[self.plugin._key(event)] = float("inf")
        self.assertFalse(self.plugin._allowed(event, explicit=True))

    async def test_group_can_disable_mention_requirement(self):
        self.plugin.config.update(enable_group=True, group_require_at=False)
        self.assertTrue(self.plugin._allowed(self.event(group="42")))

    async def test_multiple_attachments_do_not_start_job(self):
        self.assertIn(
            "一次只", await self.plugin._launch(self.event(self.attachment, Image(file="x")))
        )
        self.assertFalse(self.plugin.jobs)
        self.assertFalse(self.plugin.active_directories)

    async def test_supported_attachment_filter_is_case_insensitive(self):
        pdf, image = File("REPORT.PDF"), Image(file="anything")
        event = self.event(File("notes.txt"), Plain("text"), pdf, image, File("malware.exe"))
        self.assertEqual(self.plugin._attachments(event), [pdf, image])

    async def test_tool_has_no_arbitrary_source_path_argument(self):
        import inspect

        parameters = inspect.signature(self.plugin.immersive_translate).parameters
        self.assertEqual(list(parameters), ["event", "target_language"])
        self.assertIn(
            "120秒", await self.plugin.immersive_translate(self.event(Plain("C:/secret.pdf")))
        )

    async def test_invalid_language_does_not_create_job_or_consume_recent(self):
        event = self.event()
        self.plugin.recent[self.plugin._key(event)] = (float("inf"), self.attachment)
        for target in ("../../secret", "en\n--flag", "", "x" * 100):
            if not target:
                continue
            with self.subTest(target=target):
                self.assertIn("语言", await self.plugin._launch(event, target))
                self.assertFalse(self.plugin.jobs)
                self.assertIn(self.plugin._key(event), self.plugin.recent)
        self.assertEqual(list(self.plugin.root.iterdir()), [])

    async def test_single_global_job_no_queue_even_for_different_user(self):
        event, task = await self.launch_blocked_job()
        directory = next(iter(self.plugin.active_directories))
        result = await self.plugin._launch(self.event(self.attachment, user="bob", session="other"))
        self.assertIn("正在运行", result)
        self.assertEqual(self.plugin.jobs, {self.plugin._key(event): task})
        self.assertEqual(self.plugin.active_directories, {directory})

    async def test_cancel_before_job_coroutine_starts_cleans_registry_and_request(self):
        event, task = await self.launch_blocked_job()
        directory = next(iter(self.plugin.active_directories))
        request = directory / "request.json"
        request.write_text("secret", encoding="utf-8")
        task.cancel()  # No event-loop turn has allowed _job to enter its try/finally.
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)  # Flush the registered done callback, not a timing delay.
        self.assertTrue(task.cancelled())
        self.assertFalse(self.plugin.jobs)
        self.assertFalse(self.plugin.active_directories)
        self.assertFalse(request.exists())
        self.assertTrue(directory.is_dir())

    async def test_cancel_command_cancels_running_job_and_preserves_other_users_job(self):
        owner, task = await self.launch_blocked_job()
        await asyncio.sleep(0)
        other = await self.run_message(Plain("/沉浸翻译 取消"), user="bob")
        self.assertIn("已取消", other.sent[0])
        self.assertFalse(task.done())
        cancelled = await self.run_message(Plain("/沉浸翻译 取消"))
        self.assertTrue(task.cancelled())
        self.assertFalse(self.plugin.jobs)
        self.assertFalse(self.plugin.active_directories)
        self.assertIn("已取消", cancelled.sent[0])

    async def test_old_task_done_callback_does_not_remove_replacement_job(self):
        directory = self.plugin.root / ("job_" + "a" * 32)
        directory.mkdir()
        old = asyncio.create_task(asyncio.sleep(0))
        replacement = asyncio.create_task(asyncio.sleep(0))
        await asyncio.gather(old, replacement)
        key = self.plugin._key(self.event())
        self.plugin.jobs[key] = replacement
        self.plugin._task_done(key, directory, old)
        self.assertIs(self.plugin.jobs[key], replacement)
        self.plugin.jobs.clear()

    async def test_unload_cancels_tasks_and_cleaner_and_prevents_new_jobs(self):
        _, task = await self.launch_blocked_job()
        cleaner = asyncio.create_task(asyncio.Event().wait())
        self.plugin.cleaner = cleaner
        self.plugin.pending[("session", "user")] = float("inf")
        self.plugin.recent[("session", "user")] = (float("inf"), self.attachment)
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        self.assertTrue(cleaner.cancelled())
        self.assertFalse(self.plugin.jobs)
        self.assertFalse(self.plugin.active_directories)
        self.assertFalse(self.plugin.pending)
        self.assertFalse(self.plugin.recent)
        self.assertIn("卸载", await self.plugin._launch(self.event(self.attachment)))
        await self.plugin.terminate()  # Repeated unload is harmless.

    async def test_source_download_receives_size_and_private_network_policy(self):
        self.plugin.config.update(max_file_mb=3, allow_private_downloads=True)
        with patch.object(plugin_module, "download", new_callable=AsyncMock) as download:
            destination = await self.plugin._source(self.attachment, self.data)
        self.assertEqual(destination, self.data / "source.pdf")
        download.assert_awaited_once_with(self.attachment.url, destination, 3 * 1024 * 1024, True)

    async def test_source_reads_adapter_file_when_url_missing(self):
        source = self.data / "original.pdf"
        source.write_bytes(b"%PDF-test")
        component = File("original.PDF", file=str(source))
        destination = await self.plugin._source(component, self.plugin.root)
        component.get_file.assert_awaited_once_with(allow_return_url=True)
        self.assertEqual(destination.name, "source.pdf")
        self.assertEqual(destination.read_bytes(), b"%PDF-test")
        self.assertEqual(source.read_bytes(), b"%PDF-test")

    async def test_source_accepts_local_file_uri_and_image_file(self):
        source = self.data / "space name.png"
        source.write_bytes(b"image-data")
        for component in (Image(file=source.as_uri()), Image(file=str(source))):
            with self.subTest(source=component.file):
                destination = await self.plugin._source(component, self.plugin.root)
                self.assertEqual(destination.name, "source.png")
                self.assertEqual(destination.read_bytes(), b"image-data")

    async def test_source_rejects_missing_oversized_and_network_paths(self):
        for source in (
            None,
            123,
            "",
            "base64://payload",
            "//host/share/file.pdf",
            "\\\\host\\share\\file.pdf",
            "file://remotehost/share/file.pdf",
            str(self.data / "missing.pdf"),
        ):
            with self.subTest(source=source):
                with self.assertRaises(TranslationError):
                    await self.plugin._source(Image(file=source), self.plugin.root)
        self.plugin.config["max_file_mb"] = 1
        oversized = self.data / "large.pdf"
        oversized.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaises(TranslationError):
            await self.plugin._source(File(file=str(oversized)), self.plugin.root)
        self.assertFalse((self.plugin.root / "source.pdf").exists())

    async def test_source_accepts_file_at_exact_size_limit(self):
        self.plugin.config["max_file_mb"] = 1
        source = self.data / "exact.pdf"
        source.write_bytes(b"x" * (1024 * 1024))
        destination = await self.plugin._source(File(file=str(source)), self.plugin.root)
        self.assertEqual(destination.stat().st_size, 1024 * 1024)

    async def test_source_download_uses_bounded_defaults(self):
        self.plugin.config["max_file_mb"] = 999999
        with patch.object(plugin_module, "download", new_callable=AsyncMock) as download:
            await self.plugin._source(self.attachment, self.data)
        self.assertEqual(download.await_args.args[2:], (100 * 1024 * 1024, False))

    async def test_source_rechecks_size_after_copy_to_close_growth_race(self):
        source = self.data / "small.pdf"
        source.write_bytes(b"tiny")
        self.plugin.config["max_file_mb"] = 1

        def growing_copy(original, destination):
            Path(destination).write_bytes(b"x" * (1024 * 1024 + 1))

        with patch.object(plugin_module.shutil, "copyfile", side_effect=growing_copy):
            with self.assertRaises(TranslationError):
                await self.plugin._source(File(file=str(source)), self.plugin.root)

    async def test_source_unsupported_file_does_not_download(self):
        with patch.object(plugin_module, "download", new_callable=AsyncMock) as download:
            with self.assertRaises(TranslationError):
                await self.plugin._source(
                    File("script.exe", url="https://example.invalid/x"), self.data
                )
        download.assert_not_awaited()

    def make_bridge(self):
        bridge = types.SimpleNamespace(url="http://unused.invalid/v1", token="test-secret", calls=7)
        manager = Mock()
        manager.__aenter__ = AsyncMock(return_value=bridge)
        manager.__aexit__ = AsyncMock(return_value=False)
        return manager

    async def run_real_job(self, *, result=None, error=None):
        directory = self.plugin.root / ("job_" + "b" * 32)
        directory.mkdir(exist_ok=True)
        event = self.event(self.attachment)
        source = directory / "source.pdf"
        source.write_bytes(b"%PDF-original")
        manager = self.make_bridge()
        captured = {}

        async def engine(*args):
            captured.update(json.loads((directory / "request.json").read_text("utf-8")))
            if error:
                raise error
            return result if result is not None else {"outputs": []}

        self.plugin.active_directories.add(directory)
        with (
            patch.object(self.plugin, "_source", AsyncMock(return_value=source)),
            patch.object(plugin_module, "ModelBridge", return_value=manager),
            patch.object(plugin_module, "run_engine", side_effect=engine),
            patch.object(plugin_module, "python_path", return_value="not-executed"),
        ):
            await self.plugin._job(event, self.attachment, "ja", directory)
        self.assertFalse((directory / "request.json").exists())
        self.assertNotIn(directory, self.plugin.active_directories)
        return event, directory, captured

    async def test_job_success_returns_only_valid_pdf_and_report(self):
        directory = self.plugin.root / ("job_" + "b" * 32)
        directory.mkdir()
        output = directory / "translated.pdf"
        output.write_bytes(b"%PDF-result")
        event, directory, request = await self.run_real_job(
            result={
                "outputs": [{"path": str(output), "kind": "dual"}],
                "warnings": ["review tables"],
            }
        )
        self.assertEqual(request["lang_out"], "ja")
        self.assertEqual(request["model"], "astrbot")
        self.assertEqual(request["api_key"], "test-secret")
        self.assertEqual(Path(request["source"]).parent, directory.resolve())
        self.assertEqual(event.sent[0][0].file, str(output.resolve()))
        self.assertEqual(event.sent[1][0].name, "翻译报告.json")
        self.assertIn("翻译完成", event.sent[2])
        report = json.loads((directory / "report.json").read_text("utf-8"))
        self.assertEqual(report["model_bridge_requests"], 7)
        self.assertNotIn("test-secret", json.dumps(report))

    async def test_job_rejects_output_outside_directory(self):
        outside = self.data / "outside.pdf"
        outside.write_bytes(b"secret")
        event, _, _ = await self.run_real_job(result={"outputs": [{"path": str(outside)}]})
        self.assertEqual(len(event.sent), 1)
        self.assertIn("输出路径无效", event.sent[0])
        self.assertFalse(any(isinstance(item, list) for item in event.sent))

    async def test_job_rejects_non_pdf_output(self):
        directory = self.plugin.root / ("job_" + "b" * 32)
        directory.mkdir()
        output = directory / "output.txt"
        output.write_text("not a PDF", encoding="utf-8")
        event, _, _ = await self.run_real_job(result={"outputs": [{"path": str(output)}]})
        self.assertIn("输出路径无效", event.sent[0])

    async def test_job_empty_outputs_reports_error(self):
        event, _, _ = await self.run_real_job()
        self.assertIn("未生成译本", event.sent[0])

    async def test_job_translation_error_is_shown_and_credentials_removed(self):
        event, _, _ = await self.run_real_job(error=TranslationError("safe actionable error"))
        self.assertEqual(event.sent, ["safe actionable error"])

    async def test_job_unexpected_error_does_not_leak_exception_secrets(self):
        with self.assertLogs("immersive.tests", level="ERROR"):
            event, _, _ = await self.run_real_job(error=RuntimeError("private-api-key"))
        self.assertIn("翻译或附件发送失败", event.sent[0])
        self.assertNotIn("private-api-key", repr(event.sent))

    async def test_job_timeout_reports_and_cleans_request(self):
        event, _, _ = await self.run_real_job(error=TimeoutError())
        self.assertIn("超过总时限", event.sent[0])

    async def test_job_cancel_propagates_after_cleaning_request(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.run_real_job(error=asyncio.CancelledError())
        directory = self.plugin.root / ("job_" + "b" * 32)
        self.assertFalse((directory / "request.json").exists())
        self.assertNotIn(directory, self.plugin.active_directories)

    async def test_job_missing_provider_reports_actionable_error(self):
        self.context.get_current_chat_provider_id.return_value = None
        directory = self.plugin.root / ("job_" + "c" * 32)
        directory.mkdir()
        event = self.event()
        with patch.object(self.plugin, "_source", AsyncMock(return_value=directory / "source.pdf")):
            await self.plugin._job(event, self.attachment, "zh", directory)
        self.assertIn("未配置文本模型", event.sent[0])

    async def test_status_reports_engine_error_without_starting_job(self):
        with (
            patch.object(plugin_module, "python_path", return_value="not-executed"),
            patch.object(
                plugin_module,
                "check_engine",
                AsyncMock(side_effect=TranslationError("engine missing")),
            ),
        ):
            event = await self.run_message(Plain("/沉浸翻译 状态"))
        self.assertEqual(event.sent, ["engine missing"])
        self.assertFalse(self.plugin.jobs)

    async def test_status_does_not_launch_concurrent_engine_checks(self):
        await self.plugin.check_lock.acquire()
        try:
            with patch.object(plugin_module, "check_engine", new_callable=AsyncMock) as check:
                event = await self.run_message(Plain("/沉浸翻译 状态"))
                check.assert_not_awaited()
        finally:
            self.plugin.check_lock.release()
        self.assertIn("自检正在进行", event.sent[0])


class IntegerTests(unittest.TestCase):
    def test_config_bounds_and_invalid_value_fallback(self):
        for value, expected in ((0, 1), (101, 100), ("5", 5), (None, 20), ("bad", 20)):
            with self.subTest(value=value):
                self.assertEqual(
                    plugin_module.integer({"value": value}, "value", 20, 1, 100), expected
                )


if __name__ == "__main__":
    unittest.main()
