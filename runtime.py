"""On-demand, isolated engine setup. Importing or describing never installs anything.

The OS lock file is deliberately never removed: unlinking a locked inode permits a
second installer to lock a different inode. Kernel locks are released on crashes.
The venv stays at its final path (entry-point shebangs contain absolute paths).
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import platform
import sys
import tempfile
import weakref
from contextlib import asynccontextmanager
from pathlib import Path

from . import process
from .errors import TranslationError

_REQUIREMENTS = Path(__file__).resolve().parent
_LOG_LIMIT = 1024 * 1024
_OUTPUT_LIMIT = 16384
_LOCK_POLL = 0.1
_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_OWNER = "astrbot_plugin_immersivetranslation/runtime-v1"


def _setup_timeout(config: dict) -> int:
    try:
        value = config.get("engine_setup_timeout", 1800)
        if isinstance(value, bool):
            raise ValueError
        return max(60, min(7200, int(value)))
    except (TypeError, ValueError, OverflowError):
        raise TranslationError("engine_setup_timeout 必须是秒数（60–7200，默认 1800）。") from None


def _private_directory(path: Path) -> None:
    if path.is_symlink() or path.resolve() != path.absolute():
        raise TranslationError("自动引擎目录不能是外部链接，请管理员检查插件数据目录。")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _read_json(path: Path) -> dict:
    try:
        if path.is_symlink() or path.stat().st_size > 16384:
            return {}
        value = json.loads(path.read_text("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_json(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".runtime-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@asynccontextmanager
async def _file_lock(path: Path):
    """Nonblocking advisory locking; cancellation never leaves a waiter thread."""
    if path.is_symlink():
        raise TranslationError("自动引擎锁文件无效，请管理员检查插件数据目录。")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            # msvcrt locks a byte range; keep a persistent byte in the file.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
        while not locked:
            try:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                await asyncio.sleep(_LOCK_POLL)
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class EngineRuntime:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.directory = self.root / "runtime"
        self.venv = self.directory / "venv"
        self.python = self.venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.ready = self.directory / "ready.json"
        self.owner = self.directory / "owner.json"
        self._status = ""
        self._manual_ready = ""

    def _fingerprint(self, config: dict) -> str:
        base = Path(sys.executable)
        stat = base.stat()
        identity = {
            "schema": 2,
            "base": str(base.resolve()),
            "base_stat": [stat.st_size, stat.st_mtime_ns],
            "version": sys.version,
            "platform": sys.platform,
            "machine": platform.machine(),
            "implementation": sys.implementation.name,
            "ocr": config.get("ocr_mode", "off") == "auto",
        }
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode())
        for name in self._requirements(config):
            digest.update(name.name.encode())
            digest.update(name.read_bytes())
        return digest.hexdigest()

    @staticmethod
    def _requirements(config: dict) -> list[Path]:
        names = ["requirements-engine.txt"]
        if config.get("ocr_mode", "off") == "auto":
            names.append("requirements-engine-ocr.txt")
        return [_REQUIREMENTS / name for name in names]

    def describe(self, config: dict) -> str:
        """Only inspect small local markers; do not create directories or subprocesses."""
        manual = str(config.get("engine_python", "")).strip()
        if self._status:
            return self._status
        if manual:
            return (
                "手动引擎：已自检就绪。" if manual == self._manual_ready else "手动引擎：待自检。"
            )
        if not config.get("auto_install_engine", True):
            return "自动安装已关闭；请管理员启用 auto_install_engine 或配置 engine_python。"
        try:
            marker = _read_json(self.ready)
            if (
                marker.get("fingerprint") == self._fingerprint(config)
                and marker.get("python") == str(self.python)
                and self.python.is_file()
            ):
                return "自动引擎：已就绪（缓存状态，下次翻译前将自检）。"
        except (OSError, ValueError):
            pass
        return "自动引擎：尚未就绪；下次翻译将自动准备独立环境。"

    async def _stage(self, text: str, notify) -> None:
        self._status = text
        if notify is not None:
            try:
                await notify(text)
            except Exception:
                # Notification transport errors must neither leak nor break setup.
                pass

    async def ensure(self, config: dict, notify=None) -> str:
        """Return a checked interpreter. The whole wait/setup has its own deadline."""
        try:
            timeout = _setup_timeout(config)
            self._status = "引擎环境：等待独立环境检查。"
            return await asyncio.wait_for(self._ensure(dict(config), notify), timeout)
        except asyncio.CancelledError:
            self._status = "引擎准备已取消；下次明确发起翻译时可重试。"
            raise
        except asyncio.TimeoutError:
            self._status = (
                "引擎准备超时并已终止；请检查网络或调整 engine_setup_timeout，随后重新发起翻译。"
            )
            raise TranslationError(self._status) from None
        except TranslationError as exc:
            self._status = str(exc)
            raise
        except Exception:
            self._status = "引擎准备失败；请管理员检查本地权限、磁盘空间和引擎安装日志后重新翻译。"
            raise TranslationError(self._status) from None

    async def _ensure(self, config: dict, notify) -> str:
        if str(config.get("engine_python", "")).strip():
            await self._stage("手动引擎：正在自检（不会安装依赖）。", notify)
            try:
                python = process.python_path(config)
                report = await process.check_engine(python, self.root / "engine_home")
                if not isinstance(report, dict) or report.get("ok") is not True:
                    raise ValueError
            except asyncio.CancelledError:
                raise
            except Exception:
                self._manual_ready = ""
                raise TranslationError(
                    "手动引擎自检失败；请管理员检查 engine_python 及独立引擎依赖，插件不会修改该环境。"
                ) from None
            self._manual_ready = python
            self._status = ""
            return python
        if not config.get("auto_install_engine", True):
            raise TranslationError(
                "自动安装已关闭；请管理员启用 auto_install_engine，或配置已安装引擎的 engine_python 绝对路径。"
            )
        if not (3, 10) <= sys.version_info[:2] <= (3, 13):
            raise TranslationError(
                "自动引擎需要 AstrBot 使用 Python 3.10–3.13；请更换宿主 Python 或配置独立的 engine_python。"
            )
        if config.get("ocr_mode", "off") == "auto" and sys.version_info[:2] > (3, 12):
            raise TranslationError(
                "自动 OCR 需要 Python 3.10–3.12；请关闭 OCR、更换宿主 Python 或配置兼容的 engine_python。"
            )
        if sys.platform == "win32":
            try:
                process._check_windows_command(
                    getattr(sys, "_base_executable", None) or sys.executable
                )
            except TranslationError:
                raise TranslationError(
                    "Windows 商店执行别名不支持安全自动引擎；请用独立 Python 3.12 运行 AstrBot，"
                    "或配置由独立 Python 创建的 engine_python。"
                ) from None
        fingerprint = self._fingerprint(config)
        loop_locks = _LOCKS.setdefault(asyncio.get_running_loop(), {})
        lock = loop_locks.setdefault(str(self.directory), asyncio.Lock())
        async with lock:
            _private_directory(self.directory)
            await self._stage("自动引擎：等待安装锁（其他任务可能正在准备）。", notify)
            async with _file_lock(self.directory / "setup.lock"):
                return await self._managed(config, fingerprint, notify)

    def _claim_venv(self) -> None:
        expected = {"owner": _OWNER, "venv": str(self.venv)}
        if self.venv.is_symlink() or self.venv.resolve() != self.venv.absolute():
            raise TranslationError("自动引擎目录是外部链接；请管理员检查，插件不会修改它。")
        if self.venv.exists() and _read_json(self.owner) != expected:
            raise TranslationError(
                "自动引擎目录已有非本插件管理的环境；请管理员备份并检查，插件不会覆盖或删除它。"
            )
        if not self.venv.exists():
            _atomic_json(self.owner, expected)

    def _environment(self) -> dict:
        env = process.engine_environment(self.root / "engine_home")
        for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PIP_TARGET", "PIP_PREFIX"):
            env.pop(name, None)
        env["PYTHONNOUSERSITE"] = "1"
        env["PIP_USER"] = "0"
        env["PIP_REQUIRE_VIRTUALENV"] = "true"
        env["VIRTUAL_ENV"] = str(self.venv)
        # Preserve administrator exclusions as well as loopback bridge bypasses.
        bypass = [
            os.environ.get("NO_PROXY", ""),
            os.environ.get("no_proxy", ""),
            "127.0.0.1,localhost",
        ]
        env["NO_PROXY"] = env["no_proxy"] = ",".join(value for value in bypass if value)
        return env

    async def _managed(self, config: dict, fingerprint: str, notify) -> str:
        self._claim_venv()
        marker = _read_json(self.ready)
        cached = (
            marker.get("fingerprint") == fingerprint
            and marker.get("python") == str(self.python)
            and self.python.is_file()
        )
        try:
            if not cached:
                self.ready.unlink(missing_ok=True)
                if (self.venv / "pyvenv.cfg").is_file():
                    self._check_venv_config()
                # stdlib creates python + pyvenv.cfg BEFORE ensurepip. Their
                # existence does not prove bootstrap succeeded: cancellation can
                # leave pip absent. Rerun non-destructive venv bootstrap for every
                # uncached setup; never --clear, rename or delete the owned venv.
                await self._stage(
                    "自动引擎：正在创建或修复独立 Python 环境（不会修改宿主依赖）。", notify
                )
                # Never use a Store App Execution Alias as the venv home: its
                # broker can launch descendants outside our Windows Job. The
                # physical base interpreter also avoids nested-venv misdetection.
                # This is the current Python's base, never a PATH-selected Python.
                if sys.platform == "win32":
                    bootstrap = str(Path(sys.base_prefix) / "python.exe")
                else:
                    bootstrap = getattr(sys, "_base_executable", None) or sys.executable
                await self._run([bootstrap, "-m", "venv", str(self.venv)], "venv")
                self._check_venv_config()
                _private_directory(self.directory / "pip-cache")
                await self._stage(
                    "自动引擎：正在下载并安装依赖，首次准备可能需要较长时间。", notify
                )
                requirements = self._requirements(config)[-1]
                await self._run(
                    [
                        str(self.python),
                        "-m",
                        "pip",
                        "--disable-pip-version-check",
                        "--no-input",
                        "--retries",
                        "0",
                        "install",
                        "--require-virtualenv",
                        "--prefix",
                        str(self.venv),
                        "--cache-dir",
                        str(self.directory / "pip-cache"),
                        "-r",
                        str(requirements),
                    ],
                    "pip",
                )
            await self._stage("自动引擎：正在验证解释器及引擎依赖。", notify)
            self._check_venv_config()
            await self._validate(config)
            _atomic_json(self.ready, {"fingerprint": fingerprint, "python": str(self.python)})
        except BaseException:
            # Invalidate even a previously cached environment after failed/cancelled checks.
            self.ready.unlink(missing_ok=True)
            raise
        self._status = ""
        return str(self.python)

    def _check_venv_config(self) -> None:
        cfg = self.venv / "pyvenv.cfg"
        try:
            if cfg.is_symlink() or cfg.stat().st_size > 16384:
                raise ValueError
            values = {
                key.strip().lower(): value.strip().lower()
                for line in cfg.read_text("utf-8").splitlines()
                if "=" in line
                for key, value in [line.split("=", 1)]
            }
            if values.get("include-system-site-packages") != "false":
                raise ValueError
        except (OSError, ValueError):
            raise TranslationError(
                "独立环境隔离配置无效；请管理员检查 runtime 目录，插件不会使用宿主 site-packages。"
            ) from None

    async def _validate(self, config: dict) -> None:
        # Metadata plus imports are checked without constructing OCR/downloading models.
        script = (
            "import importlib,sys,json; from pathlib import Path; from importlib.metadata import version; "
            "assert Path(sys.prefix).resolve()==Path(sys.argv[1]).resolve(); "
            "assert sys.prefix!=sys.base_prefix; "
            "assert (3,10)<=sys.version_info[:2]<=(3,13); "
            "assert version('pdf2zh-next')=='2.9.0'; "
        )
        if config.get("ocr_mode", "off") == "auto":
            script += (
                "assert sys.version_info[:2]<=(3,12); "
                "assert version('rapidocr-onnxruntime')=='1.4.4'; "
                "importlib.import_module('rapidocr_onnxruntime'); "
            )
        script += "print(json.dumps({'ok':True}))"
        output = await self._run([str(self.python), "-c", script, str(self.venv)], "check", True)
        try:
            if json.loads(output).get("ok") is not True:
                raise ValueError
            report = await process.check_engine(str(self.python), self.root / "engine_home")
            if not isinstance(report, dict) or report.get("ok") is not True:
                raise ValueError
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TranslationError(
                "引擎依赖自检失败；请管理员检查本地安装日志及 Python/系统架构兼容性，再重新翻译。"
            ) from None

    async def _run(self, args: list[str], phase: str, capture: bool = False) -> bytes:
        """Drain both streams continuously; cap retained output and private disk log."""
        log_path = self.directory / "setup.log"
        if log_path.is_symlink():
            raise TranslationError("引擎安装日志路径无效，请管理员检查插件数据目录。")
        fd = os.open(
            log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        output = bytearray()
        signatures: set[str] = set()
        written = 0
        child = None
        with os.fdopen(fd, "wb") as log:
            os.chmod(log_path, 0o600)

            async def drain(stream, collect=False):
                nonlocal written
                while chunk := await stream.read(8192):
                    lower = chunk.lower()
                    for token in (
                        b"ensurepip",
                        b"no module named venv",
                        b"no module named pip",
                        b"no matching distribution",
                        b"could not find a version",
                        b"failed building wheel",
                        b"connection",
                        b"proxy",
                        b"timed out",
                        b"certificate",
                        b"network",
                        b"name resolution",
                    ):
                        if token in lower:
                            signatures.add(token.decode())
                    if written < _LOG_LIMIT:
                        block = chunk[: _LOG_LIMIT - written]
                        log.write(block)
                        written += len(block)
                    if collect:
                        output.extend(chunk[: max(0, _OUTPUT_LIMIT + 1 - len(output))])

            readers = []
            try:
                # Shield creation so cancellation in the spawn handoff cannot orphan
                # a process that exists but has not yet been returned to this task.
                spawning = asyncio.create_task(
                    process.spawn_process(
                        *args,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=self._environment(),
                        start_new_session=sys.platform != "win32",
                    )
                )
                try:
                    child = await asyncio.shield(spawning)
                except asyncio.CancelledError:
                    try:
                        child = await spawning
                    except Exception:
                        pass
                    raise
                readers = [
                    asyncio.create_task(drain(child.stdout, capture)),
                    asyncio.create_task(drain(child.stderr)),
                    asyncio.create_task(child.wait()),
                ]
                await asyncio.gather(*readers)
                if child.returncode != 0 or len(output) > _OUTPUT_LIMIT:
                    raise TranslationError(self._failure(phase, signatures))
                return bytes(output)
            except OSError:
                raise TranslationError(self._failure(phase, signatures)) from None
            finally:
                try:
                    if child is not None:
                        await process.kill_tree(child)
                finally:
                    for task in readers:
                        if not task.done():
                            task.cancel()
                    if readers:
                        await asyncio.gather(*readers, return_exceptions=True)

    @staticmethod
    def _failure(phase: str, signatures: set[str]) -> str:
        if phase == "venv" or signatures.intersection(
            {"ensurepip", "no module named venv", "no module named pip"}
        ):
            return (
                "无法创建带 pip 的独立环境；请管理员为宿主 Python 安装 venv/ensurepip 支持，"
                "或配置已安装引擎的 engine_python；插件不会升级宿主包。"
            )
        if signatures.intersection(
            {"no matching distribution", "could not find a version", "failed building wheel"}
        ):
            return (
                "依赖没有适用的安装包或构建失败；请检查 Python 版本、系统架构及镜像源的 wheel 支持，"
                "也可配置兼容的 engine_python。"
            )
        if signatures.intersection(
            {"connection", "proxy", "timed out", "certificate", "network", "name resolution"}
        ):
            return (
                "引擎依赖下载失败；请管理员检查网络、代理、证书和 pip 镜像设置，然后重新发起翻译。"
            )
        return (
            "引擎依赖安装或自检失败；请管理员检查本地安装日志、网络和系统兼容性，然后重新发起翻译。"
        )
