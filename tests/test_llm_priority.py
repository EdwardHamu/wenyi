"""多 LLM 优先级调度、Pi 审计自动切换与静默期恢复测试。"""

from __future__ import annotations

import concurrent.futures
import json
import unittest
from unittest.mock import patch

from trans_novel.config import Config
from trans_novel.llm.base import RequestEvent
from trans_novel.llm.priority import PriorityLLMClient
from trans_novel.llm.providers.pi import PiAuditError
from trans_novel.llm.usage import UsageSample


class TestPriorityLLMClient(unittest.TestCase):
    def _create_two_fake_config(self) -> Config:
        return Config.from_dict(
            {
                "llm_priority": "01",
                "llm_list": [
                    {"provider": "fake"},
                    {"provider": "fake"},
                ],
            }
        )

    def _create_three_fake_config(self) -> Config:
        return Config.from_dict(
            {
                "llm_priority": "012",
                "llm_list": [
                    {"provider": "fake"},
                    {"provider": "fake"},
                    {"provider": "fake"},
                ],
            }
        )

    @patch("trans_novel.llm.priority._dispatch_audit_notifications")
    def test_immediate_retry_on_failover(self, mock_dispatch):
        config = self._create_two_fake_config()
        client = PriorityLLMClient(config)

        def c0_handler(messages, tier, json_mode):
            raise PiAuditError("内容安全审计拦截：触发规则")

        def c1_handler(messages, tier, json_mode):
            return "翻译成功结果"

        client._clients[0].handler = c0_handler
        client._clients[1].handler = c1_handler

        events = []
        client.set_event_sink(lambda ev, **kwargs: events.append((ev, kwargs)))

        res = client.complete([{"role": "user", "content": "hello"}], stage="Translator")
        self.assertEqual(res, "翻译成功结果")
        self.assertEqual(client.current_config_index, 1)

        # 检查是否发送了一次审计降级通知
        mock_dispatch.assert_called_once()
        # 检查事件账本是否记录了降级事件
        downgrades = [kw for ev, kw in events if ev == "llm_audit_downgrade"]
        self.assertEqual(len(downgrades), 1)
        self.assertEqual(downgrades[0]["from_index"], 0)
        self.assertEqual(downgrades[0]["to_index"], 1)

    @patch("trans_novel.llm.priority._dispatch_audit_notifications")
    def test_pi_502_status_error_immediately_uses_next_provider(self, mock_dispatch):
        config = Config.from_dict(
            {
                "llm_priority": "01",
                "llm_list": [
                    {
                        "provider": "pi",
                        "cli_path": "pi",
                        "max_retries": 3,
                        "tiers": {"strong": {"model": "m"}},
                    },
                    {"provider": "fake"},
                ],
            }
        )
        client = PriorityLLMClient(config)
        client._clients[1].handler = lambda m, t, j: "备用 provider 回复"
        call_count = 0

        def fake_run(argv, *, input_text=None, timeout=None, provider=None):
            nonlocal call_count
            call_count += 1

            class Result:
                returncode = 0
                stdout = json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [],
                            "stopReason": "error",
                            "errorMessage": "API Error: 502 status code (no body)",
                        },
                    }
                )
                stderr = ""

            return Result()

        with (
            patch("trans_novel.llm.providers.pi.run_cli_process", side_effect=fake_run),
            patch("trans_novel.llm.providers.pi._log_pi_error"),
        ):
            result = client.complete([{"role": "user", "content": "hello"}])

        self.assertEqual(result, "备用 provider 回复")
        self.assertEqual(call_count, 1)
        self.assertEqual(client.current_config_index, 1)
        mock_dispatch.assert_called_once()

    @patch("trans_novel.llm.priority._dispatch_audit_notifications")
    def test_ordinary_errors_do_not_switch(self, mock_dispatch):
        config = self._create_two_fake_config()
        client = PriorityLLMClient(config)

        def c0_handler(messages, tier, json_mode):
            raise RuntimeError("普通网络超时")

        client._clients[0].handler = c0_handler

        events = []
        client.set_event_sink(lambda ev, **kwargs: events.append((ev, kwargs)))

        with self.assertRaisesRegex(RuntimeError, "普通网络超时"):
            client.complete([{"role": "user", "content": "hello"}])

        self.assertEqual(client.current_config_index, 0)
        mock_dispatch.assert_not_called()
        self.assertEqual(len(events), 0)

    @patch("trans_novel.llm.priority._handle_audit_error")
    @patch("trans_novel.llm.priority._dispatch_audit_notifications")
    def test_lowest_priority_hard_exits(self, mock_dispatch, mock_handle):
        config = self._create_two_fake_config()
        client = PriorityLLMClient(config)

        # 模拟已经降级到 1 号（最低优先级）
        client._current_step = 1

        def c1_handler(messages, tier, json_mode):
            raise PiAuditError("最低优先级审计拦截")

        client._clients[1].handler = c1_handler

        with self.assertRaises(PiAuditError):
            client.complete([{"role": "user", "content": "hello"}])

        # 最低优先级调用 _handle_audit_error 硬退出
        mock_handle.assert_called_once()
        self.assertEqual(mock_dispatch.call_count, 0)

    @patch("trans_novel.llm.priority._dispatch_audit_notifications")
    def test_concurrent_audits_only_downgrade_once(self, mock_dispatch):
        config = self._create_two_fake_config()
        client = PriorityLLMClient(config)

        call_counts = {0: 0, 1: 0}

        def c0_handler(messages, tier, json_mode):
            call_counts[0] += 1
            raise PiAuditError("并发审计拦截")

        def c1_handler(messages, tier, json_mode):
            call_counts[1] += 1
            return "并发成功"

        client._clients[0].handler = c0_handler
        client._clients[1].handler = c1_handler

        events = []
        client.set_event_sink(lambda ev, **kwargs: events.append((ev, kwargs)))

        threads_count = 8
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads_count) as executor:
            futures = [
                executor.submit(client.complete, [{"role": "user", "content": f"msg {i}"}])
                for i in range(threads_count)
            ]
            results = [f.result() for f in futures]

        self.assertEqual(results, ["并发成功"] * threads_count)
        self.assertEqual(client.current_config_index, 1)
        # 仅触发 1 次降级通知
        self.assertEqual(mock_dispatch.call_count, 1)
        downgrades = [kw for ev, kw in events if ev == "llm_audit_downgrade"]
        self.assertEqual(len(downgrades), 1)

    @patch("trans_novel.llm.priority._dispatch_audit_notifications")
    def test_notification_failure_still_switches(self, mock_dispatch):
        mock_dispatch.side_effect = RuntimeError("网络阻断导致告警发送失败")

        config = self._create_two_fake_config()
        client = PriorityLLMClient(config)

        client._clients[0].handler = lambda m, t, j: (_ for _ in ()).throw(PiAuditError("审计拦截"))
        client._clients[1].handler = lambda m, t, j: "告警失败依然切换"

        res = client.complete([{"role": "user", "content": "test"}])
        self.assertEqual(res, "告警失败依然切换")
        self.assertEqual(client.current_config_index, 1)

    @patch("trans_novel.llm.priority._dispatch_audit_notifications")
    def test_quiet_period_recovery_and_reset_on_re_audit(self, mock_dispatch):
        config = self._create_three_fake_config()
        client = PriorityLLMClient(config)

        # 0 号初始审计失败
        client._clients[0].handler = lambda m, t, j: (_ for _ in ()).throw(
            PiAuditError("0号审计拦截")
        )
        client._clients[1].handler = lambda m, t, j: "1号回复"
        client._clients[2].handler = lambda m, t, j: "2号回复"

        events = []
        client.set_event_sink(lambda ev, **kwargs: events.append((ev, kwargs)))

        # 第一次请求：从 0 降级到 1
        res = client.complete([{"role": "user", "content": "r1"}])
        self.assertEqual(res, "1号回复")
        self.assertEqual(client.current_config_index, 1)

        # 未满 600 秒：保持在 1
        client._last_audit_time -= 590.0
        res = client.complete([{"role": "user", "content": "r2"}])
        self.assertEqual(res, "1号回复")
        self.assertEqual(client.current_config_index, 1)

        # 在 1 号再次触发审计：降级到 2 号，并重置恢复计时
        client._clients[1].handler = lambda m, t, j: (_ for _ in ()).throw(
            PiAuditError("1号审计拦截")
        )
        res = client.complete([{"role": "user", "content": "r3"}])
        self.assertEqual(res, "2号回复")
        self.assertEqual(client.current_config_index, 2)

        # 重置后过去了 590 秒（未满 600 秒）：仍保持在 2 号
        client._last_audit_time -= 590.0
        res = client.complete([{"role": "user", "content": "r4"}])
        self.assertEqual(res, "2号回复")
        self.assertEqual(client.current_config_index, 2)

        # 满 600 秒后发起新请求：恢复到最高优先级 0 号
        client._last_audit_time -= 20.0  # 累计 >= 610 秒
        # 让 0 号恢复正常工作
        client._clients[0].handler = lambda m, t, j: "0号恢复后回复"
        res = client.complete([{"role": "user", "content": "r5"}])
        self.assertEqual(res, "0号恢复后回复")
        self.assertEqual(client.current_config_index, 0)

        recoveries = [kw for ev, kw in events if ev == "llm_quiet_period_recovery"]
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(recoveries[0]["from_index"], 2)
        self.assertEqual(recoveries[0]["to_index"], 0)

    def test_preflight_validates_all_backup_configs(self):
        config = Config.from_dict(
            {
                "llm_priority": "01",
                "llm_list": [
                    {"provider": "fake"},
                    {
                        "provider": "deepseek",
                        "api_key_env": "NON_EXISTENT_BACKUP_KEY_12345",
                    },
                ],
            }
        )
        client = PriorityLLMClient(config)
        with self.assertRaisesRegex(RuntimeError, "NON_EXISTENT_BACKUP_KEY_12345"):
            client.validate_credentials()

    def test_status_listener_tracks_config_index(self):
        config = self._create_two_fake_config()
        client = PriorityLLMClient(config)

        events: list[RequestEvent] = []
        client.set_status_listener(events.append)

        # 0 号执行
        client._clients[0].handler = lambda m, t, j: "ok0"
        client.complete([{"role": "user", "content": "test"}])
        self.assertTrue(all(e.config_index == 0 for e in events))

        # 降级到 1 号
        client._clients[0].handler = lambda m, t, j: (_ for _ in ()).throw(PiAuditError("审计"))
        client._clients[1].handler = lambda m, t, j: "ok1"
        with patch("trans_novel.llm.priority._dispatch_audit_notifications"):
            client.complete([{"role": "user", "content": "test"}])

        # 降级后的新事件 config_index 变为 1
        c1_events = [e for e in events if e.config_index == 1]
        self.assertTrue(len(c1_events) > 0)

    def test_json_diagnostics_retention(self):
        config = self._create_two_fake_config()
        client = PriorityLLMClient(config)

        client._clients[0].handler = lambda m, t, j: "not a valid json output"

        with self.assertRaises(Exception):
            client.complete_json([{"role": "user", "content": "json pls"}])

        self.assertEqual(client.last_json_response(), "not a valid json output")

    def test_token_usage_aggregated_across_configs(self):
        config = self._create_two_fake_config()
        client = PriorityLLMClient(config)

        # 客户端 0 消耗 token
        client._clients[0].usage.record(
            "strong",
            UsageSample(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            stage="Translator",
        )
        # 客户端 1 消耗 token
        client._clients[1].usage.record(
            "cheap",
            UsageSample(prompt_tokens=200, completion_tokens=80, total_tokens=280),
            stage="Reviewer",
        )

        summary = client.usage_summary()
        self.assertEqual(summary["totals"]["calls"], 2)
        self.assertEqual(summary["totals"]["prompt_tokens"], 300)
        self.assertEqual(summary["totals"]["completion_tokens"], 130)
        self.assertEqual(summary["totals"]["total_tokens"], 430)
        self.assertIn("strong", summary["by_tier"])
        self.assertIn("cheap", summary["by_tier"])
        self.assertIn("Translator", summary["by_stage"])
        self.assertIn("Reviewer", summary["by_stage"])


if __name__ == "__main__":
    unittest.main()
