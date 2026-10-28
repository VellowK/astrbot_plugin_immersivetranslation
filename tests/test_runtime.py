"""Offline runtime lifecycle tests; subprocesses and dependency checks are doubles."""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("_translation_runtime_tests")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(package.__name__ + ".runtime", ROOT / "runtime.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", code=0, gate=None):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stderr.feed_data(stderr)
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.code = code
        self.returncode = None
        self.gate = gate
        self.killed = False

    async def wait(self):
        if self.gate is not None:
            await self.gate.wait()
        self.returncode = self.code
        return self.code


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "plugin_data"
        self.runtime = module.EngineRuntime(self.root)
        self.requirements = Path(self.temp.name) / "requirements"
        self.requirements.mkdir()
        (self.requirements / "requirements-engine.txt").write_text("pdf2zh-next==2.9.0\n", "utf-8")
        (self.requirements / "requirements-engine-ocr.txt").write_text(
            "-r requirements-engine.txt\nrapidocr-onnxruntime==1.4.4\n", "utf-8"
        )
        self.calls = []
        self.processes = []
        self.fail_phase = None
        self.fail_output = b"secret-token=https://user:password@example.invalid/private"
        self.pip_gate = None
        self.pip_started = asyncio.Event()
        self.check = AsyncMock(return_value={"ok": True})
        self.spawn = AsyncMock(side_effect=self.fake_spawn)
        self.kill = AsyncMock(side_effect=self.fake_kill)
        self.enterContext(patch.object(module, "_REQUIREMENTS", self.requirements))
        self.enterContext(patch.object(module.sys, "version_info", (3, 12, 0)))
        self.enterContext(patch.object(module.process, "spawn_process", self.spawn))
        self.enterContext(patch.object(module.process, "check_engine", self.check))
        self.enterContext(patch.object(module.process, "kill_tree", self.kill))

    async def fake_spawn(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        phase = "venv" if args[1:3] == ("-m", "venv") else "pip" if "pip" in args else "check"
        if phase == "venv" and self.fail_phase != "venv":
            python = self.runtime.python
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"fake python")
            (self.runtime.venv / "pyvenv.cfg").write_text(
                "home = fake\ninclude-system-site-packages = false\n", "utf-8"
            )
        failed = self.fail_phase == phase
        child = FakeProcess(
            stdout=b'{"ok": true}\n' if phase == "check" else b"installing\n",
            stderr=self.fail_output if failed else b"",
            code=1 if failed else 0,
            gate=self.pip_gate if phase == "pip" else None,
        )
        self.processes.append(child)
        if phase == "pip":
            self.pip_started.set()
        return child

    @staticmethod
    async def fake_kill(child):
        if child.returncode is None:
            child.killed = True
            child.code = -9
            if child.gate is not None:
                child.gate.set()
            await child.wait()

    def count_phase(self, phase):
        if phase == "venv":
            return sum(args[1:3] == ("-m", "venv") for args, _ in self.calls)
        return sum(phase in args for args, _ in self.calls)

    async def test_describe_is_read_only_and_hides_manual_paths(self):
        self.assertIn("尚未就绪", self.runtime.describe({}))
        self.assertIn(
            "待自检", self.runtime.describe({"engine_python": "https://secret.invalid/token"})
        )
        self.assertNotIn(
            "secret", self.runtime.describe({"engine_python": "https://secret.invalid/token"})
        )
        self.assertFalse(self.root.exists())
        self.spawn.assert_not_called()
        self.check.assert_not_called()

    async def test_default_install_and_cached_fast_path_still_validate(self):
        notify = AsyncMock()
        python = await self.runtime.ensure({}, notify)
        self.assertEqual(python, str(self.runtime.python))
        self.assertEqual(self.count_phase("venv"), 1)
        self.assertEqual(self.count_phase("pip"), 1)
        self.assertEqual(
            self.calls[0][0],
            (
                (
                    str(Path(sys.base_prefix) / "python.exe")
                    if sys.platform == "win32"
                    else getattr(sys, "_base_executable", None) or sys.executable
                ),
                "-m",
                "venv",
                str(self.runtime.venv),
            ),
        )
        args, kwargs = next(call for call in self.calls if "pip" in call[0])
        self.assertEqual(args[0], python)
        self.assertIn("--require-virtualenv", args)
        self.assertEqual(args[args.index("--prefix") + 1], str(self.runtime.venv))
        self.assertEqual(
            args[args.index("--cache-dir") + 1], str(self.runtime.directory / "pip-cache")
        )
        self.assertEqual(args[-1], str(self.requirements / "requirements-engine.txt"))
        self.assertNotIn("--system-site-packages", args)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["start_new_session"], sys.platform != "win32")
        self.assertGreaterEqual(notify.await_count, 4)
        self.assertIn("已就绪", self.runtime.describe({}))
        self.assertIn("缓存", module.EngineRuntime(self.root).describe({}))
        marker = json.loads(self.runtime.ready.read_text("utf-8"))
        self.assertEqual(len(marker["fingerprint"]), 64)
        await self.runtime.ensure({})
        self.assertEqual(self.count_phase("venv"), 1)
        self.assertEqual(self.count_phase("pip"), 1)
        self.assertEqual(self.count_phase("-c"), 2)
        self.assertEqual(self.check.await_count, 2)
        self.assertTrue(all(child.returncode is not None for child in self.processes))

    async def test_store_alias_host_fails_before_install_with_actionable_error(self):
        with (
            patch.object(module.sys, "platform", "win32"),
            patch.object(
                module.sys,
                "_base_executable",
                "C:/Users/example/AppData/Local/Microsoft/WindowsApps/python.exe",
            ),
        ):
            with self.assertRaisesRegex(module.TranslationError, "商店执行别名"):
                await self.runtime.ensure({})
        self.spawn.assert_not_awaited()
        self.assertFalse(self.runtime.directory.exists())

    async def test_bootstrap_uses_host_base_executable_and_preserves_spaces(self):
        base = str(Path(self.temp.name) / "base Python" / "python")
        with (
            patch.object(module.sys, "platform", "linux"),
            patch.object(module.sys, "_base_executable", base),
        ):
            await self.runtime.ensure({})
        self.assertEqual(self.calls[0][0], (base, "-m", "venv", str(self.runtime.venv)))
        self.assertEqual(self.calls[1][0][0], str(self.runtime.python))

    async def test_bootstrap_without_base_executable_falls_back_to_host(self):
        with (
            patch.object(module.sys, "platform", "linux"),
            patch.object(module.sys, "_base_executable", None),
        ):
            await self.runtime.ensure({})
        self.assertEqual(self.calls[0][0], (sys.executable, "-m", "venv", str(self.runtime.venv)))

    async def test_windows_bootstrap_uses_physical_base(self):
        base = str(Path(self.temp.name) / "physical Python")
        with (
            patch.object(module.sys, "platform", "win32"),
            patch.object(module.sys, "base_prefix", base),
            patch.object(
                module.sys,
                "_base_executable",
                str(Path(self.temp.name) / "another base" / "python.exe"),
            ),
        ):
            await self.runtime.ensure({})
        self.assertEqual(
            self.calls[0][0],
            (str(Path(base) / "python.exe"), "-m", "venv", str(self.runtime.venv)),
        )

    async def test_requirements_bytes_invalidate_even_without_size_change(self):
        await self.runtime.ensure({})
        before = json.loads(self.runtime.ready.read_text("utf-8"))["fingerprint"]
        (self.requirements / "requirements-engine.txt").write_text("pdf2zh-next==2.9.1\n", "utf-8")
        self.assertIn("尚未就绪", self.runtime.describe({}))
        await self.runtime.ensure({})
        self.assertEqual(self.count_phase("pip"), 2)
        self.assertEqual(self.count_phase("venv"), 2)
        self.assertNotEqual(
            before, json.loads(self.runtime.ready.read_text("utf-8"))["fingerprint"]
        )

    async def test_ocr_enable_selects_optional_requirements_and_validates_metadata(self):
        await self.runtime.ensure({})
        await self.runtime.ensure({"ocr_mode": "auto"})
        self.assertEqual(self.count_phase("pip"), 2)
        self.assertEqual(self.count_phase("venv"), 2)
        pip_args = [args for args, _ in self.calls if "pip" in args][-1]
        self.assertEqual(pip_args[-1], str(self.requirements / "requirements-engine-ocr.txt"))
        script = [args[2] for args, _ in self.calls if "-c" in args][-1]
        self.assertIn("rapidocr-onnxruntime", script)
        self.assertIn("rapidocr_onnxruntime", script)
        self.assertIn("sys.prefix!=sys.base_prefix", script)
        (self.requirements / "requirements-engine-ocr.txt").write_text("# changed\n", "utf-8")
        self.assertIn("尚未就绪", self.runtime.describe({"ocr_mode": "auto"}))
        await self.runtime.ensure({"ocr_mode": "auto"})
        self.assertEqual(self.count_phase("pip"), 3)

    async def test_fingerprint_includes_base_identity_and_platform(self):
        before = self.runtime._fingerprint({})
        with patch.object(module.platform, "machine", return_value="different-architecture"):
            self.assertNotEqual(before, self.runtime._fingerprint({}))
        with patch.object(module.sys, "version", "different-base-version"):
            self.assertNotEqual(before, self.runtime._fingerprint({}))
        self.assertNotEqual(before, self.runtime._fingerprint({"ocr_mode": "auto"}))

    async def test_optout_actionable_and_never_creates_environment(self):
        with self.assertRaisesRegex(module.TranslationError, "auto_install_engine"):
            await self.runtime.ensure({"auto_install_engine": False})
        self.assertFalse(self.runtime.ready.exists())
        self.assertFalse(self.root.exists())
        self.spawn.assert_not_called()

    async def test_manual_always_uses_helpers_and_never_installs(self):
        config = {"engine_python": sys.executable, "auto_install_engine": False, "ocr_mode": "auto"}
        with patch.object(
            module.process, "python_path", wraps=module.process.python_path
        ) as resolve:
            self.assertEqual(await self.runtime.ensure(config), sys.executable)
            resolve.assert_called_once_with(config)
        self.check.assert_awaited_once_with(sys.executable, self.root / "engine_home")
        self.spawn.assert_not_called()
        self.assertFalse(self.runtime.ready.exists())
        self.assertIn("已自检就绪", self.runtime.describe(config))

    async def test_invalid_manual_path_and_report_never_expose_raw_error(self):
        for value in ("secret-password", sys.executable):
            self.check.return_value = {"ok": False, "errors": [self.fail_output.decode()]}
            with self.assertRaises(module.TranslationError) as caught:
                await self.runtime.ensure({"engine_python": value})
            self.assertNotIn("password", str(caught.exception))
            self.assertIn("不会修改", str(caught.exception))
        self.spawn.assert_not_called()

    def test_timeout_bounds_and_invalid_values(self):
        self.assertEqual(module._setup_timeout({}), 1800)
        for value, expected in ((1, 60), (60, 60), (99999, 7200), ("120", 120)):
            self.assertEqual(module._setup_timeout({"engine_setup_timeout": value}), expected)
        for value in (None, True, "secret-invalid", float("inf")):
            with self.assertRaises(module.TranslationError) as caught:
                module._setup_timeout({"engine_setup_timeout": value})
            self.assertNotIn("secret", str(caught.exception))

    async def test_schema_timeout_reaches_actual_setup_deadline(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text("utf-8"))
        key = "engine_setup_timeout"
        self.assertEqual(module._setup_timeout({key: schema[key]["default"]}), 1800)
        original = module.asyncio.wait_for
        with patch.object(module.asyncio, "wait_for", wraps=original) as deadline:
            await self.runtime.ensure({key: 91})
        self.assertEqual(deadline.call_args.args[1], 91)

    async def test_unsupported_python_and_ocr_fail_before_files_or_processes(self):
        for version, config in (((3, 9), {}), ((3, 14), {}), ((3, 13), {"ocr_mode": "auto"})):
            with patch.object(module.sys, "version_info", version):
                with self.assertRaisesRegex(module.TranslationError, "Python"):
                    await self.runtime.ensure(config)
        self.spawn.assert_not_called()
        self.assertFalse(self.root.exists())

    async def test_failed_pip_safe_error_no_readiness_and_explicit_retry(self):
        self.fail_phase = "pip"
        self.fail_output += b" proxy connection failed"
        with self.assertRaisesRegex(module.TranslationError, "网络") as caught:
            await self.runtime.ensure({})
        self.assertNotIn("password", str(caught.exception))
        self.assertNotIn("secret", self.runtime.describe({}))
        self.assertFalse(self.runtime.ready.exists())
        self.assertIn(b"password", (self.runtime.directory / "setup.log").read_bytes())
        self.fail_phase = None
        await self.runtime.ensure({})
        self.assertEqual(self.count_phase("pip"), 2)
        self.assertEqual(self.count_phase("venv"), 2)
        self.assertTrue(self.runtime.ready.exists())

    async def test_venv_and_architecture_diagnostics(self):
        self.fail_phase = "venv"
        self.fail_output = b"No module named ensurepip secret-password"
        with self.assertRaisesRegex(module.TranslationError, "venv/ensurepip"):
            await self.runtime.ensure({})
        self.assertFalse(self.runtime.ready.exists())
        self.fail_phase = "pip"
        self.fail_output = b"No matching distribution found for secret-password"
        with self.assertRaisesRegex(module.TranslationError, "架构") as caught:
            await self.runtime.ensure({})
        self.assertNotIn("secret", str(caught.exception))
        self.assertFalse(self.runtime.ready.exists())

    async def test_validation_failure_invalidates_cached_marker(self):
        await self.runtime.ensure({})
        self.check.return_value = {"ok": False, "errors": [self.fail_output.decode()]}
        with self.assertRaisesRegex(module.TranslationError, "自检失败") as caught:
            await self.runtime.ensure({})
        self.assertNotIn("secret", str(caught.exception))
        self.assertFalse(self.runtime.ready.exists())
        self.check.return_value = {"ok": True}
        await self.runtime.ensure({})
        self.assertEqual(self.count_phase("pip"), 2)

    async def test_metadata_subprocess_failure_never_marks_ready(self):
        self.fail_phase = "check"
        with self.assertRaises(module.TranslationError):
            await self.runtime.ensure({"ocr_mode": "auto"})
        self.assertFalse(self.runtime.ready.exists())
        self.check.assert_not_called()

    async def test_cancelled_setup_kills_tree_and_releases_locks(self):
        self.pip_gate = asyncio.Event()
        task = asyncio.create_task(self.runtime.ensure({}))
        await self.pip_started.wait()
        child = self.processes[-1]
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(child.killed)
        self.assertFalse(self.runtime.ready.exists())
        self.assertIn("取消", self.runtime.describe({}))
        self.pip_gate = None
        await self.runtime.ensure({})
        self.assertTrue(self.runtime.ready.exists())

    async def test_cancelled_spawn_handoff_does_not_orphan_child(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def delayed_spawn(*args, **kwargs):
            child = await self.fake_spawn(*args, **kwargs)
            started.set()
            await release.wait()
            return child

        self.spawn.side_effect = delayed_spawn
        task = asyncio.create_task(self.runtime.ensure({}))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.processes[0].killed)
        self.assertFalse(self.runtime.ready.exists())

    async def test_progress_is_safe_and_no_marker_exists_until_validated(self):
        self.pip_gate = asyncio.Event()
        task = asyncio.create_task(self.runtime.ensure({}))
        await self.pip_started.wait()
        self.assertIn("下载并安装", self.runtime.describe({}))
        self.assertNotIn("password", self.runtime.describe({}))
        self.assertFalse(self.runtime.ready.exists())
        self.pip_gate.set()
        await task
        self.assertTrue(self.runtime.ready.exists())

    async def test_crash_leftover_lock_and_partial_owned_venv_can_retry(self):
        self.runtime.directory.mkdir(parents=True)
        (self.runtime.directory / "setup.lock").write_bytes(b"old process crashed")
        self.runtime._claim_venv()
        self.runtime.venv.mkdir()
        (self.runtime.venv / "partial-install").write_bytes(b"preserve")
        await self.runtime.ensure({})
        self.assertTrue(self.runtime.ready.exists())
        self.assertEqual((self.runtime.venv / "partial-install").read_bytes(), b"preserve")
        self.assertEqual(self.count_phase("venv"), 1)

    async def test_interpreter_and_config_without_pip_are_repaired(self):
        # venv writes these before running ensurepip. Model a cancellation after
        # those writes but before pip is available, not just an empty directory.
        self.runtime.directory.mkdir(parents=True)
        self.runtime._claim_venv()
        self.runtime.python.parent.mkdir(parents=True)
        self.runtime.python.write_bytes(b"partial python")
        (self.runtime.venv / "pyvenv.cfg").write_text(
            "include-system-site-packages = false\n", "utf-8"
        )
        sentinel = self.runtime.venv / "keep-existing-data"
        sentinel.write_bytes(b"preserve")
        pip_available = False

        async def incomplete_bootstrap(*args, **kwargs):
            nonlocal pip_available
            if args[1:3] == ("-m", "venv"):
                pip_available = True
            if "pip" in args and not pip_available:
                return FakeProcess(stderr=b"No module named pip", code=1)
            return await self.fake_spawn(*args, **kwargs)

        self.spawn.side_effect = incomplete_bootstrap
        await self.runtime.ensure({})
        self.assertEqual(self.count_phase("venv"), 1)
        self.assertEqual(self.count_phase("pip"), 1)
        self.assertTrue(self.runtime.ready.is_file())
        self.assertEqual(sentinel.read_bytes(), b"preserve")

    async def test_corrupted_ready_marker_reinstalls_without_destroying_venv(self):
        await self.runtime.ensure({})
        self.runtime.ready.write_bytes(b"invalid-json")
        self.assertIn("尚未就绪", self.runtime.describe({}))
        await self.runtime.ensure({})
        self.assertEqual(self.count_phase("pip"), 2)
        self.assertEqual(self.count_phase("venv"), 2)

    async def test_cancelled_cached_validation_invalidates_readiness(self):
        await self.runtime.ensure({})
        checking = asyncio.Event()

        async def pending_check(*args):
            checking.set()
            await asyncio.Event().wait()

        self.check.side_effect = pending_check
        task = asyncio.create_task(self.runtime.ensure({}))
        await checking.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.runtime.ready.exists())

    async def test_missing_requirements_reports_safe_failure_without_install(self):
        (self.requirements / "requirements-engine.txt").unlink()
        with self.assertRaises(module.TranslationError) as caught:
            await self.runtime.ensure({})
        self.assertNotIn(str(self.temp.name), str(caught.exception))
        self.assertFalse(self.runtime.ready.exists())
        self.spawn.assert_not_called()

    async def test_timeout_kills_tree_and_no_ready_marker(self):
        self.pip_gate = asyncio.Event()
        original_wait_for = asyncio.wait_for

        async def expire_after_spawn(coro, timeout):
            task = asyncio.create_task(coro)
            await self.pip_started.wait()
            return await original_wait_for(task, 0.01)

        with patch.object(module.asyncio, "wait_for", side_effect=expire_after_spawn):
            with self.assertRaisesRegex(module.TranslationError, "超时"):
                await self.runtime.ensure({})
        self.assertTrue(any(child.killed for child in self.processes))
        self.assertFalse(self.runtime.ready.exists())
        self.pip_gate = None
        await self.runtime.ensure({})
        self.assertTrue(self.runtime.ready.exists())

    async def test_single_flight_across_runtime_instances(self):
        self.pip_gate = asyncio.Event()
        one = asyncio.create_task(self.runtime.ensure({}))
        await self.pip_started.wait()
        other = module.EngineRuntime(self.root)
        two = asyncio.create_task(other.ensure({}))
        await asyncio.sleep(0)
        self.assertEqual(self.count_phase("pip"), 1)
        self.pip_gate.set()
        self.assertEqual(await one, await two)
        self.assertEqual(self.count_phase("pip"), 1)
        self.assertEqual(self.check.await_count, 2)

    async def test_waiter_cancellation_does_not_invalidate_active_install(self):
        self.pip_gate = asyncio.Event()
        active = asyncio.create_task(self.runtime.ensure({}))
        await self.pip_started.wait()
        waiting = asyncio.create_task(module.EngineRuntime(self.root).ensure({}))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.pip_gate.set()
        await active
        self.assertTrue(self.runtime.ready.exists())
        self.assertEqual(self.count_phase("pip"), 1)

    async def test_os_lock_excludes_distinct_handles_and_recovers_after_cancel(self):
        self.runtime.directory.mkdir(parents=True)
        lock = self.runtime.directory / "setup.lock"
        entered = asyncio.Event()

        async def contender():
            async with module._file_lock(lock):
                entered.set()

        with patch.object(module, "_LOCK_POLL", 0.005):
            async with module._file_lock(lock):
                waiting = asyncio.create_task(contender())
                await asyncio.sleep(0.02)
                self.assertFalse(entered.is_set())
                waiting.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiting
                self.assertTrue(lock.exists())
            async with module._file_lock(lock):
                pass
        self.assertTrue(lock.exists())

    async def test_file_lock_excludes_a_distinct_python_process(self):
        self.runtime.directory.mkdir(parents=True)
        lock = self.runtime.directory / "setup.lock"
        script = """
import asyncio, importlib, sys, types
from pathlib import Path
package = types.ModuleType('_lock_probe')
package.__path__ = [sys.argv[1]]
sys.modules[package.__name__] = package
runtime = importlib.import_module('_lock_probe.runtime')
async def acquire():
    async with runtime._file_lock(Path(sys.argv[2])):
        return 'acquired'
async def main():
    try:
        print(await asyncio.wait_for(acquire(), 0.2))
    except asyncio.TimeoutError:
        print('blocked')
asyncio.run(main())
"""

        async def probe():
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-I", "-S", "-c", script, str(ROOT), str(lock)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        async with module._file_lock(lock):
            self.assertEqual(await probe(), "blocked")
        self.assertEqual(await probe(), "acquired")

    async def test_os_lock_wait_has_setup_deadline_and_can_be_reused(self):
        self.runtime.directory.mkdir(parents=True)
        async with module._file_lock(self.runtime.directory / "setup.lock"):
            with patch.object(module, "_setup_timeout", return_value=0.02):
                with self.assertRaisesRegex(module.TranslationError, "超时"):
                    await self.runtime.ensure({})
        self.spawn.assert_not_called()
        await self.runtime.ensure({})
        self.assertTrue(self.runtime.ready.exists())

    async def test_unmanaged_directory_is_not_destroyed_or_modified(self):
        self.runtime.venv.mkdir(parents=True)
        sentinel = self.runtime.venv / "do-not-delete.txt"
        sentinel.write_text("external data", "utf-8")
        with self.assertRaisesRegex(module.TranslationError, "不会覆盖或删除"):
            await self.runtime.ensure({})
        self.assertEqual(sentinel.read_text("utf-8"), "external data")
        self.spawn.assert_not_called()

    async def test_system_site_packages_is_rejected_without_pip(self):
        await self.runtime.ensure({})
        cfg = self.runtime.venv / "pyvenv.cfg"
        cfg.write_text("include-system-site-packages = true\n", "utf-8")
        with self.assertRaisesRegex(module.TranslationError, "隔离配置无效"):
            await self.runtime.ensure({})
        self.assertFalse(self.runtime.ready.exists())
        self.assertEqual(self.count_phase("pip"), 1)

    async def test_environment_isolated_but_admin_proxy_and_index_retained(self):
        settings = {
            "PYTHONPATH": "secret-injection",
            "PYTHONHOME": "secret-injection",
            "PYTHONUSERBASE": "secret-injection",
            "PIP_TARGET": "external-target",
            "PIP_PREFIX": "external-prefix",
            "PIP_USER": "1",
            "NO_PROXY": "admin.internal,other.internal",
            "no_proxy": "admin.internal,other.internal",
            "HTTPS_PROXY": "https://user:password@proxy.invalid",
            "PIP_INDEX_URL": "https://user:password@index.invalid/simple",
        }
        with patch.dict(os.environ, settings):
            before = dict(os.environ)
            env = self.runtime._environment()
            self.assertEqual(dict(os.environ), before)
        for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PIP_TARGET", "PIP_PREFIX"):
            self.assertNotIn(name, env)
        self.assertEqual(env["PIP_USER"], "0")
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertEqual(env["HTTPS_PROXY"], settings["HTTPS_PROXY"])
        self.assertEqual(env["PIP_INDEX_URL"], settings["PIP_INDEX_URL"])
        self.assertIn("admin.internal", env["NO_PROXY"])
        self.assertIn("other.internal", env["NO_PROXY"])
        self.assertIn("127.0.0.1,localhost", env["NO_PROXY"])
        self.assertEqual(env["HOME"], str(self.root / "engine_home"))

    async def test_local_log_bounded_and_private_and_capture_bounded(self):
        self.runtime.directory.mkdir(parents=True)
        child = FakeProcess(stdout=b"x" * (module._LOG_LIMIT * 2))
        self.spawn.side_effect = None
        self.spawn.return_value = child
        await self.runtime._run(["fake"], "pip")
        log = self.runtime.directory / "setup.log"
        self.assertEqual(log.stat().st_size, module._LOG_LIMIT)
        if os.name != "nt":
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        child = FakeProcess(stdout=b"x" * (module._OUTPUT_LIMIT + 1))
        self.spawn.return_value = child
        with self.assertRaises(module.TranslationError):
            await self.runtime._run(["fake"], "check", True)

    async def test_notification_failure_does_not_fail_install_or_leak(self):
        callback = AsyncMock(side_effect=RuntimeError("secret-password"))
        await self.runtime.ensure({}, callback)
        self.assertTrue(self.runtime.ready.exists())
        self.assertNotIn("secret", self.runtime.describe({}))

    async def test_unexpected_os_errors_are_safe(self):
        self.spawn.side_effect = OSError(self.fail_output.decode())
        with self.assertRaises(module.TranslationError) as caught:
            await self.runtime.ensure({})
        self.assertNotIn("password", str(caught.exception))
        self.assertFalse(self.runtime.ready.exists())

    async def test_atomic_marker_has_no_leftover_temporary_files(self):
        await self.runtime.ensure({})
        self.assertEqual(list(self.runtime.directory.glob(".runtime-*.tmp")), [])
        self.assertTrue(self.runtime.owner.is_file())
        self.assertTrue(self.runtime.ready.is_file())

    @unittest.skipIf(os.name == "nt", "Unprivileged Windows symlinks are unavailable")
    async def test_external_runtime_symlink_is_refused(self):
        self.root.mkdir()
        external = Path(self.temp.name) / "external"
        external.mkdir()
        self.runtime.directory.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(module.TranslationError, "外部链接"):
            await self.runtime.ensure({})
        self.assertEqual(list(external.iterdir()), [])
        self.spawn.assert_not_called()
