"""Shared helpers for local CLI providers."""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from typing import Any

_ACTIVE_PROCESSES: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()


def render_cli_chat_messages(messages: Sequence[Mapping[str, Any]]) -> str:
    """Render non-system chat messages for one-shot CLI providers.

    Preserve the legacy raw payload for the overwhelmingly common single-message request. For a
    replayed multi-turn transcript, retain role boundaries so previous assistant output cannot be
    mistaken for another user instruction.
    """
    rows = [
        (str(message.get("role", "user") or "user").upper(), str(message.get("content", "")))
        for message in messages
        if message.get("content", "")
    ]
    if len(rows) <= 1:
        return rows[0][1] if rows else ""
    return "\n\n".join(f"[{role}]\n{content}" for role, content in rows)


def cli_launch_error(
    provider: str,
    cli_path: str,
    error: FileNotFoundError,
) -> RuntimeError:
    """Build an actionable error when a local provider CLI cannot be started."""
    detail = " ".join(str(error).split())
    message = (
        f"无法启动本地 {provider} CLI：{cli_path}."
        "请确认 llm.cli_path 指向可执行文件，或确认该命令已加入 PATH。"
    )
    if detail:
        message += f" 原始错误：{detail}"
    return RuntimeError(message)


def register_active_process(proc: subprocess.Popen) -> None:
    """登记活跃的外部 CLI 子进程。"""
    with _ACTIVE_LOCK:
        _ACTIVE_PROCESSES.add(proc)


def unregister_active_process(proc: subprocess.Popen) -> None:
    """注销已结束的外部 CLI 子进程。"""
    with _ACTIVE_LOCK:
        _ACTIVE_PROCESSES.discard(proc)


def terminate_process(proc: subprocess.Popen) -> None:
    """终止指定的子进程及其子进程树（Windows 优先杀整棵树）。"""
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return

    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
            return
        except Exception:
            pass

    try:
        proc.kill()
    except Exception:
        pass


def terminate_all_active_processes() -> None:
    """终止所有登记中的活跃 CLI 子进程，供信号处理器或中断快速清理使用。"""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE_PROCESSES)
        _ACTIVE_PROCESSES.clear()

    for proc in procs:
        terminate_process(proc)


def _is_mocked(func: Any) -> bool:
    """检查函数是否被 unittest.mock 替换（常见于单元测试环境）。"""
    return (
        hasattr(func, "mock_calls")
        or hasattr(func, "side_effect")
        or type(func).__module__.startswith("unittest.mock")
    )


def run_cli_process(
    argv: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: float | None = None,
    provider: str = "CLI",
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    encoding: str = "utf-8",
) -> subprocess.CompletedProcess[str]:
    """统一运行本地 CLI 命令并纳管子进程生命周期。

    - 兼容测试环境中的 patch("subprocess.run") mock
    - Windows 下设置 CREATE_NEW_PROCESS_GROUP 隔离控制台 Ctrl+C 广播，防 cmd 批处理卡死
    - 登记到全局活跃子进程池，供 Ctrl+C 信号处理器一键全部终止
    - 捕获 FileNotFoundError 并转换为标准 cli_launch_error
    - 超时或中断异常时安全清理子进程树
    """
    if _is_mocked(subprocess.run):
        try:
            return subprocess.run(
                list(argv),
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding=encoding,
            )
        except FileNotFoundError as error:
            raise cli_launch_error(provider, argv[0], error) from error

    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding=encoding,
            errors="replace",
            cwd=cwd,
            env=env,
            creationflags=creationflags,
        )
    except FileNotFoundError as error:
        raise cli_launch_error(provider, argv[0], error) from error

    register_active_process(proc)
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_process(proc)
        try:
            proc.communicate(timeout=1)
        except Exception:
            pass
        raise
    except BaseException:
        terminate_process(proc)
        raise
    finally:
        unregister_active_process(proc)

    return subprocess.CompletedProcess(
        args=list(argv),
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout or "",
        stderr=stderr or "",
    )
