"""请求状态和 CLI 渲染的离线测试。"""

from __future__ import annotations

import unittest

from rich.progress import Progress

from trans_novel.cli import RequestStatusColumn
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.llm.status import (
    ERROR,
    REQUESTING,
    SUCCESS,
    TIMEOUT,
    RequestEvent,
    RequestStatusTracker,
    classify_error,
)


class TestRequestStatus(unittest.TestCase):
    def test_fake_client_success_reports_stage_and_status(self):
        events = []
        client = FakeClient()
        client.set_status_listener(events.append)

        self.assertEqual(
            client.complete([{"role": "user", "content": "x"}], stage="Translator"),
            "",
        )
        self.assertEqual([event.status for event in events], [REQUESTING, SUCCESS])
        self.assertEqual(events[0].stage, "Translator")
        self.assertEqual(events[-1].request_id, events[0].request_id)

    def test_failure_is_reported_and_re_raised(self):
        def fail(messages, tier, json_mode):
            raise RuntimeError("service unavailable")

        events = []
        client = FakeClient(handler=fail)
        client.set_status_listener(events.append)
        with self.assertRaisesRegex(RuntimeError, "service unavailable"):
            client.complete([{"role": "user", "content": "x"}], stage="Reviewer")

        self.assertEqual([event.status for event in events], [REQUESTING, ERROR])
        self.assertEqual(events[-1].error_type, "RuntimeError")
        self.assertEqual(events[-1].error_message, "service unavailable")

    def test_timeout_classification(self):
        self.assertEqual(classify_error(TimeoutError("slow")), TIMEOUT)

    def test_tracker_keeps_last_attempt_and_previous_request(self):
        tracker = RequestStatusTracker()
        tracker(RequestEvent(1, REQUESTING, "strong", "first", 1, 0.0))
        tracker(RequestEvent(1, TIMEOUT, "strong", "first", 1, 1.0, "TimeoutError", "slow"))
        tracker(RequestEvent(2, REQUESTING, "cheap", "second", 1, 0.0))
        tracker(RequestEvent(2, SUCCESS, "cheap", "second", 1, 0.2))
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot.current.status, SUCCESS)
        self.assertEqual(snapshot.previous.status, TIMEOUT)
        self.assertEqual(snapshot.active_count, 0)

    def test_status_column_contains_current_and_previous(self):
        tracker = RequestStatusTracker()
        tracker(RequestEvent(1, ERROR, "strong", "Translator", 1, 0.2, "RuntimeError", "bad"))
        tracker(RequestEvent(2, SUCCESS, "strong", "Polisher", 1, 0.1))
        column = RequestStatusColumn(tracker)
        with Progress() as progress:
            progress.add_task("x", total=None)
            rendered = column.render(progress.tasks[0])
        output = rendered.plain
        self.assertIn("当前请求：成功完成", output)
        self.assertIn("上一次请求：错误", output)
        self.assertIn("bad", output)


if __name__ == "__main__":
    unittest.main()
