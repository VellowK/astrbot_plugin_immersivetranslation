"""Process configuration and environment boundaries, no engine dependency."""

import asyncio
import importlib.util
import json
import os
import signal
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("_translation_process_tests")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(package.__name__ + ".process", ROOT / "process.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    def test_python_requires_explicit_absolute_file(self):
        for value in ("", "python", "relative/python", "does-not-exist"):
            with self.subTest(value=value), self.assertRaises(module.TranslationError):
                module.python_path({"engine_python": value})
        self.assertEqual(module.python_path({"engine_python": sys.executable}), sys.executable)

    def test_engine_home_is_private_child_environment_only(self):
        before = os.environ.copy()
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(
                os.environ,
                {
                    # Windows folds these keys; each value must contain both
                    # bypass entries so this fixture works on every platform.
                    "NO_PROXY": "internal.example,private.example,127.0.0.1",
                    "no_proxy": "internal.example,private.example,127.0.0.1",
                    "PYTHONPATH": "host-path",
                    "PYTHONHOME": "host-home",
                    "PYTHONUSERBASE": "host-user",
                    "VIRTUAL_ENV": "host-venv",
                    "__PYVENV_LAUNCHER__": "host-launcher",
                    "PIP_INDEX_URL": "https://index.example/simple",
                },
            ),
        ):
            state = Path(temp) / "state"
            env = module.engine_environment(state)
            self.assertTrue(state.is_dir())
            self.assertEqual(env["HOME"], str(state.resolve()))
            self.assertEqual(env["USERPROFILE"], str(state.resolve()))
            for key in ("TMPDIR", "TMP", "TEMP"):
                self.assertEqual(Path(env[key]), state.resolve() / "tmp")
                self.assertTrue(Path(env[key]).is_dir())
            self.assertEqual(
                env["NO_PROXY"], "127.0.0.1,localhost,internal.example,private.example"
            )
            self.assertEqual(env["no_proxy"], env["NO_PROXY"])
            self.assertEqual(env["PYTHONNOUSERSITE"], "1")
            self.assertEqual(env["PIP_INDEX_URL"], "https://index.example/simple")
            for name in (
                "PYTHONPATH",
                "PYTHONHOME",
                "PYTHONUSERBASE",
                "VIRTUAL_ENV",
                "__PYVENV_LAUNCHER__",
            ):
                self.assertNotIn(name, env)
        self.assertEqual(dict(os.environ), dict(before))

    async def test_status_passes_home_and_cleans_process(self):
        process = types.SimpleNamespace(communicate=AsyncMock(return_value=(b'{"ok": true}', None)))
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(module, "spawn_process", AsyncMock(return_value=process)) as spawn,
            patch.object(module, "kill_tree", AsyncMock()) as kill,
        ):
            state = Path(temp) / "home"
            self.assertEqual(await module.check_engine(sys.executable, state), {"ok": True})
            self.assertEqual(spawn.call_args.kwargs["env"]["HOME"], str(state.resolve()))
            kill.assert_awaited_once_with(process)

    async def test_status_invalid_json_and_oversize_are_safe(self):
        for output in (b"not-json", b"x" * 16385):
            process = types.SimpleNamespace(communicate=AsyncMock(return_value=(output, None)))
            with (
                self.subTest(size=len(output)),
                patch.object(module, "spawn_process", AsyncMock(return_value=process)),
                patch.object(module, "kill_tree", AsyncMock()) as kill,
            ):
                with self.assertRaises(module.TranslationError):
                    await module.check_engine(sys.executable)
                kill.assert_awaited_once_with(process)

    async def test_status_cancellation_reaps_process(self):
        process = types.SimpleNamespace(communicate=AsyncMock(side_effect=asyncio.CancelledError))
        with (
            patch.object(module, "spawn_process", AsyncMock(return_value=process)),
            patch.object(module, "kill_tree", AsyncMock()) as kill,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await module.check_engine(sys.executable)
            kill.assert_awaited_once_with(process)

    async def test_store_aliased_manual_venv_refused_before_spawn(self):
        with tempfile.TemporaryDirectory() as temp:
            venv = Path(temp)
            python = venv / "Scripts" / "python.exe"
            python.parent.mkdir()
            python.touch()
            config = venv / "pyvenv.cfg"
            original = "home = C:/Users/example/AppData/Local/Microsoft/WindowsApps/PythonSoftwareFoundation.Python.3.11\n"
            config.write_text(original, "utf-8")
            with (
                patch.object(module.sys, "platform", "win32"),
                patch.object(module.asyncio, "create_subprocess_exec", AsyncMock()) as spawn,
            ):
                with self.assertRaisesRegex(module.TranslationError, "商店执行别名"):
                    await module.spawn_process(str(python), "-c", "raise AssertionError")
                spawn.assert_not_awaited()
            self.assertEqual(config.read_text("utf-8"), original)
            config.write_text("home = C:/Program Files/WindowsApps/PythonPackage\n", "utf-8")
            module._check_windows_command(str(python))

    def test_direct_store_alias_refused(self):
        with self.assertRaisesRegex(module.TranslationError, "商店执行别名"):
            module._check_windows_command(
                "C:/Users/example/AppData/Local/Microsoft/WindowsApps/python.exe"
            )

    async def test_posix_exited_leader_still_kills_process_group(self):
        child = types.SimpleNamespace(pid=12345, returncode=0, wait=AsyncMock(), kill=AsyncMock())
        with (
            patch.object(module.sys, "platform", "linux"),
            patch.object(module.os, "killpg", create=True) as killpg,
            patch.object(module.signal, "SIGKILL", 9, create=True),
        ):
            await module.kill_tree(child)
        killpg.assert_called_once_with(child.pid, 9)
        child.kill.assert_not_called()
        child.wait.assert_awaited_once()


# A real root starts a real descendant, then exits. The descendant continues
# heartbeating and has its own 30s ceiling, so even a broken test cannot leak it
# indefinitely. Stdout is DEVNULL so asyncio.wait() does not wait on child pipes.
# These two roots use the physical Windows base: the standard venv redirector
# has its own exit-time cleanup, which would kill the descendant before this
# regression exercises OUR retained Job. Venv identity is tested separately.
_DESCENDANT = """
import os, sys, time
from pathlib import Path
root = Path(sys.argv[1])
(root / 'child.pid').write_text(str(os.getpid()))
end = time.monotonic() + 30
while time.monotonic() < end:
    (root / 'heartbeat').write_text(str(time.monotonic_ns()))
    time.sleep(0.025)
"""
_PARENT = """
import subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[1])
subprocess.Popen([sys.executable, '-c', sys.argv[2], str(root)])
end = time.monotonic() + 10
while not (root / 'heartbeat').exists():
    if time.monotonic() > end:
        sys.exit(2)
    time.sleep(0.01)
"""


class RealProcessTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), 10)

    async def _assert_descendant_alive(self, root):
        heartbeat = root / "heartbeat"
        previous = heartbeat.read_text()
        await self._wait_for(lambda: bool(value := heartbeat.read_text()) and value != previous)

    def _assert_descendant_owned(self, root, child):
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.OpenJobObjectW.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        kernel.IsProcessInJob.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel.OpenJobObjectW(4, False, child._translation_job.name)
        process = kernel.OpenProcess(0x1000, False, int((root / "child.pid").read_text()))
        try:
            self.assertTrue(job)
            self.assertTrue(process)
            owned = wintypes.BOOL()
            self.assertTrue(kernel.IsProcessInJob(process, job, ctypes.byref(owned)))
            self.assertTrue(owned.value, "Live descendant escaped its ownership job")
        finally:
            if process:
                kernel.CloseHandle(process)
            if job:
                kernel.CloseHandle(job)

    async def _assert_descendant_dead(self, root):
        # Windows exposes a true process-signalled check, including the case
        # where the PID still exists briefly after termination.
        pid = int((root / "child.pid").read_text())
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
            if handle:
                try:
                    await self._wait_for(lambda: kernel.WaitForSingleObject(handle, 0) == 0)
                finally:
                    kernel.CloseHandle(handle)
        # Also portable to POSIX, where an adopted zombie can still pass kill(0).
        await asyncio.sleep(0.1)
        heartbeat = (root / "heartbeat").read_text()
        await asyncio.sleep(0.2)
        self.assertEqual((root / "heartbeat").read_text(), heartbeat)

    async def _emergency_cleanup(self, root, child=None):
        # Run even if assertions fail; the descendant's own timeout is a final
        # backstop, not the test's ordinary cleanup strategy.
        if child is not None:
            await module.kill_tree(child)
        pid_path = root / "child.pid"
        if not pid_path.exists():
            return
        pid = int(pid_path.read_text())
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(1, False, pid)
            if handle:
                try:
                    kernel.TerminateProcess(handle, 1)
                finally:
                    kernel.CloseHandle(handle)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def test_cancellation_kills_descendant_after_root_exits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            child = None
            sleeping = asyncio.Event()

            async def run():
                nonlocal child
                child = await module.spawn_process(
                    str(Path(sys.base_prefix) / "python.exe")
                    if sys.platform == "win32"
                    else sys.executable,
                    "-c",
                    _PARENT,
                    str(root),
                    _DESCENDANT,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(child.wait(), 10)
                    self.assertEqual(child.returncode, 0)
                    sleeping.set()
                    await asyncio.Event().wait()
                finally:
                    await module.kill_tree(child)

            task = asyncio.create_task(run())
            try:
                await asyncio.wait_for(sleeping.wait(), 10)
                await self._assert_descendant_alive(root)
                self._assert_descendant_owned(root, child)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await self._assert_descendant_dead(root)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await self._emergency_cleanup(root, child)

    async def test_spawn_handoff_cancellation_reaps_real_exited_tree(self):
        real_spawn = asyncio.create_subprocess_exec
        created = asyncio.Event()
        release = asyncio.Event()
        child = None
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            async def delayed_spawn(*args, **kwargs):
                nonlocal child
                child = await real_spawn(*args, **kwargs)
                # Deliberately hold the Process back after the root exits, so
                # the spawn helper, not its caller, must retain/clean ownership.
                await asyncio.wait_for(child.wait(), 10)
                created.set()
                await release.wait()
                return child

            with patch.object(module.asyncio, "create_subprocess_exec", delayed_spawn):
                task = asyncio.create_task(
                    module.spawn_process(
                        str(Path(sys.base_prefix) / "python.exe")
                        if sys.platform == "win32"
                        else sys.executable,
                        "-c",
                        _PARENT,
                        str(root),
                        _DESCENDANT,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                )
                try:
                    await asyncio.wait_for(created.wait(), 10)
                    self.assertEqual(child.returncode, 0)
                    await self._assert_descendant_alive(root)
                    task.cancel()
                    await asyncio.sleep(0)
                    task.cancel()  # A second cancel must not interrupt cleanup.
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 10)
                    await self._assert_descendant_dead(root)
                finally:
                    release.set()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await self._emergency_cleanup(root, child)

    @unittest.skipUnless(sys.platform == "win32", "Windows ownership barrier")
    async def test_windows_closed_job_never_executes_payload(self):
        real_spawn = asyncio.create_subprocess_exec
        real_job = module._WindowsJob
        jobs = []

        def make_job():
            job = real_job()
            jobs.append(job)
            return job

        async def spawn_after_close(*args, **kwargs):
            jobs[-1].close()  # Simulate ownership loss before launcher assignment.
            return await real_spawn(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "must-not-exist"
            child = None
            try:
                with (
                    patch.object(module, "_WindowsJob", make_job),
                    patch.object(module.asyncio, "create_subprocess_exec", spawn_after_close),
                ):
                    child = await module.spawn_process(
                        sys.executable,
                        "-c",
                        "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                        str(marker),
                    )
                    await asyncio.wait_for(child.wait(), 10)
                self.assertEqual(child.returncode, 125)
                self.assertFalse(marker.exists())
            finally:
                if child is not None:
                    await module.kill_tree(child)
                for job in jobs:
                    job.close()

    async def test_original_venv_interpreter_arguments_and_stdio_are_preserved(self):
        import venv

        with tempfile.TemporaryDirectory(prefix="translation venv ") as temp:
            environment = Path(temp) / "selected venv"
            venv.EnvBuilder(with_pip=False).create(environment)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            child = await module.spawn_process(
                str(python),
                "-c",
                "import json,sys; print(json.dumps([sys.executable,sys.prefix,sys.argv[1:],input()]))",
                "argument with spaces",
                "中文参数",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                output, errors = await asyncio.wait_for(child.communicate(b"hello\n"), 10)
                self.assertEqual(child.returncode, 0, errors)
                executable, prefix, arguments, received = json.loads(output)
                self.assertEqual(Path(executable), python)
                self.assertEqual(Path(prefix), environment)
                self.assertEqual(arguments, ["argument with spaces", "中文参数"])
                self.assertEqual(received, "hello")
            finally:
                await module.kill_tree(child)
