"""Process configuration and environment boundaries, no engine dependency."""

import importlib.util
import os
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
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            env = module.engine_environment(state)
            self.assertTrue(state.is_dir())
            self.assertEqual(env["HOME"], str(state.resolve()))
            self.assertEqual(env["USERPROFILE"], str(state.resolve()))
            self.assertEqual(env["NO_PROXY"], "127.0.0.1,localhost")
            self.assertEqual(env["no_proxy"], env["NO_PROXY"])
        self.assertEqual(dict(os.environ), dict(before))

    async def test_status_passes_home_and_cleans_process(self):
        process = types.SimpleNamespace(communicate=AsyncMock(return_value=(b'{"ok": true}', None)))
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(
                module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
            ) as spawn,
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
                patch.object(
                    module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
                ),
                patch.object(module, "kill_tree", AsyncMock()) as kill,
            ):
                with self.assertRaises(module.TranslationError):
                    await module.check_engine(sys.executable)
                kill.assert_awaited_once_with(process)

    async def test_status_cancellation_reaps_process(self):
        import asyncio

        process = types.SimpleNamespace(communicate=AsyncMock(side_effect=asyncio.CancelledError))
        with (
            patch.object(module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)),
            patch.object(module, "kill_tree", AsyncMock()) as kill,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await module.check_engine(sys.executable)
            kill.assert_awaited_once_with(process)
