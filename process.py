"""Kill the whole engine process tree on cancellation, not just its wrapper."""

import asyncio
import os
import signal
import sys
import uuid
import weakref
from pathlib import Path

from .errors import TranslationError


def python_path(config: dict) -> str:
    value = str(config.get("engine_python", "")).strip()
    if value:
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            raise TranslationError("engine_python 必须是已安装引擎的 Python 可执行文件绝对路径。")
        return str(path)
    raise TranslationError(
        "手动引擎模式需填写有效的 engine_python 绝对路径；留空并开启 auto_install_engine 可自动安装。"
    )


def engine_environment(state: Path) -> dict:
    """Keep third-party config/models/text caches out of the host user profile."""
    state = state.resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = os.environ.copy()
    env["HOME"] = env["USERPROFILE"] = str(state)
    temporary = state / "tmp"
    temporary.mkdir(parents=True, exist_ok=True, mode=0o700)
    for key in ("TMPDIR", "TMP", "TEMP"):
        env[key] = str(temporary)
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    # Keep administrator exemptions while ensuring the local model bridge bypasses proxies.
    exemptions = ["127.0.0.1", "localhost"]
    for key in ("NO_PROXY", "no_proxy"):
        for value in env.get(key, "").split(","):
            value = value.strip()
            if value and value not in exemptions:
                exemptions.append(value)
    env["NO_PROXY"] = env["no_proxy"] = ",".join(exemptions)
    return env


# The launcher contains no user imports/site initialization before assignment. Its
# short-lived job handle is closed BEFORE starting the original command; only this
# host owns the durable handle. Closing it also kills descendants of an exited root.
_WINDOWS_LAUNCHER = r"""
import ctypes, subprocess, sys
from ctypes import wintypes
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
k.OpenJobObjectW.restype = wintypes.HANDLE
k.GetCurrentProcess.restype = wintypes.HANDLE
k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
k.AssignProcessToJobObject.restype = wintypes.BOOL
k.CloseHandle.argtypes = [wintypes.HANDLE]
k.CloseHandle.restype = wintypes.BOOL
h = k.OpenJobObjectW(1, False, sys.argv[1])
if not h:
    sys.exit(125)
ok = k.AssignProcessToJobObject(h, k.GetCurrentProcess())
k.CloseHandle(h)
if not ok:
    sys.exit(125)
try:
    sys.exit(subprocess.call(sys.argv[2:]))
except OSError:
    sys.stderr.write('Unable to start isolated engine process.\n')
    sys.exit(126)
"""


class _WindowsJob:
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.name = "Local\\immersivetranslation-" + uuid.uuid4().hex
        handle = kernel.CreateJobObjectW(None, self.name)
        if not handle:
            raise OSError("Unable to create process ownership job")
        self._close = weakref.finalize(self, kernel.CloseHandle, handle)
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise OSError("Unable to configure process ownership job")

    def close(self):
        self._close()


async def _finish_cleanup(task):
    """Repeated cancellation must not interrupt a spawn/cleanup ownership handoff."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


def _check_windows_command(command) -> None:
    """Store aliases broker execution outside Jobs; refuse unsafe manual venvs."""
    path = Path(command)
    config = (
        path.parent.parent / "pyvenv.cfg"
        if path.parent.name.lower() == "scripts"
        else path.parent / "pyvenv.cfg"
    )
    try:
        values = [str(path).replace("\\", "/").lower()]
        if config.is_file():
            if config.stat().st_size > 16384:
                raise OSError
            values.extend(
                value.strip().replace("\\", "/").lower()
                for line in config.read_text("utf-8").splitlines()
                if "=" in line
                for key, value in [line.split("=", 1)]
                if key.strip().lower() == "home"
            )
        if any("/microsoft/windowsapps/" in value for value in values):
            raise TranslationError(
                "该 Windows Python 使用商店执行别名，无法安全取消其子进程；"
                "请使用独立 Python 3.12 的受管或手动引擎，不要使用 Windows 商店执行别名。"
            )
    except (OSError, UnicodeError):
        raise TranslationError("无法验证 Windows 引擎环境，请管理员检查其 pyvenv.cfg。") from None


async def spawn_process(*args, **kwargs):
    """Spawn an owned tree; always call ``await kill_tree(child)`` in a finally.

    Accepts create_subprocess_exec arguments (except Windows ``executable``);
    returns its usual Process with unchanged stdio/wait/communicate APIs. POSIX
    always creates a new session. Windows launches the original argv unchanged,
    using its requested interpreter, only after a stdlib launcher joins our Job.
    Cancellation during creation also reaps the process before propagating.
    Mock this helper, not asyncio.create_subprocess_exec, in caller unit tests.
    """
    if not args:
        raise ValueError("A process command is required")
    job = None
    if sys.platform == "win32":
        if "executable" in kwargs:
            raise ValueError("Windows owned processes do not support executable overrides")
        _check_windows_command(args[0])
        # Prefer the selected independent Python, not a possibly Store-aliased
        # AstrBot host. Its -I -S launcher still joins the Job before the payload.
        launcher = Path(args[0])
        if launcher.name.lower() not in {"python.exe", "pythonw.exe"}:
            launcher = Path(sys.base_prefix) / "python.exe"
            if not launcher.is_file():
                launcher = Path(sys.executable)
        job = _WindowsJob()
        args = (str(launcher), "-I", "-S", "-c", _WINDOWS_LAUNCHER, job.name, *args)
        kwargs.pop("start_new_session", None)
    else:
        kwargs["start_new_session"] = True

    async def create():
        try:
            child = await asyncio.create_subprocess_exec(*args, **kwargs)
            if job is not None:
                child._translation_job = job
            return child
        except BaseException:
            if job is not None:
                job.close()
            raise

    spawning = asyncio.create_task(create())
    try:
        return await asyncio.shield(spawning)
    except asyncio.CancelledError:
        try:
            child = await _finish_cleanup(spawning)
            await _finish_cleanup(asyncio.create_task(kill_tree(child)))
        except Exception:
            # Preserve cancellation; failed creation has already closed its job.
            pass
        raise


async def kill_tree(process):
    job = getattr(process, "_translation_job", None)
    if job is not None:
        job.close()
    elif sys.platform == "win32":
        # Legacy/raw Process compatibility. Only spawn_process can retain a tree
        # after its root exits; never taskkill an exited (possibly reused) PID.
        if process.returncode is None:
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            except OSError:
                pass  # Still reap the root if taskkill is unavailable.
    else:
        # A group outlives its leader. Never gate this on process.returncode.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


async def run_engine(python: str, directory: Path, bridge):
    with (directory / "engine.log").open("wb") as log:
        env = engine_environment(directory.parent.parent / "engine_home")
        process = await spawn_process(
            python,
            str(Path(__file__).resolve().with_name("engine_runner.py")),
            str(directory),
            stdout=log,
            stderr=log,
            env=env,
            start_new_session=sys.platform != "win32",
        )
        waiter = asyncio.create_task(process.wait())
        try:
            while process.returncode is None:
                if bridge.failures:
                    raise TranslationError("翻译模型调用失败或达到调用次数上限，已终止本次排版。")
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), 1)
                except asyncio.TimeoutError:
                    continue
        finally:
            await kill_tree(process)
            await waiter
    status = directory / "result.json"
    if not status.is_file() or status.stat().st_size > 256 * 1024:
        raise TranslationError("引擎未返回有效结果，请用 /沉浸翻译 状态 检查独立环境。")
    import json

    data = json.loads(status.read_text("utf-8"))
    if process.returncode != 0 or not data.get("ok"):
        raise TranslationError(data.get("error") or "文档翻译失败，原文件未改动。")
    return data


async def check_engine(python: str, state: Path | None = None) -> dict:
    import json

    process = await spawn_process(
        python,
        str(Path(__file__).resolve().with_name("engine_runner.py")),
        "--check",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=engine_environment(state) if state else None,
        start_new_session=sys.platform != "win32",
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 30)
        if len(output) > 16384:
            raise TranslationError("引擎自检输出过大。")
        return json.loads(output)
    except (ValueError, asyncio.TimeoutError) as exc:
        raise TranslationError(
            "引擎自检失败，请检查 engine_python 和 requirements-engine.txt。"
        ) from exc
    finally:
        await kill_tree(process)
