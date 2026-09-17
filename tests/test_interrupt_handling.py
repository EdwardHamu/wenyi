"""测试 Ctrl+C 信号响应、进程清理与中断取消机制。"""

from __future__ import annotations

import signal
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from trans_novel.cli import _handle_interrupt_signal, install_signal_handlers
from trans_novel.llm.providers._cli import (
    _ACTIVE_PROCESSES,
    register_active_process,
    run_cli_process,
    terminate_all_active_processes,
    unregister_active_process,
)
from trans_novel.pipeline.executor import InterruptibleThreadPoolExecutor


class TestInterruptHandling(unittest.TestCase):
    def setUp(self):
        # 清空活跃进程池
        terminate_all_active_processes()

    def tearDown(self):
        terminate_all_active_processes()

    def test_active_process_registration_and_termination(self):
        mock_proc1 = MagicMock(spec=subprocess.Popen)
        mock_proc1.poll.return_value = None
        mock_proc1.pid = 12345

        mock_proc2 = MagicMock(spec=subprocess.Popen)
        mock_proc2.poll.return_value = None
        mock_proc2.pid = 12346

        register_active_process(mock_proc1)
        register_active_process(mock_proc2)

        self.assertIn(mock_proc1, _ACTIVE_PROCESSES)
        self.assertIn(mock_proc2, _ACTIVE_PROCESSES)

        unregister_active_process(mock_proc1)
        self.assertNotIn(mock_proc1, _ACTIVE_PROCESSES)
        self.assertIn(mock_proc2, _ACTIVE_PROCESSES)

        terminate_all_active_processes()
        self.assertEqual(len(_ACTIVE_PROCESSES), 0)
        self.assertTrue(mock_proc2.kill.called or mock_proc2.poll.called)

    def test_run_cli_process_file_not_found(self):
        with self.assertRaises(RuntimeError) as ctx:
            run_cli_process(["nonexistent_binary_xyz_12345"], provider="TestCLI")
        self.assertIn("无法启动本地 TestCLI CLI", str(ctx.exception))

    def test_run_cli_process_timeout_terminates_and_cleans_up(self):
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd=["test"], timeout=1)

        with patch("subprocess.Popen", return_value=mock_proc):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_cli_process(["test_cmd"], timeout=1)

        # 验证退出后进程已被从活跃池注销
        self.assertNotIn(mock_proc, _ACTIVE_PROCESSES)
        self.assertTrue(mock_proc.kill.called or mock_proc.poll.called)

    def test_run_cli_process_keyboard_interrupt_terminates(self):
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None
        mock_proc.pid = 99998
        mock_proc.communicate.side_effect = KeyboardInterrupt()

        with patch("subprocess.Popen", return_value=mock_proc):
            with self.assertRaises(KeyboardInterrupt):
                run_cli_process(["test_cmd"])

        self.assertNotIn(mock_proc, _ACTIVE_PROCESSES)
        self.assertTrue(mock_proc.kill.called or mock_proc.poll.called)

    def test_interruptible_thread_pool_executor_cancels_pending_tasks(self):
        executed_tasks = []

        def slow_task(x: int):
            executed_tasks.append(x)
            if x == 1:
                raise KeyboardInterrupt("Simulated Ctrl+C")
            return x

        with self.assertRaises(KeyboardInterrupt):
            with InterruptibleThreadPoolExecutor(max_workers=1) as ex:
                fut1 = ex.submit(slow_task, 1)
                fut2 = ex.submit(slow_task, 2)
                fut3 = ex.submit(slow_task, 3)
                fut1.result()

        # 任务 2 和任务 3 应该已被 cancel，不会被执行
        self.assertEqual(executed_tasks, [1])
        self.assertTrue(fut2.cancelled() or not fut2.running())
        self.assertTrue(fut3.cancelled() or not fut3.running())

    def test_signal_handler_invokes_cleanup_and_exit(self):
        mock_stderr = MagicMock()
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None
        mock_proc.pid = 88888
        register_active_process(mock_proc)

        with (
            patch("sys.stderr.write", mock_stderr.write),
            patch("sys.stderr.flush", mock_stderr.flush),
            patch("os._exit") as mock_exit,
            patch("trans_novel.cli._INTERRUPTED", False),
        ):
            _handle_interrupt_signal(signal.SIGINT, None)

            mock_exit.assert_called_once_with(130)
            self.assertTrue(mock_stderr.write.called)
            self.assertEqual(len(_ACTIVE_PROCESSES), 0)

    def test_install_signal_handlers_sets_sigint(self):
        with (
            patch.dict("os.environ", {"TRANS_NOVEL_FORCE_SIGNAL_HANDLERS": "1"}),
            patch("signal.signal") as mock_signal,
        ):
            install_signal_handlers()
            mock_signal.assert_any_call(signal.SIGINT, _handle_interrupt_signal)
            if hasattr(signal, "SIGBREAK"):
                mock_signal.assert_any_call(signal.SIGBREAK, _handle_interrupt_signal)
