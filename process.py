"""Kill the whole engine process tree on cancellation, not just its wrapper."""

import asyncio
import os
import signal
import sys
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
        "请先在插件配置中填写独立引擎的 engine_python 绝对路径，并按 README 安装引擎依赖。"
    )


def engine_environment(state: Path) -> dict:
    """Keep third-party config/models/text caches out of the host user profile."""
    state = state.resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = os.environ.copy()
    env["HOME"] = env["USERPROFILE"] = str(state)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    return env


async def kill_tree(process):
    if process.returncode is not None:
        return
    if sys.platform == "win32":
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
    else:
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
        process = await asyncio.create_subprocess_exec(
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

    process = await asyncio.create_subprocess_exec(
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
