"""可中断的并发执行工具。

在收到用户中断或任何异常退出时，主动取消排队任务且不强行等待未完成线程，
避免 Python 标准 ThreadPoolExecutor 在上下文退出时无限期 hang 住。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import TracebackType


class InterruptibleThreadPoolExecutor(ThreadPoolExecutor):
    """支持在异常或用户中断退出时立即取消排队任务且不强行等待的线程池。"""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        if exc_type is not None:
            # 发生异常（包括 KeyboardInterrupt / SIGINT）时，立即取消排队任务且不等待
            self.shutdown(wait=False, cancel_futures=True)
            return False
        self.shutdown(wait=True)
        return False
