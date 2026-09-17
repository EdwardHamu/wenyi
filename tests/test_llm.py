"""LLM 抽象层与 JSON 解析的测试（离线）。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from trans_novel.llm.json_parser import parse_json_loose, parse_json_result
from trans_novel.llm.providers.fake import FakeClient


class TestParseJsonLoose(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_json_loose('{"a":1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(parse_json_loose("```json\n[1,2,3]\n```"), [1, 2, 3])

    def test_surrounded_by_prose(self):
        text = '思考结束。结果如下：["译文1","译文2"] 完毕。'
        self.assertEqual(parse_json_loose(text), ["译文1", "译文2"])

    def test_failure(self):
        with self.assertRaises(ValueError):
            parse_json_loose("没有任何 JSON 内容")


class TestResolveTier(unittest.TestCase):
    def test_fallback_chain(self):
        from trans_novel.config import TierConfig
        from trans_novel.llm.tiers import resolve_tier

        strong = TierConfig(model="pro")
        cheap = TierConfig(model="flash")
        fast = TierConfig(model="flash", options={"thinking": False})

        # 三档全有 → 各归各
        tiers = {"strong": strong, "cheap": cheap, "fast": fast}
        self.assertIs(resolve_tier(tiers, "fast"), fast)
        self.assertIs(resolve_tier(tiers, "cheap"), cheap)
        self.assertIs(resolve_tier(tiers, "strong"), strong)
        # 无 fast → 落 cheap（不升到更贵的 strong）
        tiers2 = {"strong": strong, "cheap": cheap}
        self.assertIs(resolve_tier(tiers2, "fast"), cheap)
        # 只有 strong → 都落 strong
        tiers3 = {"strong": strong}
        self.assertIs(resolve_tier(tiers3, "fast"), strong)
        self.assertIs(resolve_tier(tiers3, "cheap"), strong)
        # 未知档 → 落 strong
        self.assertIs(resolve_tier(tiers, "unknown"), strong)


class TestFakeClient(unittest.TestCase):
    def test_default(self):
        c = FakeClient()
        self.assertEqual(c.complete([{"role": "user", "content": "x"}]), "")
        self.assertEqual(c.complete_json([{"role": "user", "content": "x"}]), [])

    def test_handler(self):
        def handler(messages, tier, json_mode):
            return '["A","B"]' if json_mode else "hello"

        c = FakeClient(handler=handler)
        self.assertEqual(c.complete([{"role": "user", "content": "x"}]), "hello")
        self.assertEqual(c.complete_json([{"role": "user", "content": "x"}]), ["A", "B"])
        self.assertEqual(len(c.calls), 2)


class TestParseJsonLooseRepairs(unittest.TestCase):
    def test_parse_result_reports_whether_repair_was_used(self):
        self.assertFalse(parse_json_result('{"a": 1}').repaired)
        repaired = parse_json_result('{"a": 1')
        self.assertTrue(repaired.repaired)
        self.assertEqual(repaired.value, {"a": 1})

    def test_inner_ascii_quotes_repaired(self):
        # 真实案例：claude-opus-4.6 经 OpenRouter 输出的译文含未转义英文引号
        raw = '{"translations":["磨到那份锱铢必较里暗含的"小气"二字无声地烫上面颊。"]}'
        got = parse_json_loose(raw)
        self.assertEqual(
            got["translations"][0], '磨到那份锱铢必较里暗含的"小气"二字无声地烫上面颊。'
        )

    def test_trailing_extra_brace(self):
        # 真实案例：gemini-3.1-pro 输出末尾多一个 }
        self.assertEqual(parse_json_loose('{"a": 1}\n}'), {"a": 1})

    def test_unescaped_quotes_with_trailing_extra_brace_keeps_object(self):
        raw = '{"translations":["他说"好"。"]}\n}'
        self.assertEqual(
            parse_json_loose(raw),
            {"translations": ['他说"好"。']},
        )

    def test_valid_json_untouched(self):
        self.assertEqual(parse_json_loose('{"a": "b, c: d"}'), {"a": "b, c: d"})

    def test_escaped_quotes_still_work(self):
        self.assertEqual(parse_json_loose('{"a": "he said \\"hi\\""}'), {"a": 'he said "hi"'})

    def test_premature_array_close_keeps_following_string_items_in_array(self):
        raw = '{"translations":["第一项"],"第二项","第三项"]}'
        self.assertEqual(
            parse_json_loose(raw),
            {"translations": ["第一项", "第二项", "第三项"]},
        )

    def test_premature_array_close_can_be_combined_with_unescaped_quotes(self):
        raw = '{"translations":["他说"好"。"],"第二项","第三项"]}'
        self.assertEqual(
            parse_json_loose(raw),
            {"translations": ['他说"好"。', "第二项", "第三项"]},
        )

    def test_valid_object_property_after_array_is_not_repaired(self):
        raw = '{"translations":["第一项"],"note":"第二项"}'
        self.assertEqual(
            parse_json_loose(raw),
            {"translations": ["第一项"], "note": "第二项"},
        )


class TestProviderRequestKwargs(unittest.TestCase):
    messages = [{"role": "user", "content": "x"}]

    def test_json_mode_adds_lowercase_keyword_without_mutating_messages(self):
        from trans_novel.llm.providers._openai_compatible import (
            base_request_kwargs,
        )

        messages = [
            {"role": "system", "content": "仅输出指定对象。"},
            {"role": "user", "content": "x"},
        ]
        kwargs = base_request_kwargs("m", messages, json_mode=True)

        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertIn("json", kwargs["messages"][0]["content"])
        self.assertEqual(messages[0]["content"], "仅输出指定对象。")

    def test_json_mode_also_mentions_json_in_user_message(self):
        # 部分中转网关只校验 user/input 内容里是否含 "json"（比如转发到
        # Responses API 的 text.format 校验），所以 user 消息也要兜底补一份。
        from trans_novel.llm.providers._openai_compatible import (
            base_request_kwargs,
        )

        messages = [
            {"role": "system", "content": "仅输出指定对象。"},
            {"role": "user", "content": "翻译这句话。"},
        ]
        kwargs = base_request_kwargs("m", messages, json_mode=True)

        self.assertIn("json", kwargs["messages"][-1]["content"].lower())
        self.assertEqual(messages[-1]["content"], "翻译这句话。")

    def test_json_mode_skips_user_message_already_mentioning_json(self):
        from trans_novel.llm.providers._openai_compatible import (
            base_request_kwargs,
        )

        messages = [
            {"role": "system", "content": "仅输出指定对象。"},
            {"role": "user", "content": "请输出 JSON 数组。"},
        ]
        kwargs = base_request_kwargs("m", messages, json_mode=True)

        self.assertEqual(kwargs["messages"][-1]["content"], "请输出 JSON 数组。")

    def test_deepseek_dialect_and_recursive_extra_body(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.deepseek import (
            DeepSeekTierOptions,
            build_request_kwargs,
        )

        tier = ResolvedTier(
            model="m",
            options=DeepSeekTierOptions(
                extra_body={"thinking": {"budget": 8192}},
            ),
        )
        kwargs = build_request_kwargs(tier, self.messages)

        self.assertEqual(kwargs["reasoning_effort"], "high")
        self.assertEqual(
            kwargs["extra_body"],
            {"thinking": {"type": "enabled", "budget": 8192}},
        )

        disabled = ResolvedTier(
            model="m",
            options=DeepSeekTierOptions(thinking=False),
        )
        disabled_kwargs = build_request_kwargs(disabled, self.messages)
        self.assertNotIn("reasoning_effort", disabled_kwargs)
        self.assertEqual(
            disabled_kwargs["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

    def test_openrouter_dialect_and_explicit_disable(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.openrouter import (
            OpenRouterTierOptions,
            build_request_kwargs,
        )

        enabled = ResolvedTier(
            model="m",
            options=OpenRouterTierOptions(reasoning_effort="high"),
        )
        disabled = ResolvedTier(
            model="m",
            options=OpenRouterTierOptions(thinking=False),
        )

        self.assertEqual(
            build_request_kwargs(enabled, self.messages)["extra_body"],
            {"reasoning": {"effort": "high"}},
        )
        self.assertEqual(
            build_request_kwargs(disabled, self.messages)["extra_body"],
            {"reasoning": {"enabled": False}},
        )

    def test_openai_dialect(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.openai import (
            OpenAITierOptions,
            build_request_kwargs,
        )

        tier = ResolvedTier(
            model="m",
            options=OpenAITierOptions(reasoning_effort="low"),
        )
        kwargs = build_request_kwargs(tier, self.messages)

        self.assertEqual(kwargs["reasoning_effort"], "low")
        self.assertNotIn("extra_body", kwargs)

        disabled = ResolvedTier(
            model="m",
            options=OpenAITierOptions(thinking=False),
        )
        disabled_kwargs = build_request_kwargs(disabled, self.messages)
        self.assertEqual(disabled_kwargs["reasoning_effort"], "none")

    def test_openai_uses_max_completion_tokens(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.openai import (
            OpenAITierOptions,
            build_request_kwargs,
        )

        enabled = ResolvedTier(model="m", options=OpenAITierOptions())
        disabled = ResolvedTier(
            model="m",
            options=OpenAITierOptions(thinking=False),
        )

        enabled_kwargs = build_request_kwargs(
            enabled,
            self.messages,
            max_tokens=100,
        )
        disabled_kwargs = build_request_kwargs(
            disabled,
            self.messages,
            max_tokens=100,
        )
        self.assertNotIn("max_tokens", enabled_kwargs)
        self.assertEqual(enabled_kwargs["max_completion_tokens"], 4096)
        self.assertNotIn("max_tokens", disabled_kwargs)
        self.assertEqual(disabled_kwargs["max_completion_tokens"], 100)

    def test_generic_compatible_endpoint_maps_reasoning_dialects(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.openai_compatible import (
            OpenAICompatibleTierOptions,
            build_request_kwargs,
        )

        tier = ResolvedTier(
            model="m",
            options=OpenAICompatibleTierOptions(
                thinking=True,
                reasoning_effort="medium",
                request_overrides={"thinking": {"budget": 8192}},
            ),
        )
        deepseek = build_request_kwargs(
            tier,
            self.messages,
            max_tokens=100,
            reasoning_style="deepseek",
        )
        openai = build_request_kwargs(
            tier,
            self.messages,
            reasoning_style="openai",
        )
        openrouter = build_request_kwargs(
            tier,
            self.messages,
            reasoning_style="openrouter",
        )

        self.assertEqual(deepseek["reasoning_effort"], "medium")
        self.assertEqual(
            deepseek["extra_body"],
            {"thinking": {"type": "enabled", "budget": 8192}},
        )
        self.assertEqual(deepseek["max_tokens"], 4096)
        self.assertEqual(openai["reasoning_effort"], "medium")
        self.assertEqual(
            openai["extra_body"],
            {"thinking": {"budget": 8192}},
        )
        self.assertEqual(
            openrouter["extra_body"],
            {
                "reasoning": {"effort": "medium"},
                "thinking": {"budget": 8192},
            },
        )

    def test_generic_compatible_endpoint_explicitly_disables_reasoning(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.openai_compatible import (
            OpenAICompatibleTierOptions,
            build_request_kwargs,
        )

        tier = ResolvedTier(
            model="m",
            options=OpenAICompatibleTierOptions(thinking=False),
        )

        self.assertEqual(
            build_request_kwargs(
                tier,
                self.messages,
                reasoning_style="deepseek",
            )["extra_body"],
            {"thinking": {"type": "disabled"}},
        )
        self.assertEqual(
            build_request_kwargs(
                tier,
                self.messages,
                reasoning_style="openai",
            )["reasoning_effort"],
            "none",
        )
        self.assertEqual(
            build_request_kwargs(
                tier,
                self.messages,
                reasoning_style="openrouter",
            )["extra_body"],
            {"reasoning": {"enabled": False}},
        )

    def test_generic_compatible_endpoint_can_only_use_raw_overrides(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.openai_compatible import (
            OpenAICompatibleTierOptions,
            build_request_kwargs,
        )

        tier = ResolvedTier(
            model="m",
            options=OpenAICompatibleTierOptions(
                thinking=True,
                request_overrides={"enable_thinking": True},
            ),
        )
        kwargs = build_request_kwargs(tier, self.messages, max_tokens=100)

        self.assertNotIn("reasoning_effort", kwargs)
        self.assertEqual(kwargs["extra_body"], {"enable_thinking": True})
        self.assertEqual(kwargs["max_tokens"], 4096)


class TestAnthropicProvider(unittest.TestCase):
    messages = [
        {"role": "system", "content": "你是翻译。"},
        {"role": "user", "content": "x"},
    ]

    def test_splits_system_and_defaults_to_effort_flag(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.anthropic import (
            AnthropicTierOptions,
            build_cli_invocation,
        )

        tier = ResolvedTier(model="claude-opus-4-8", options=AnthropicTierOptions())
        extra_argv, system_prompt, stdin_text = build_cli_invocation(tier, self.messages)

        self.assertEqual(system_prompt, "你是翻译。")
        self.assertEqual(stdin_text, "x")
        self.assertIn("--model", extra_argv)
        self.assertEqual(extra_argv[extra_argv.index("--model") + 1], "claude-opus-4-8")
        self.assertIn("--effort", extra_argv)
        self.assertEqual(extra_argv[extra_argv.index("--effort") + 1], "high")

    def test_thinking_disabled_skips_effort_flag(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.anthropic import (
            AnthropicTierOptions,
            build_cli_invocation,
        )

        tier = ResolvedTier(
            model="claude-haiku-4-5",
            options=AnthropicTierOptions(thinking=False),
        )
        extra_argv, _, _ = build_cli_invocation(tier, self.messages)

        self.assertNotIn("--effort", extra_argv)

    def test_custom_reasoning_effort_is_passed_through(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.anthropic import (
            AnthropicTierOptions,
            build_cli_invocation,
        )

        tier = ResolvedTier(
            model="claude-opus-4-8",
            options=AnthropicTierOptions(reasoning_effort="xhigh"),
        )
        extra_argv, _, _ = build_cli_invocation(tier, self.messages)

        self.assertEqual(extra_argv[extra_argv.index("--effort") + 1], "xhigh")

    def test_json_mode_appends_instruction_without_mutating_input(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.anthropic import (
            AnthropicTierOptions,
            build_cli_invocation,
        )

        tier = ResolvedTier(model="m", options=AnthropicTierOptions(thinking=False))
        _, system_prompt, _ = build_cli_invocation(tier, self.messages, json_mode=True)

        self.assertIn("json", system_prompt)
        self.assertEqual(self.messages[0]["content"], "你是翻译。")

    def test_json_mode_without_system_message_still_injects_instruction(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.anthropic import (
            AnthropicTierOptions,
            build_cli_invocation,
        )

        tier = ResolvedTier(model="m", options=AnthropicTierOptions(thinking=False))
        _, system_prompt, _ = build_cli_invocation(
            tier, [{"role": "user", "content": "x"}], json_mode=True
        )

        self.assertIn("json", system_prompt)

    def test_multiple_non_system_messages_preserve_roles_in_stdin_text(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.anthropic import (
            AnthropicTierOptions,
            build_cli_invocation,
        )

        tier = ResolvedTier(model="m", options=AnthropicTierOptions(thinking=False))
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ]
        _, _, stdin_text = build_cli_invocation(tier, messages)

        self.assertEqual(
            stdin_text,
            "[USER]\nfirst\n\n[ASSISTANT]\nsecond\n\n[USER]\nthird",
        )

    def test_usage_normalization_treats_cache_write_as_miss(self):
        from trans_novel.llm.providers.anthropic import normalize_anthropic_usage

        usage = {
            "input_tokens": 10,
            "cache_creation_input_tokens": 5,
            "cache_read_input_tokens": 20,
            "output_tokens": 7,
        }
        sample = normalize_anthropic_usage(usage)

        self.assertEqual(sample.cache_miss_tokens, 15)
        self.assertEqual(sample.cache_hit_tokens, 20)
        self.assertEqual(sample.prompt_tokens, 35)
        self.assertEqual(sample.completion_tokens, 7)
        self.assertEqual(sample.total_tokens, 42)

    def test_usage_normalization_handles_missing_usage(self):
        from trans_novel.llm.providers.anthropic import normalize_anthropic_usage

        self.assertIsNone(normalize_anthropic_usage(None))

    def test_complete_invokes_cli_and_returns_result_text(self):
        import json
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.anthropic import AnthropicClient

        cfg = LLMConfig(tiers={"strong": {"model": "claude-opus-4-8"}})
        client = AnthropicClient(cfg)

        fake_result = {
            "is_error": False,
            "result": "你好",
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 5,
            },
        }

        captured = {}

        def fake_run(argv, *, input, capture_output, text, timeout, encoding):
            captured["argv"] = argv
            captured["input"] = input
            captured["timeout"] = timeout
            captured["encoding"] = encoding
            prompt_file = argv[argv.index("--system-prompt-file") + 1]
            with open(prompt_file, "r", encoding="utf-8") as f:
                captured["system_prompt_file_content"] = f.read()

            class Result:
                returncode = 0
                stdout = json.dumps(fake_result)
                stderr = ""

            return Result()

        with (
            patch("shutil.which", return_value=r"C:\nodejs\claude.cmd"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            text = client.complete(
                [
                    {"role": "system", "content": "你是翻译。"},
                    {"role": "user", "content": "hello"},
                ],
                tier="strong",
                stage="Translator",
            )

        self.assertEqual(text, "你好")
        self.assertEqual(captured["encoding"], "utf-8")
        self.assertEqual(captured["input"], "hello")
        self.assertIn(r"C:\nodejs\claude.cmd", captured["argv"])
        self.assertIn("--safe-mode", captured["argv"])
        self.assertIn("--no-session-persistence", captured["argv"])
        self.assertIn("--tools", captured["argv"])
        self.assertIn("none", captured["argv"])
        self.assertIn("--system-prompt-file", captured["argv"])
        self.assertNotIn("--system-prompt", captured["argv"])
        self.assertEqual(captured["system_prompt_file_content"], "你是翻译。")
        self.assertIn("--model", captured["argv"])
        self.assertIn("claude-opus-4-8", captured["argv"])

        summary = client.usage_summary()
        self.assertEqual(summary["totals"]["prompt_tokens"], 10)
        self.assertEqual(summary["totals"]["completion_tokens"], 5)

    def test_complete_cleans_up_system_prompt_temp_file(self):
        import json
        import os
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.anthropic import AnthropicClient

        cfg = LLMConfig(tiers={"strong": {"model": "m"}})
        client = AnthropicClient(cfg)

        captured = {}

        def fake_run(argv, *, input, capture_output, text, timeout, encoding):
            prompt_file = argv[argv.index("--system-prompt-file") + 1]
            captured["prompt_file"] = prompt_file
            self.assertTrue(os.path.exists(prompt_file))

            class Result:
                returncode = 0
                stdout = json.dumps({"is_error": False, "result": "ok", "usage": None})
                stderr = ""

            return Result()

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            client.complete(
                [
                    {"role": "system", "content": "sys with <angle> brackets"},
                    {"role": "user", "content": "x"},
                ]
            )

        self.assertFalse(os.path.exists(captured["prompt_file"]))

    def test_complete_omits_system_prompt_file_flag_when_no_system_message(self):
        import json
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.anthropic import AnthropicClient

        cfg = LLMConfig(tiers={"strong": {"model": "m"}})
        client = AnthropicClient(cfg)

        captured = {}

        def fake_run(argv, *, input, capture_output, text, timeout, encoding):
            captured["argv"] = argv

            class Result:
                returncode = 0
                stdout = json.dumps({"is_error": False, "result": "ok", "usage": None})
                stderr = ""

            return Result()

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            client.complete([{"role": "user", "content": "x"}])

        self.assertNotIn("--system-prompt-file", captured["argv"])

    def test_complete_retries_on_is_error_then_succeeds(self):
        import json
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.anthropic import AnthropicClient

        cfg = LLMConfig(tiers={"strong": {"model": "m"}}, max_retries=2)
        client = AnthropicClient(cfg)

        calls = {"count": 0}

        def fake_run(argv, *, input, capture_output, text, timeout, encoding):
            calls["count"] += 1

            class Result:
                returncode = 0
                stderr = ""

            result = Result()
            if calls["count"] == 1:
                result.stdout = json.dumps({"is_error": True, "result": ""})
            else:
                result.stdout = json.dumps({"is_error": False, "result": "ok", "usage": None})
            return result

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", side_effect=fake_run),
            patch("time.sleep", return_value=None),
        ):
            text = client.complete([{"role": "user", "content": "x"}])

        self.assertEqual(text, "ok")
        self.assertEqual(calls["count"], 2)

    def test_complete_raises_when_cli_not_found(self):
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.anthropic import AnthropicClient

        cfg = LLMConfig(tiers={"strong": {"model": "m"}})
        client = AnthropicClient(cfg)

        with patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "cli_path"):
                client.complete([{"role": "user", "content": "x"}])

    def test_complete_uses_explicit_cli_path_over_which(self):
        import json
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.anthropic import AnthropicClient

        cfg = LLMConfig(tiers={"strong": {"model": "m"}}, cli_path=r"D:\custom\claude.cmd")
        client = AnthropicClient(cfg)

        captured = {}

        def fake_run(argv, *, input, capture_output, text, timeout, encoding):
            captured["argv"] = argv

            class Result:
                returncode = 0
                stdout = json.dumps({"is_error": False, "result": "ok", "usage": None})
                stderr = ""

            return Result()

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            client.complete([{"role": "user", "content": "x"}])

        self.assertIn(r"D:\custom\claude.cmd", captured["argv"])
        self.assertNotIn("/usr/bin/claude", captured["argv"])

    def test_complete_reports_missing_cli_path(self):
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.anthropic import AnthropicClient

        cli_path = r"D:\missing\claude.cmd"
        client = AnthropicClient(
            LLMConfig(tiers={"strong": {"model": "m"}}, cli_path=cli_path, max_retries=0)
        )

        with patch("subprocess.run", side_effect=FileNotFoundError(2, "No such file")):
            with self.assertRaises(RuntimeError) as context:
                client.complete([{"role": "user", "content": "x"}])

        message = str(context.exception)
        self.assertIn("Claude CLI", message)
        self.assertIn(cli_path, message)


class TestCodexProvider(unittest.TestCase):
    def test_prompt_preserves_roles_and_json_instruction(self):
        from trans_novel.llm.providers.codex import build_codex_prompt

        prompt = build_codex_prompt(
            [
                {"role": "system", "content": "你是译者。"},
                {"role": "user", "content": "翻译这句话。"},
            ],
            json_mode=True,
        )

        self.assertIn("[SYSTEM]", prompt)
        self.assertIn("[USER]", prompt)
        self.assertIn("valid JSON", prompt)

    def test_parse_jsonl_result_and_usage(self):
        import json

        from trans_novel.llm.providers.codex import parse_codex_events

        output = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "你好"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 4,
                            "output_tokens": 3,
                        },
                    }
                ),
            ]
        )

        text, usage = parse_codex_events(output)

        self.assertEqual(text, "你好")
        self.assertEqual(usage.prompt_tokens, 10)
        self.assertEqual(usage.cache_hit_tokens, 4)
        self.assertEqual(usage.cache_miss_tokens, 6)
        self.assertEqual(usage.total_tokens, 13)

    def test_complete_invokes_codex_exec(self):
        import json
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.codex import CodexClient

        client = CodexClient(LLMConfig(tiers={"strong": {"model": "m"}}))
        captured = {}

        def fake_run(argv, *, input, capture_output, text, timeout, encoding):
            captured["argv"] = argv
            captured["input"] = input

            class Result:
                returncode = 0
                stdout = "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"type": "agent_message", "text": "ok"},
                            }
                        ),
                        json.dumps({"type": "turn.completed", "usage": None}),
                    ]
                )
                stderr = ""

            return Result()

        with (
            patch("shutil.which", return_value="/usr/bin/codex"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            result = client.complete([{"role": "user", "content": "x"}])

        self.assertEqual(result, "ok")
        self.assertEqual(captured["argv"][:2], ["/usr/bin/codex", "exec"])
        self.assertIn("--json", captured["argv"])
        self.assertIn("--sandbox", captured["argv"])
        self.assertIn("mcp_servers={}", captured["argv"])
        self.assertEqual(captured["input"], "[USER]\nx")

    def test_complete_reports_missing_cli_path(self):
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.codex import CodexClient

        cli_path = r"D:\missing\codex.cmd"
        client = CodexClient(
            LLMConfig(tiers={"strong": {"model": "m"}}, cli_path=cli_path, max_retries=0)
        )

        with patch("subprocess.run", side_effect=FileNotFoundError(2, "No such file")):
            with self.assertRaises(RuntimeError) as context:
                client.complete([{"role": "user", "content": "x"}])

        message = str(context.exception)
        self.assertIn("Codex CLI", message)
        self.assertIn(cli_path, message)


class TestAgyProvider(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        from unittest.mock import patch

        self._agy_log_dir = tempfile.TemporaryDirectory()
        self._agy_log_path = os.path.join(self._agy_log_dir.name, "agy_errors.log")
        self._agy_log_patch = patch(
            "trans_novel.llm.providers.agy.AGY_ERROR_LOG_FILE", self._agy_log_path
        )
        self._agy_log_patch.start()
        self.addCleanup(self._agy_log_patch.stop)
        self.addCleanup(self._agy_log_dir.cleanup)

    def test_prompt_preserves_roles_and_json_instruction(self):
        from trans_novel.llm.providers.agy import build_agy_prompt

        prompt = build_agy_prompt(
            [
                {"role": "system", "content": "你是译者。"},
                {"role": "user", "content": "翻译这句话。"},
                {"role": "assistant", "content": ""},
            ],
            json_mode=True,
        )

        self.assertIn("[SYSTEM]\n你是译者。", prompt)
        self.assertIn("[USER]\n翻译这句话。", prompt)
        self.assertNotIn("[ASSISTANT]", prompt)
        self.assertIn(
            "[OUTPUT FORMAT]\nReturn only valid JSON, with no markdown fence or explanation.",
            prompt,
        )

        prompt_plain = build_agy_prompt([{"role": "user", "content": "hello"}], json_mode=False)
        self.assertNotIn("[OUTPUT FORMAT]", prompt_plain)
        self.assertEqual(prompt_plain, "[USER]\nhello")

    def test_tier_options_validation_and_mapping(self):
        from pydantic import ValidationError

        from trans_novel.llm.providers.agy import AgyTierOptions

        default_options = AgyTierOptions()
        self.assertEqual(default_options.reasoning_effort, "high")

        for effort in ("low", "medium", "high"):
            options = AgyTierOptions(reasoning_effort=effort)
            self.assertEqual(options.reasoning_effort, effort)

        normalized = AgyTierOptions(reasoning_effort=" LOW ")
        self.assertEqual(normalized.reasoning_effort, "low")

        with self.assertRaises(ValidationError):
            AgyTierOptions(reasoning_effort="xhigh")
        with self.assertRaises(ValidationError):
            AgyTierOptions(reasoning_effort="max")
        with self.assertRaises(ValidationError):
            AgyTierOptions(extra_param=123)

    def test_agy_ndjson_input_and_cli_arguments(self):
        import json

        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.agy import (
            AgyTierOptions,
            build_agy_argv,
            build_agy_input_payload,
            build_agy_invocation,
        )

        tier = ResolvedTier(model="gemini-3.1-pro", options=AgyTierOptions(reasoning_effort="low"))
        argv = build_agy_argv("C:\\tools\\agy.cmd", tier)
        self.assertEqual(
            argv,
            [
                "C:\\tools\\agy.cmd",
                "--print=",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--disable-slash-commands",
                "--model",
                "gemini-3.1-pro",
                "--effort",
                "low",
            ],
        )

        payload = build_agy_input_payload("你好世界")
        self.assertTrue(payload.endswith("\n"))
        data = json.loads(payload)
        self.assertEqual(
            data,
            {
                "event": "user",
                "message": {"role": "user", "content": "你好世界"},
            },
        )

        inv_argv, inv_payload = build_agy_invocation(
            tier,
            [{"role": "user", "content": "测试"}],
            cli_path="/usr/bin/agy",
            json_mode=True,
        )
        self.assertEqual(inv_argv[0], "/usr/bin/agy")
        self.assertIn("valid JSON", json.loads(inv_payload)["message"]["content"])

    def test_agy_input_payload_protocol_invariants(self):
        import json

        from trans_novel.llm.providers.agy import build_agy_input_payload

        prompt = '你好，世界！\nHello world!\n{"json": true}'
        payload = build_agy_input_payload(prompt)

        # payload 以单个换行结束
        self.assertTrue(payload.endswith("\n"))
        self.assertFalse(payload.endswith("\n\n"))

        # 非 ASCII 提示词不会被转义破坏
        self.assertIn("你好，世界！", payload)
        self.assertNotIn(r"\u4f60\u597d", payload)

        data = json.loads(payload)
        # 顶层 event 为 "user"
        self.assertEqual(data.get("event"), "user")
        # message 为对象
        message = data.get("message")
        self.assertIsInstance(message, dict)
        # message.role 为 "user"
        self.assertEqual(message.get("role"), "user")
        # message.content 为完整提示词
        self.assertEqual(message.get("content"), prompt)

    def test_parse_events_success_and_usage(self):
        import json

        from trans_novel.llm.providers.agy import parse_agy_events

        output = "\n".join(
            [
                json.dumps({"event": "status", "status": "RUNNING"}),
                json.dumps(
                    {
                        "event": "result",
                        "result": {
                            "status": "SUCCESS",
                            "response": "翻译完成",
                            "usage": {
                                "input_tokens": 100,
                                "output_tokens": 50,
                                "cache_read_tokens": 30,
                                "total_tokens": 150,
                            },
                        },
                    }
                ),
            ]
        )

        text, usage = parse_agy_events(output)
        self.assertEqual(text, "翻译完成")
        self.assertIsNotNone(usage)
        self.assertEqual(usage.prompt_tokens, 100)
        self.assertEqual(usage.completion_tokens, 50)
        self.assertEqual(usage.cache_hit_tokens, 30)
        self.assertEqual(usage.cache_miss_tokens, 70)
        self.assertEqual(usage.total_tokens, 150)

    def test_usage_calculations_and_missing_fields(self):
        import json

        from trans_novel.llm.providers.agy import normalize_agy_usage, parse_agy_events

        usage = normalize_agy_usage(
            {"input_tokens": 80, "output_tokens": 20, "cache_read_tokens": 10}
        )
        self.assertEqual(usage.prompt_tokens, 80)
        self.assertEqual(usage.completion_tokens, 20)
        self.assertEqual(usage.cache_hit_tokens, 10)
        self.assertEqual(usage.cache_miss_tokens, 70)
        self.assertEqual(usage.total_tokens, 100)

        usage_no_cache = normalize_agy_usage({"input_tokens": 50, "output_tokens": 10})
        self.assertEqual(usage_no_cache.cache_hit_tokens, 0)
        self.assertEqual(usage_no_cache.cache_miss_tokens, 50)
        self.assertEqual(usage_no_cache.total_tokens, 60)

        self.assertIsNone(normalize_agy_usage(None))

        output = json.dumps(
            {
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "ok",
                    "input_tokens": 40,
                    "output_tokens": 10,
                },
            }
        )
        _, sample = parse_agy_events(output)
        self.assertIsNotNone(sample)
        self.assertEqual(sample.prompt_tokens, 40)
        self.assertEqual(sample.total_tokens, 50)

    def test_parse_events_exceptions(self):
        import json

        from trans_novel.llm.providers.agy import parse_agy_events

        with self.assertRaisesRegex(RuntimeError, "non-JSON data"):
            parse_agy_events("not a valid json line")

        empty_stream = json.dumps({"event": "status", "status": "RUNNING"})
        with self.assertRaisesRegex(RuntimeError, "did not return a result event"):
            parse_agy_events(empty_stream)

        failed_event = json.dumps(
            {
                "event": "result",
                "result": {"status": "ERROR", "error": "rate limit exceeded"},
            }
        )
        with self.assertRaisesRegex(RuntimeError, "non-success status 'ERROR'"):
            parse_agy_events(failed_event)

        empty_resp = json.dumps(
            {"event": "result", "result": {"status": "SUCCESS", "response": ""}}
        )
        with self.assertRaisesRegex(RuntimeError, "empty or missing response"):
            parse_agy_events(empty_resp)

        whitespace_resp = json.dumps(
            {
                "event": "result",
                "result": {"status": "SUCCESS", "response": "   \n\t  "},
            }
        )
        with self.assertRaisesRegex(RuntimeError, "empty or missing response"):
            parse_agy_events(whitespace_resp)

    def test_parse_events_error_context_does_not_duplicate_stderr(self):
        import json

        from trans_novel.llm.providers.agy import parse_agy_events

        failed_event = json.dumps(
            {
                "event": "result",
                "result": {"status": "ERROR", "error": "provider rejected request"},
            }
        )
        with self.assertRaisesRegex(RuntimeError, "non-success status 'ERROR'"):
            parse_agy_events(
                failed_event,
                "actual stderr",
                error_context={"request_id": 7, "stderr": "context stderr"},
            )

        with open(self._agy_log_path, encoding="utf-8") as log_file:
            log = log_file.read()
        self.assertIn("provider rejected request", log)
        self.assertNotIn("got multiple values for keyword argument 'stderr'", log)

    def test_complete_invokes_agy_cli(self):
        import json
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.agy import AgyClient

        client = AgyClient(
            LLMConfig(
                tiers={
                    "strong": {
                        "model": "gemini-3.1-pro",
                        "options": {"reasoning_effort": "medium"},
                    }
                },
                cli_path="C:\\tools\\agy.cmd",
                timeout=300,
            )
        )
        captured = {}

        def fake_run(argv, *, input, capture_output, text, timeout, encoding):
            captured["argv"] = argv
            captured["input"] = input
            captured["timeout"] = timeout

            class Result:
                returncode = 0
                stdout = json.dumps(
                    {
                        "event": "result",
                        "result": {
                            "status": "SUCCESS",
                            "response": "翻译结果",
                            "usage": {"input_tokens": 12, "output_tokens": 4},
                        },
                    }
                )
                stderr = ""

            return Result()

        with patch("subprocess.run", side_effect=fake_run):
            text = client.complete([{"role": "user", "content": "hello"}], stage="translation")

        self.assertEqual(text, "翻译结果")
        self.assertEqual(captured["argv"][:2], ["C:\\tools\\agy.cmd", "--print="])
        self.assertIn("--disable-slash-commands", captured["argv"])
        self.assertEqual(captured["argv"][captured["argv"].index("--model") + 1], "gemini-3.1-pro")
        self.assertEqual(captured["argv"][captured["argv"].index("--effort") + 1], "medium")
        self.assertEqual(captured["timeout"], 300)
        self.assertIn("hello", captured["input"])

        summary = client.usage.summary()
        self.assertEqual(summary["by_tier"]["strong"]["prompt_tokens"], 12)
        self.assertEqual(summary["by_tier"]["strong"]["completion_tokens"], 4)
        self.assertEqual(summary["by_provider"]["Agy"]["prompt_tokens"], 12)

    def test_cli_path_resolution(self):
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.agy import AgyClient

        c1 = AgyClient(
            LLMConfig(
                tiers={"strong": {"model": "m", "cli_path": "C:\\tier\\agy.cmd"}},
                cli_path="C:\\global\\agy.cmd",
            )
        )
        self.assertEqual(c1._ensure_cli_path(c1.tiers["strong"]), "C:\\tier\\agy.cmd")

        c2 = AgyClient(
            LLMConfig(
                tiers={"strong": {"model": "m"}},
                cli_path="C:\\global\\agy.cmd",
            )
        )
        self.assertEqual(c2._ensure_cli_path(), "C:\\global\\agy.cmd")

        c3 = AgyClient(LLMConfig(tiers={"strong": {"model": "m"}}))
        with patch("shutil.which", return_value="/usr/local/bin/agy"):
            self.assertEqual(c3._ensure_cli_path(), "/usr/local/bin/agy")

        c4 = AgyClient(LLMConfig(tiers={"strong": {"model": "m"}}))
        with patch("shutil.which", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                c4._ensure_cli_path()
            self.assertIn("llm.cli_path", str(ctx.exception))

    def test_complete_reports_missing_cli_path(self):
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.agy import AgyClient

        cli_path = r"D:\missing\agy.cmd"
        client = AgyClient(
            LLMConfig(tiers={"strong": {"model": "m"}}, cli_path=cli_path, max_retries=0)
        )

        with patch("subprocess.run", side_effect=FileNotFoundError(2, "No such file")):
            with self.assertRaises(RuntimeError) as context:
                client.complete([{"role": "user", "content": "x"}])

        message = str(context.exception)
        self.assertIn("Agy CLI", message)
        self.assertIn(cli_path, message)

    def test_subprocess_nonzero_exit_and_retry(self):
        import json
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.agy import AgyClient

        client = AgyClient(
            LLMConfig(tiers={"strong": {"model": "m"}}, cli_path="agy", max_retries=1)
        )

        calls = 0

        def fake_run(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:

                class FailResult:
                    returncode = 1
                    stdout = ""
                    stderr = "fatal: process crashed"

                return FailResult()

            class SuccessResult:
                returncode = 0
                stdout = json.dumps(
                    {
                        "event": "result",
                        "result": {"status": "SUCCESS", "response": "retried ok"},
                    }
                )
                stderr = ""

            return SuccessResult()

        with patch("subprocess.run", side_effect=fake_run):
            text = client.complete([{"role": "user", "content": "x"}])

        self.assertEqual(text, "retried ok")
        self.assertEqual(calls, 2)

    def test_subprocess_timeout_retries(self):
        import json
        import subprocess
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.agy import AgyClient

        client = AgyClient(
            LLMConfig(tiers={"strong": {"model": "m"}}, cli_path="agy", max_retries=1)
        )

        calls = 0

        def fake_run(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise subprocess.TimeoutExpired(cmd=["agy"], timeout=600)

            class SuccessResult:
                returncode = 0
                stdout = json.dumps(
                    {
                        "event": "result",
                        "result": {"status": "SUCCESS", "response": "recovered after timeout"},
                    }
                )
                stderr = ""

            return SuccessResult()

        with patch("subprocess.run", side_effect=fake_run):
            text = client.complete([{"role": "user", "content": "x"}])

        self.assertEqual(text, "recovered after timeout")
        self.assertEqual(calls, 2)

    def test_model_must_be_explicitly_configured(self):
        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.agy import AgyClient

        with self.assertRaises(ValueError) as ctx:
            AgyClient(LLMConfig(tiers={}))
        self.assertIn("llm.tiers.strong.model", str(ctx.exception))

    def test_parse_events_logs_malformed_json(self):
        from trans_novel.llm.providers.agy import parse_agy_events

        with self.assertRaises(RuntimeError):
            parse_agy_events("not json at all")

        with open(self._agy_log_path, encoding="utf-8") as log_file:
            log = log_file.read()
        self.assertIn("Operation: parse_agy_events", log)
        self.assertIn("not json at all", log)

    def test_complete_logs_cli_failure_with_command_context(self):
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.agy import AgyClient

        client = AgyClient(LLMConfig(tiers={"strong": {"model": "m"}}, max_retries=0))

        def fake_run(argv, *, input, capture_output, text, timeout, encoding):
            class Result:
                returncode = 2
                stdout = "partial output"
                stderr = "provider failed"

            return Result()

        with (
            patch("shutil.which", return_value="/usr/bin/agy"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            with self.assertRaises(RuntimeError):
                client.complete([{"role": "user", "content": "x"}], stage="Translator")

        with open(self._agy_log_path, encoding="utf-8") as log_file:
            log = log_file.read()
        self.assertIn("Operation: agy_cli_attempt", log)
        self.assertIn("Stage: Translator", log)
        self.assertIn("provider failed", log)
        self.assertIn("partial output", log)

    def test_complete_json_logs_model_response_parse_failure(self):
        import json
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.agy import AgyClient

        client = AgyClient(LLMConfig(tiers={"strong": {"model": "m"}}))

        def fake_run(argv, *, input, capture_output, text, timeout, encoding):
            class Result:
                returncode = 0
                stdout = json.dumps(
                    {
                        "event": "result",
                        "result": {"status": "SUCCESS", "response": "not json", "usage": None},
                    }
                )
                stderr = ""

            return Result()

        with (
            patch("shutil.which", return_value="/usr/bin/agy"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            with self.assertRaises(ValueError):
                client.complete_json([{"role": "user", "content": "x"}], stage="Translator")

        with open(self._agy_log_path, encoding="utf-8") as log_file:
            log = log_file.read()
        self.assertIn("Operation: complete_json_parse", log)
        self.assertIn("Stage: Translator", log)
        self.assertIn("MODEL RESPONSE:", log)
        self.assertIn("not json", log)


class TestPiProvider(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        from unittest.mock import patch

        self._pi_log_dir = tempfile.TemporaryDirectory()
        self._pi_log_path = os.path.join(self._pi_log_dir.name, "pi_errors.log")
        self._pi_log_patch = patch(
            "trans_novel.llm.providers.pi.PI_ERROR_LOG_FILE", self._pi_log_path
        )
        self._pi_log_patch.start()
        self.addCleanup(self._pi_log_patch.stop)
        self.addCleanup(self._pi_log_dir.cleanup)

        from trans_novel.llm.providers.pi import _reset_audit_alert_state_for_testing

        _reset_audit_alert_state_for_testing()
        self.addCleanup(_reset_audit_alert_state_for_testing)

    def test_invocation_splits_system_and_defaults_thinking(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.pi import PiTierOptions, build_pi_invocation

        tier = ResolvedTier(model="deepseek-v4-pro", options=PiTierOptions())
        extra_argv, system_prompt, stdin_text = build_pi_invocation(
            tier,
            [
                {"role": "system", "content": "你是翻译。"},
                {"role": "user", "content": "x"},
            ],
        )

        self.assertEqual(system_prompt, "你是翻译。")
        self.assertEqual(stdin_text, "x")
        self.assertEqual(extra_argv[extra_argv.index("--model") + 1], "deepseek-v4-pro")
        self.assertEqual(extra_argv[extra_argv.index("--thinking") + 1], "high")

    def test_invocation_preserves_roles_for_multiturn_transcript(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.pi import PiTierOptions, build_pi_invocation

        tier = ResolvedTier(model="m", options=PiTierOptions(thinking=False))
        _, _, stdin_text = build_pi_invocation(
            tier,
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
                {"role": "user", "content": "third"},
            ],
        )

        self.assertEqual(
            stdin_text,
            "[USER]\nfirst\n\n[ASSISTANT]\nsecond\n\n[USER]\nthird",
        )

    def test_thinking_disabled_uses_off(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.pi import PiTierOptions, build_pi_invocation

        tier = ResolvedTier(
            model="m", options=PiTierOptions(thinking=False, reasoning_effort="high")
        )
        extra_argv, _, _ = build_pi_invocation(tier, [{"role": "user", "content": "x"}])

        self.assertEqual(extra_argv[extra_argv.index("--thinking") + 1], "off")

    def test_json_mode_appends_instruction_without_mutating_input(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.pi import PiTierOptions, build_pi_invocation

        tier = ResolvedTier(model="m", options=PiTierOptions(thinking=False))
        _, system_prompt, _ = build_pi_invocation(
            tier, [{"role": "system", "content": "你是翻译。"}], json_mode=True
        )

        self.assertIn("valid JSON", system_prompt)

    def test_usage_normalization(self):
        from trans_novel.llm.providers.pi import normalize_pi_usage

        usage = {
            "input": 100,
            "output": 20,
            "cacheRead": 30,
            "cacheWrite": 40,
            "totalTokens": 190,
        }
        sample = normalize_pi_usage(usage)

        self.assertEqual(sample.prompt_tokens, 170)
        self.assertEqual(sample.completion_tokens, 20)
        self.assertEqual(sample.total_tokens, 190)
        self.assertEqual(sample.cache_hit_tokens, 30)
        self.assertEqual(sample.cache_miss_tokens, 140)

        fallback_sample = normalize_pi_usage(
            {"input": 100, "output": 20, "cacheRead": 30, "cacheWrite": 40}
        )
        self.assertEqual(fallback_sample.total_tokens, 190)
        self.assertIsNone(normalize_pi_usage(None))

    def test_parse_events_returns_text_and_usage(self):
        import json

        from trans_novel.llm.providers.pi import parse_pi_events

        output = "\n".join(
            [
                json.dumps({"type": "agent_start"}),
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "x"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "你好"}],
                            "usage": {
                                "input": 10,
                                "output": 3,
                                "cacheRead": 4,
                                "totalTokens": 17,
                            },
                            "stopReason": "stop",
                        },
                    }
                ),
            ]
        )

        text, usage = parse_pi_events(output)

        self.assertEqual(text, "你好")
        self.assertEqual(usage.prompt_tokens, 14)
        self.assertEqual(usage.completion_tokens, 3)
        self.assertEqual(usage.cache_hit_tokens, 4)
        self.assertEqual(usage.cache_miss_tokens, 10)

    def test_parse_events_raises_on_error_stop_reason(self):
        import json

        from trans_novel.llm.providers.pi import parse_pi_events

        output = json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": "boom",
                },
            }
        )

        with self.assertRaises(RuntimeError) as context:
            parse_pi_events(output)
        self.assertIn("boom", str(context.exception))

    def test_parse_events_waits_for_successful_automatic_retry(self):
        import json

        from trans_novel.llm.providers.pi import parse_pi_events

        output = "\n".join(
            json.dumps(event)
            for event in [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "partial"}],
                        "usage": {"input": 1, "output": 1, "totalTokens": 2},
                        "stopReason": "error",
                        "errorMessage": "transient failure",
                    },
                },
                {"type": "agent_end"},
                {"type": "auto_retry_start"},
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "final"}],
                        "usage": {"input": 10, "output": 3, "totalTokens": 13},
                        "stopReason": "stop",
                    },
                },
                {"type": "agent_settled"},
            ]
        )

        text, usage = parse_pi_events(output)

        self.assertEqual(text, "final")
        self.assertEqual(usage.prompt_tokens, 10)
        self.assertEqual(usage.completion_tokens, 3)

    def test_parse_events_preserves_unicode_line_separators_in_json(self):
        import json

        from trans_novel.llm.providers.pi import parse_pi_events

        text = "a\u2028b\u2029c"
        output = json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "usage": None,
                    "stopReason": "stop",
                },
            },
            ensure_ascii=False,
        )

        parsed_text, usage = parse_pi_events(output)

        self.assertEqual(parsed_text, text)
        self.assertIsNone(usage)

    def test_parse_events_raises_on_non_jsonl(self):
        from trans_novel.llm.providers.pi import parse_pi_events

        with self.assertRaises(RuntimeError):
            parse_pi_events("not json at all")

        with open(self._pi_log_path, encoding="utf-8") as log_file:
            log = log_file.read()
        self.assertIn("Operation: parse_pi_events", log)
        self.assertIn("not json at all", log)

    def test_complete_logs_cli_failure_with_command_context(self):
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.pi import PiClient

        client = PiClient(LLMConfig(tiers={"strong": {"model": "m"}}, max_retries=0))

        def fake_run(argv, *, input, capture_output, text, timeout, encoding):
            class Result:
                returncode = 2
                stdout = "partial output"
                stderr = "provider failed"

            return Result()

        with (
            patch("shutil.which", return_value="/usr/bin/pi"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            with self.assertRaises(RuntimeError):
                client.complete([{"role": "user", "content": "x"}], stage="Translator")

        with open(self._pi_log_path, encoding="utf-8") as log_file:
            log = log_file.read()
        self.assertIn("Operation: pi_cli_attempt", log)
        self.assertIn("Stage: Translator", log)
        self.assertIn("provider failed", log)
        self.assertIn("partial output", log)

    def test_complete_json_logs_model_response_parse_failure(self):
        import json
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.pi import PiClient

        client = PiClient(LLMConfig(tiers={"strong": {"model": "m"}}))

        def fake_run(argv, *, input, capture_output, text, timeout, encoding):
            class Result:
                returncode = 0
                stdout = json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "not json"}],
                            "usage": None,
                            "stopReason": "stop",
                        },
                    }
                )
                stderr = ""

            return Result()

        with (
            patch("shutil.which", return_value="/usr/bin/pi"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            with self.assertRaises(ValueError):
                client.complete_json([{"role": "user", "content": "x"}], stage="Translator")

        with open(self._pi_log_path, encoding="utf-8") as log_file:
            log = log_file.read()
        self.assertIn("Operation: complete_json_parse", log)
        self.assertIn("Stage: Translator", log)
        self.assertIn("MODEL RESPONSE:", log)
        self.assertIn("not json", log)

    def test_complete_invokes_cli_and_returns_result_text(self):
        import json
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.pi import PiClient

        cfg = LLMConfig(tiers={"strong": {"model": "deepseek-v4-pro"}})
        client = PiClient(cfg)
        captured = {}

        def fake_run(argv, *, input, capture_output, text, timeout, encoding):
            captured["argv"] = argv
            captured["input"] = input
            captured["timeout"] = timeout
            captured["encoding"] = encoding
            prompt_file = argv[argv.index("--system-prompt") + 1]
            with open(prompt_file, "r", encoding="utf-8") as f:
                captured["system_prompt_file_content"] = f.read()

            class Result:
                returncode = 0
                stdout = json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "你好"}],
                            "usage": {"input": 10, "output": 5, "totalTokens": 15},
                            "stopReason": "stop",
                        },
                    }
                )
                stderr = ""

            return Result()

        with (
            patch("shutil.which", return_value=r"C:\nodejs\pi.cmd"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            text = client.complete(
                [
                    {"role": "system", "content": "你是翻译。"},
                    {"role": "user", "content": "hello"},
                ],
                tier="strong",
                stage="Translator",
            )

        self.assertEqual(text, "你好")
        self.assertEqual(captured["encoding"], "utf-8")
        self.assertEqual(captured["input"], "hello")
        self.assertIn(r"C:\nodejs\pi.cmd", captured["argv"])
        self.assertIn("-p", captured["argv"])
        self.assertIn("--no-tools", captured["argv"])
        self.assertIn("--no-session", captured["argv"])
        self.assertIn("--no-extensions", captured["argv"])
        self.assertIn("--no-context-files", captured["argv"])
        self.assertIn("--no-skills", captured["argv"])
        self.assertIn("--no-prompt-templates", captured["argv"])
        self.assertIn("--mode", captured["argv"])
        self.assertEqual(captured["argv"][captured["argv"].index("--model") + 1], "deepseek-v4-pro")
        self.assertIn("--thinking", captured["argv"])
        self.assertEqual(captured["system_prompt_file_content"], "你是翻译。")

        summary = client.usage_summary()
        self.assertEqual(summary["totals"]["prompt_tokens"], 10)
        self.assertEqual(summary["totals"]["completion_tokens"], 5)

    def test_complete_cleans_up_system_prompt_temp_file(self):
        import json
        import os
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.pi import PiClient

        cfg = LLMConfig(tiers={"strong": {"model": "m"}})
        client = PiClient(cfg)
        captured = {}

        def fake_run(argv, *, input, capture_output, text, timeout, encoding):
            prompt_file = argv[argv.index("--system-prompt") + 1]
            captured["prompt_file"] = prompt_file
            self.assertTrue(os.path.exists(prompt_file))

            class Result:
                returncode = 0
                stdout = json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "ok"}],
                            "usage": None,
                            "stopReason": "stop",
                        },
                    }
                )
                stderr = ""

            return Result()

        with (
            patch("shutil.which", return_value="/usr/bin/pi"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            client.complete(
                [
                    {"role": "system", "content": "sys with <angle> brackets"},
                    {"role": "user", "content": "x"},
                ]
            )

        self.assertFalse(os.path.exists(captured["prompt_file"]))

    def test_complete_reports_missing_cli_path(self):
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.pi import PiClient

        cli_path = r"D:\missing\pi.cmd"
        client = PiClient(
            LLMConfig(tiers={"strong": {"model": "m"}}, cli_path=cli_path, max_retries=0)
        )

        with patch("subprocess.run", side_effect=FileNotFoundError(2, "No such file")):
            with self.assertRaises(RuntimeError) as context:
                client.complete([{"role": "user", "content": "x"}])

        message = str(context.exception)
        self.assertIn("pi CLI", message)
        self.assertIn(cli_path, message)

    def test_audit_error_sends_broadcast_email_and_hard_exits(self):
        import json
        from unittest.mock import patch

        from trans_novel.llm.providers.pi import parse_pi_events

        class FakeAuditExit(BaseException):
            def __init__(self, code=1):
                self.code = code

        output = json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": 'OpenAI API error (403): {"message":"内容审计命中风险规则，请调整输入后重试"}',
                },
            }
        )

        with (
            patch("trans_novel.llm.providers.pi._send_audit_broadcast") as mock_broadcast,
            patch("trans_novel.llm.providers.pi._send_audit_email") as mock_email,
            patch(
                "trans_novel.llm.providers.pi._hard_exit_after_audit",
                side_effect=FakeAuditExit(1),
            ) as mock_exit,
        ):
            with self.assertRaises(FakeAuditExit) as ctx:
                parse_pi_events(output)
            self.assertEqual(ctx.exception.code, 1)
            mock_broadcast.assert_called_once()
            mock_email.assert_called_once()
            mock_exit.assert_called_once()

    def test_audit_error_in_complete_does_not_retry(self):
        import json
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.pi import PiClient

        class FakeAuditExit(BaseException):
            def __init__(self, code=1):
                self.code = code

        client = PiClient(LLMConfig(tiers={"strong": {"model": "m"}}, max_retries=3))
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
                            "errorMessage": 'OpenAI API error (403): {"message":"内容审计命中风险规则，请调整输入后重试"}',
                        },
                    }
                )
                stderr = ""

            return Result()

        with (
            patch("shutil.which", return_value="/usr/bin/pi"),
            patch("trans_novel.llm.providers.pi.run_cli_process", side_effect=fake_run),
            patch("trans_novel.llm.providers.pi._send_audit_broadcast"),
            patch("trans_novel.llm.providers.pi._send_audit_email"),
            patch(
                "trans_novel.llm.providers.pi._hard_exit_after_audit",
                side_effect=FakeAuditExit(1),
            ),
        ):
            with self.assertRaises(FakeAuditExit):
                client.complete([{"role": "user", "content": "x"}])

        self.assertEqual(call_count, 1)

    def test_non_audit_error_keeps_existing_behavior(self):
        import json
        from unittest.mock import patch

        from trans_novel.llm.providers.pi import parse_pi_events

        output = json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": "OpenAI API error (500): Internal server error",
                },
            }
        )

        with (
            patch("trans_novel.llm.providers.pi._send_audit_broadcast") as mock_broadcast,
            patch("trans_novel.llm.providers.pi._send_audit_email") as mock_email,
            patch("trans_novel.llm.providers.pi._hard_exit_after_audit") as mock_exit,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                parse_pi_events(output)
            self.assertIn("Internal server error", str(ctx.exception))
            mock_broadcast.assert_not_called()
            mock_email.assert_not_called()
            mock_exit.assert_not_called()

    def test_audit_detection_boundary_payloads_and_cli_exit(self):
        import json
        from unittest.mock import patch

        from trans_novel.llm.providers.pi import (
            _log_pi_error,
            parse_pi_events,
        )

        class FakeAuditExit(BaseException):
            pass

        # Case 1: Successful assistant text contains '审计'
        output = json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "这是一个关于财务审计的故事"}],
                    "usage": None,
                    "stopReason": "stop",
                },
            }
        )
        with (
            patch("trans_novel.llm.providers.pi._send_audit_broadcast") as mock_broadcast,
            patch("trans_novel.llm.providers.pi._send_audit_email") as mock_email,
            patch("trans_novel.llm.providers.pi._hard_exit_after_audit") as mock_exit,
        ):
            text, _ = parse_pi_events(output)
            self.assertEqual(text, "这是一个关于财务审计的故事")
            mock_broadcast.assert_not_called()
            mock_email.assert_not_called()
            mock_exit.assert_not_called()

        # Case 2: Extra payloads (stdout/stderr/response) contain '审计', but error does not
        with (
            patch("trans_novel.llm.providers.pi._send_audit_broadcast") as mock_broadcast,
            patch("trans_novel.llm.providers.pi._send_audit_email") as mock_email,
            patch("trans_novel.llm.providers.pi._hard_exit_after_audit") as mock_exit,
        ):
            logged = _log_pi_error(
                RuntimeError("普通网络连接失败"),
                operation="parse_pi_events",
                stdout="stdout payload contains 审计",
                stderr="stderr payload contains 审计",
                response="model response contains 审计",
            )
            self.assertTrue(logged)
            mock_broadcast.assert_not_called()
            mock_email.assert_not_called()
            mock_exit.assert_not_called()

        # Case 3: CLI non-zero exit message itself contains '审计' -> must trigger
        with (
            patch("trans_novel.llm.providers.pi._send_audit_broadcast") as mock_broadcast,
            patch("trans_novel.llm.providers.pi._send_audit_email") as mock_email,
            patch(
                "trans_novel.llm.providers.pi._hard_exit_after_audit",
                side_effect=FakeAuditExit,
            ) as mock_exit,
        ):
            with self.assertRaises(FakeAuditExit):
                _log_pi_error(
                    RuntimeError("pi CLI 退出码非 0（1）：内容审计命中风险规则"),
                    operation="pi_cli_attempt",
                )
            mock_broadcast.assert_called_once()
            mock_email.assert_called_once()
            mock_exit.assert_called_once()

    def test_channel_failures_do_not_block_each_other_and_still_exit(self):
        from unittest.mock import patch

        from trans_novel.llm.providers.pi import (
            _handle_audit_error,
            _reset_audit_alert_state_for_testing,
        )

        class FakeAuditExit(BaseException):
            pass

        # Case 1: Broadcast fails, email succeeds
        _reset_audit_alert_state_for_testing()
        with (
            patch(
                "trans_novel.llm.providers.pi._send_audit_broadcast",
                side_effect=RuntimeError("广播超时"),
            ),
            patch("trans_novel.llm.providers.pi._send_audit_email") as mock_email,
            patch(
                "trans_novel.llm.providers.pi._hard_exit_after_audit",
                side_effect=FakeAuditExit,
            ) as mock_exit,
        ):
            with self.assertRaises(FakeAuditExit):
                _handle_audit_error(operation="parse_pi_events", error=RuntimeError("命中审计"))
            mock_email.assert_called_once()
            mock_exit.assert_called_once()

        # Case 2: Email fails, broadcast succeeds
        _reset_audit_alert_state_for_testing()
        with (
            patch("trans_novel.llm.providers.pi._send_audit_broadcast") as mock_broadcast,
            patch(
                "trans_novel.llm.providers.pi._send_audit_email",
                side_effect=RuntimeError("邮件连接失败"),
            ),
            patch(
                "trans_novel.llm.providers.pi._hard_exit_after_audit",
                side_effect=FakeAuditExit,
            ) as mock_exit,
        ):
            with self.assertRaises(FakeAuditExit):
                _handle_audit_error(operation="parse_pi_events", error=RuntimeError("命中审计"))
            mock_broadcast.assert_called_once()
            mock_exit.assert_called_once()

        # Case 3: Both fail, still exits
        _reset_audit_alert_state_for_testing()
        with (
            patch(
                "trans_novel.llm.providers.pi._send_audit_broadcast",
                side_effect=RuntimeError("广播失败"),
            ),
            patch(
                "trans_novel.llm.providers.pi._send_audit_email",
                side_effect=RuntimeError("邮件失败"),
            ),
            patch(
                "trans_novel.llm.providers.pi._hard_exit_after_audit",
                side_effect=FakeAuditExit,
            ) as mock_exit,
        ):
            with self.assertRaises(FakeAuditExit):
                _handle_audit_error(operation="parse_pi_events", error=RuntimeError("命中审计"))
            mock_exit.assert_called_once()

    def test_email_config_validation_and_credential_sanitization(self):
        import os
        import tempfile
        from unittest.mock import patch

        from trans_novel.llm.providers.pi import (
            _handle_audit_error,
            _load_audit_mail_config,
            _reset_audit_alert_state_for_testing,
        )

        class FakeAuditExit(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmpdir:
            missing_path = os.path.join(tmpdir, "non_existent.yaml")
            with patch("trans_novel.llm.providers.pi.PI_ALERT_MAIL_CONFIG_FILE", missing_path):
                with self.assertRaises(RuntimeError) as ctx:
                    _load_audit_mail_config()
                self.assertIn("不存在", str(ctx.exception))

            invalid_yaml_path = os.path.join(tmpdir, "invalid.yaml")
            with open(invalid_yaml_path, "w", encoding="utf-8") as f:
                f.write("invalid: [unclosed")
            with patch("trans_novel.llm.providers.pi.PI_ALERT_MAIL_CONFIG_FILE", invalid_yaml_path):
                with self.assertRaises(RuntimeError) as ctx:
                    _load_audit_mail_config()
                self.assertIn("YAML 解析失败", str(ctx.exception))

            missing_field_path = os.path.join(tmpdir, "incomplete.yaml")
            with open(missing_field_path, "w", encoding="utf-8") as f:
                f.write(
                    'smtp_host: "smtp.gmail.com"\nsmtp_port: "465"\npassword: "secret_password"\n'
                )
            with patch(
                "trans_novel.llm.providers.pi.PI_ALERT_MAIL_CONFIG_FILE", missing_field_path
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    _load_audit_mail_config()
                self.assertIn("缺少非空字符串字段", str(ctx.exception))

            # Full run with incomplete config: broadcast still attempted, hard exit still called
            _reset_audit_alert_state_for_testing()
            with (
                patch(
                    "trans_novel.llm.providers.pi.PI_ALERT_MAIL_CONFIG_FILE",
                    missing_field_path,
                ),
                patch("trans_novel.llm.providers.pi._send_audit_broadcast") as mock_broadcast,
                patch(
                    "trans_novel.llm.providers.pi._hard_exit_after_audit",
                    side_effect=FakeAuditExit,
                ) as mock_exit,
            ):
                with self.assertRaises(FakeAuditExit):
                    _handle_audit_error(operation="parse_pi_events", error=RuntimeError("命中审计"))
                mock_broadcast.assert_called_once()
                mock_exit.assert_called_once()

            # Verify no credentials leaked into log
            with open(self._pi_log_path, encoding="utf-8") as log_f:
                log_content = log_f.read()
            self.assertNotIn("secret_password", log_content)

    def test_broadcast_response_status_and_code_validation(self):
        from unittest.mock import MagicMock, patch

        import httpx

        from trans_novel.llm.providers.pi import _send_audit_broadcast

        # Non-2xx response
        mock_resp_500 = MagicMock()
        mock_resp_500.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500 Error", request=MagicMock(), response=mock_resp_500
        )
        with patch("httpx.Client.post", return_value=mock_resp_500):
            with self.assertRaises(httpx.HTTPStatusError):
                _send_audit_broadcast(operation="parse_pi_events", error=RuntimeError("命中审计"))

        # 200 OK but code != 200 in JSON
        mock_resp_code = MagicMock()
        mock_resp_code.raise_for_status.return_value = None
        mock_resp_code.json.return_value = {"code": 403, "message": "forbidden"}
        with patch("httpx.Client.post", return_value=mock_resp_code):
            with self.assertRaises(RuntimeError) as ctx:
                _send_audit_broadcast(operation="parse_pi_events", error=RuntimeError("命中审计"))
            self.assertIn("403", str(ctx.exception))

    def test_audit_timeout_deadline_triggers_hard_exit_without_hanging(self):
        import threading
        from unittest.mock import patch

        from trans_novel.llm.providers.pi import _dispatch_audit_notifications

        hang_event = threading.Event()
        self.addCleanup(hang_event.set)

        def hanging_email(**kwargs):
            hang_event.wait(timeout=1.0)

        # Simulate monotonic clock jumping ahead past deadline
        times = [100.0, 100.0, 120.0, 120.0, 120.0]

        def fake_monotonic():
            if len(times) > 1:
                return times.pop(0)
            return times[0]

        with (
            patch("trans_novel.llm.providers.pi._send_audit_broadcast"),
            patch("trans_novel.llm.providers.pi._send_audit_email", side_effect=hanging_email),
            patch("time.monotonic", side_effect=fake_monotonic),
        ):
            # Must return promptly because remaining deadline <= 0
            _dispatch_audit_notifications(
                operation="parse_pi_events", error=RuntimeError("命中审计")
            )

    def test_concurrent_audit_errors_produce_single_notification_and_both_exit(self):
        import threading
        from unittest.mock import patch

        from trans_novel.llm.providers.pi import (
            _handle_audit_error,
            _reset_audit_alert_state_for_testing,
        )

        class FakeAuditExit(BaseException):
            pass

        _reset_audit_alert_state_for_testing()
        barrier = threading.Barrier(2)
        results = []

        def worker():
            try:
                _handle_audit_error(operation="parse_pi_events", error=RuntimeError("命中审计"))
            except FakeAuditExit:
                results.append("exit")
            except Exception as e:
                results.append(e)

        def controlled_broadcast(**kwargs):
            # Thread 1 enters broadcast, then waits for Thread 2 to start waiting
            barrier.wait(timeout=5.0)

        with (
            patch(
                "trans_novel.llm.providers.pi._send_audit_broadcast",
                side_effect=controlled_broadcast,
            ) as mock_broadcast,
            patch("trans_novel.llm.providers.pi._send_audit_email") as mock_email,
            patch(
                "trans_novel.llm.providers.pi._hard_exit_after_audit",
                side_effect=FakeAuditExit,
            ) as mock_exit,
        ):
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)

            t1.start()
            # Wait until Thread 1 hits the barrier (is in broadcast)
            # Then Thread 2 enters _handle_audit_error while t1 is active
            barrier.wait(timeout=5.0)
            t2.start()

            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

            self.assertEqual(results, ["exit", "exit"])
            mock_broadcast.assert_called_once()
            mock_email.assert_called_once()
            self.assertEqual(mock_exit.call_count, 2)

    def test_excluded_operations_with_audit_keyword_do_not_trigger_alert(self):
        from unittest.mock import patch

        from trans_novel.llm.providers.pi import _log_pi_error

        with (
            patch("trans_novel.llm.providers.pi._send_audit_broadcast") as mock_broadcast,
            patch("trans_novel.llm.providers.pi._send_audit_email") as mock_email,
            patch("trans_novel.llm.providers.pi._hard_exit_after_audit") as mock_exit,
        ):
            for op in ["pi_complete", "complete_json_parse", "pi_temp_file_cleanup"]:
                logged = _log_pi_error(RuntimeError("包含审计的配置或解析错误"), operation=op)
                self.assertTrue(logged)
                mock_broadcast.assert_not_called()
                mock_email.assert_not_called()
                mock_exit.assert_not_called()

    def test_hard_exit_terminates_active_processes_even_on_cleanup_failure(self):
        from unittest.mock import patch

        from trans_novel.llm.providers.pi import _hard_exit_after_audit

        class FakeSysExit(BaseException):
            def __init__(self, code=1):
                self.code = code

        def fake_exit(code=1):
            raise FakeSysExit(code)

        with (
            patch(
                "trans_novel.llm.providers.pi.terminate_all_active_processes",
                side_effect=RuntimeError("子进程树清理异常"),
            ) as mock_term,
            patch("os._exit", side_effect=fake_exit) as mock_exit,
        ):
            with self.assertRaises(FakeSysExit) as ctx:
                _hard_exit_after_audit()
            self.assertEqual(ctx.exception.code, 1)
            mock_term.assert_called_once()
            mock_exit.assert_called_once_with(1)

    def test_hard_exit_cleans_up_registered_prompt_temp_files(self):
        import os
        import tempfile
        from unittest.mock import patch

        from trans_novel.llm.providers.pi import (
            _hard_exit_after_audit,
            _register_prompt_temp_file,
            _reset_audit_alert_state_for_testing,
        )

        class FakeSysExit(BaseException):
            def __init__(self, code=1):
                self.code = code

        def fake_exit(code=1):
            raise FakeSysExit(code)

        _reset_audit_alert_state_for_testing()
        fd1, path1 = tempfile.mkstemp(suffix=".txt")
        os.close(fd1)
        fd2, path2 = tempfile.mkstemp(suffix=".txt")
        os.close(fd2)

        _register_prompt_temp_file(path1)
        _register_prompt_temp_file(path2)
        self.assertTrue(os.path.exists(path1))
        self.assertTrue(os.path.exists(path2))

        # Test failure on deleting one file does not prevent exit
        with (
            patch("trans_novel.llm.providers.pi.terminate_all_active_processes"),
            patch("os.remove", side_effect=[OSError("Permission denied"), None]),
            patch("os._exit", side_effect=fake_exit) as mock_exit,
        ):
            with self.assertRaises(FakeSysExit) as ctx:
                _hard_exit_after_audit()
            self.assertEqual(ctx.exception.code, 1)
            mock_exit.assert_called_once_with(1)

        # Cleanup if files still exist
        for p in (path1, path2):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


class TestCodeBuddyProvider(unittest.TestCase):
    def _tier_config(self, **options):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.codebuddy import CodeBuddyTierOptions

        return ResolvedTier(model="m", options=CodeBuddyTierOptions(**options))

    def test_invocation_splits_system_and_appends_json_instruction(self):
        from trans_novel.llm.providers.codebuddy import build_cli_invocation

        extra_argv, system_prompt, stdin_text = build_cli_invocation(
            self._tier_config(reasoning_effort="medium"),
            [
                {"role": "system", "content": "你是译者。"},
                {"role": "user", "content": "翻译这句话。"},
            ],
            json_mode=True,
        )

        self.assertEqual(extra_argv, ["--model", "m", "--effort", "medium"])
        self.assertIn("你是译者。", system_prompt)
        self.assertIn("valid json", system_prompt)
        self.assertEqual(stdin_text, "翻译这句话。")

    def test_invocation_preserves_roles_for_multiturn_transcript(self):
        from trans_novel.llm.providers.codebuddy import build_cli_invocation

        _, _, stdin_text = build_cli_invocation(
            self._tier_config(thinking=False),
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
                {"role": "user", "content": "third"},
            ],
        )

        self.assertEqual(
            stdin_text,
            "[USER]\nfirst\n\n[ASSISTANT]\nsecond\n\n[USER]\nthird",
        )

    def test_invocation_omits_effort_when_thinking_disabled(self):
        from trans_novel.llm.providers.codebuddy import build_cli_invocation

        extra_argv, _, _ = build_cli_invocation(
            self._tier_config(thinking=False),
            [{"role": "user", "content": "x"}],
        )

        self.assertEqual(extra_argv, ["--model", "m"])

    def test_parse_event_array_result_and_usage(self):
        import json

        from trans_novel.llm.providers.codebuddy import parse_codebuddy_output

        output = json.dumps(
            [
                {"type": "message", "role": "user", "content": []},
                {"type": "message", "role": "assistant", "content": []},
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "你好",
                    "usage": {
                        "input_tokens": 6,
                        "cache_creation_input_tokens": 4,
                        "cache_read_input_tokens": 10,
                        "output_tokens": 3,
                    },
                },
            ]
        )

        text, usage = parse_codebuddy_output(output)

        self.assertEqual(text, "你好")
        self.assertEqual(usage.cache_miss_tokens, 10)
        self.assertEqual(usage.cache_hit_tokens, 10)
        self.assertEqual(usage.prompt_tokens, 20)
        self.assertEqual(usage.total_tokens, 23)

    def test_parse_raises_on_error_result(self):
        import json

        from trans_novel.llm.providers.codebuddy import parse_codebuddy_output

        output = json.dumps([{"type": "result", "is_error": True, "result": "boom"}])

        with self.assertRaises(RuntimeError):
            parse_codebuddy_output(output)

    def test_parse_raises_when_no_result_event(self):
        import json

        from trans_novel.llm.providers.codebuddy import parse_codebuddy_output

        with self.assertRaises(RuntimeError):
            parse_codebuddy_output(json.dumps([{"type": "message"}]))

    def test_complete_invokes_codebuddy_print_mode(self):
        import json
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.codebuddy import CodeBuddyClient

        client = CodeBuddyClient(LLMConfig(tiers={"strong": {"model": "m"}}))
        captured = {}

        def fake_run(argv, *, input, capture_output, text, timeout, encoding):
            captured["argv"] = argv
            captured["input"] = input

            class Result:
                returncode = 0
                stdout = json.dumps(
                    [{"type": "result", "is_error": False, "result": "ok", "usage": None}]
                )
                stderr = ""

            return Result()

        with (
            patch("shutil.which", return_value="/usr/bin/codebuddy"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            result = client.complete(
                [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "x"},
                ]
            )

        argv = captured["argv"]
        self.assertEqual(result, "ok")
        self.assertEqual(argv[0], "/usr/bin/codebuddy")
        self.assertEqual(argv[-1], "-p")
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn('{"mcpServers": {}}', argv)
        # codebuddy CLI 不支持 claude 的 --safe-mode，传了会直接退出码非 0。
        self.assertNotIn("--safe-mode", argv)
        self.assertIn("--system-prompt-file", argv)
        self.assertEqual(captured["input"], "x")

    def test_complete_reports_missing_cli_path(self):
        from unittest.mock import patch

        from trans_novel.config import LLMConfig
        from trans_novel.llm.providers.codebuddy import CodeBuddyClient

        cli_path = r"D:\missing\codebuddy.cmd"
        client = CodeBuddyClient(
            LLMConfig(tiers={"strong": {"model": "m"}}, cli_path=cli_path, max_retries=0)
        )

        with patch("subprocess.run", side_effect=FileNotFoundError(2, "No such file")):
            with self.assertRaises(RuntimeError) as context:
                client.complete([{"role": "user", "content": "x"}])

        message = str(context.exception)
        self.assertIn("CodeBuddy CLI", message)
        self.assertIn(cli_path, message)


class TestProviderFactory(unittest.TestCase):
    def _config(
        self,
        provider: str,
        *,
        base_url: str | None = None,
        reasoning_style: str | None = None,
    ):
        from trans_novel.config import Config

        llm = {
            "provider": provider,
            "tiers": {"strong": {"model": "m"}},
        }
        if base_url is not None:
            llm["base_url"] = base_url
        if reasoning_style is not None:
            llm["reasoning_style"] = reasoning_style
        return Config.from_dict({"llm": llm})

    def test_builds_each_provider_from_its_own_module(self):
        from trans_novel.llm.factory import build_client
        from trans_novel.llm.providers.agy import AgyClient
        from trans_novel.llm.providers.anthropic import AnthropicClient
        from trans_novel.llm.providers.codebuddy import CodeBuddyClient
        from trans_novel.llm.providers.codex import CodexClient
        from trans_novel.llm.providers.ollama import OllamaClient
        from trans_novel.llm.providers.openai import OpenAIClient
        from trans_novel.llm.providers.openai_compatible import (
            OpenAICompatibleClient,
        )
        from trans_novel.llm.providers.openrouter import OpenRouterClient
        from trans_novel.llm.providers.orcarouter import OrcaRouterClient
        from trans_novel.llm.providers.vllm import VLLMClient

        cases = (
            ("openai", OpenAIClient, None),
            ("anthropic", AnthropicClient, None),
            ("codex", CodexClient, None),
            ("codebuddy", CodeBuddyClient, None),
            ("code_buddy", CodeBuddyClient, None),
            ("agy", AgyClient, None),
            ("openrouter", OpenRouterClient, None),
            ("orcarouter", OrcaRouterClient, None),
            ("orca-router", OrcaRouterClient, None),
            ("openai-compatible", OpenAICompatibleClient, "https://example.test/v1"),
            ("ollama", OllamaClient, None),
            ("vllm", VLLMClient, None),
        )
        for provider, expected_type, base_url in cases:
            with self.subTest(provider=provider):
                self.assertIsInstance(
                    build_client(self._config(provider, base_url=base_url)),
                    expected_type,
                )

    def test_unknown_provider_error_mentions_agy(self):
        from trans_novel.config import LLMConfig
        from trans_novel.llm.factory import create_provider_client

        with self.assertRaises(ValueError) as ctx:
            create_provider_client("unknown-provider", LLMConfig())
        self.assertIn("agy", str(ctx.exception))

    def test_orcarouter_defaults_and_api_key_validation(self):
        from trans_novel.llm.factory import build_client
        from trans_novel.llm.providers.orcarouter import OrcaRouterClient

        client = build_client(self._config("orcarouter"))
        assert isinstance(client, OrcaRouterClient)

        self.assertEqual(client.base_url, "https://api.orcarouter.ai/v1")
        self.assertEqual(client.api_key_env, "ORCAROUTER_API_KEY")
        self.assertTrue(client.requires_api_key)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "ORCAROUTER_API_KEY"):
                client.validate_credentials()
        with patch.dict(os.environ, {"ORCAROUTER_API_KEY": "secret"}, clear=True):
            client.validate_credentials()

    def test_local_provider_defaults(self):
        from trans_novel.llm.factory import build_client
        from trans_novel.llm.providers.ollama import OllamaClient
        from trans_novel.llm.providers.vllm import VLLMClient

        ollama = build_client(self._config("ollama"))
        vllm = build_client(self._config("vllm"))
        assert isinstance(ollama, OllamaClient)
        assert isinstance(vllm, VLLMClient)

        self.assertEqual(ollama.base_url, "http://localhost:11434/v1")
        self.assertEqual(vllm.base_url, "http://localhost:8000/v1")
        self.assertFalse(ollama.requires_api_key)
        self.assertFalse(vllm.requires_api_key)

        with patch.dict(os.environ, {}, clear=True):
            ollama.validate_credentials()
            vllm.validate_credentials()

    def test_remote_provider_validates_api_key_before_request(self):
        from trans_novel.llm.factory import build_client

        client = build_client(self._config("deepseek"))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_API_KEY"):
                client.validate_credentials()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "secret"}, clear=True):
            client.validate_credentials()

    def test_generic_provider_requires_base_url(self):
        from trans_novel.llm.factory import build_client

        with self.assertRaisesRegex(ValueError, "base_url"):
            build_client(self._config("openai-compatible"))

    def test_compatible_clients_use_configured_reasoning_style(self):
        from trans_novel.llm.factory import build_client
        from trans_novel.llm.providers.ollama import OllamaClient
        from trans_novel.llm.providers.openai_compatible import (
            OpenAICompatibleClient,
        )
        from trans_novel.llm.providers.vllm import VLLMClient

        compatible = build_client(
            self._config(
                "openai-compatible",
                base_url="https://example.test/v1",
                reasoning_style="deepseek",
            )
        )
        ollama = build_client(self._config("ollama", reasoning_style="openai"))
        vllm = build_client(self._config("vllm", reasoning_style="openrouter"))
        assert isinstance(compatible, OpenAICompatibleClient)
        assert isinstance(ollama, OllamaClient)
        assert isinstance(vllm, VLLMClient)

        self.assertEqual(compatible.reasoning_style, "deepseek")
        self.assertEqual(ollama.reasoning_style, "openai")
        self.assertEqual(vllm.reasoning_style, "openrouter")


class TestRoutedLLMClient(unittest.TestCase):
    def test_build_client_returns_concrete_client_without_tier_provider(self):
        from trans_novel.config import Config
        from trans_novel.llm.factory import build_client
        from trans_novel.llm.providers.deepseek import DeepSeekClient

        cfg = Config.from_dict(
            {
                "llm": {
                    "provider": "deepseek",
                    "tiers": {
                        "strong": {"model": "deepseek-chat"},
                    },
                }
            }
        )
        client = build_client(cfg)
        self.assertIsInstance(client, DeepSeekClient)

    def test_build_client_returns_routed_client_with_tier_provider(self):
        from trans_novel.config import Config
        from trans_novel.llm.factory import build_client
        from trans_novel.llm.router import RoutedLLMClient

        cfg = Config.from_dict(
            {
                "llm": {
                    "provider": "deepseek",
                    "tiers": {
                        "strong": {"provider": "fake", "model": "fake-strong"},
                        "cheap": {"provider": "fake", "model": "fake-cheap"},
                    },
                }
            }
        )
        client = build_client(cfg)
        self.assertIsInstance(client, RoutedLLMClient)

    def test_routed_client_routes_by_tier_and_falls_back(self):
        from trans_novel.config import Config
        from trans_novel.llm.providers.fake import FakeClient
        from trans_novel.llm.router import RoutedLLMClient

        cfg = Config.from_dict(
            {
                "llm": {
                    "provider": "fake",
                    "tiers": {
                        "strong": {"provider": "fake", "model": "strong-model"},
                        "cheap": {"provider": "fake", "model": "cheap-model"},
                    },
                }
            }
        )
        client = RoutedLLMClient(cfg)
        self.assertIn("fake", client.sub_clients)

        sub_fake = client.sub_clients["fake"]
        assert isinstance(sub_fake, FakeClient)

        client.complete([{"role": "user", "content": "hi"}], tier="strong")
        self.assertEqual(sub_fake.calls[-1]["tier"], "strong")

        client.complete([{"role": "user", "content": "hi"}], tier="cheap")
        self.assertEqual(sub_fake.calls[-1]["tier"], "cheap")

        # Fallback: fast -> cheap
        client.complete([{"role": "user", "content": "hi"}], tier="fast")
        self.assertEqual(sub_fake.calls[-1]["tier"], "cheap")

    def test_provider_with_only_cheap_constructs_without_error(self):
        from trans_novel.config import Config
        from trans_novel.llm.router import RoutedLLMClient

        cfg = Config.from_dict(
            {
                "llm": {
                    "provider": "fake",
                    "tiers": {
                        "strong": {"provider": "fake", "model": "fake-strong"},
                        "cheap": {"provider": "deepseek", "model": "deepseek-chat"},
                    },
                }
            }
        )
        client = RoutedLLMClient(cfg)
        self.assertIn("deepseek", client.sub_clients)
        self.assertIn("fake", client.sub_clients)

    def test_routed_client_forwards_events_and_preserves_real_provider(self):
        from trans_novel.config import Config
        from trans_novel.llm.router import RoutedLLMClient

        cfg = Config.from_dict(
            {
                "llm": {
                    "provider": "fake",
                    "tiers": {
                        "strong": {"provider": "fake", "model": "strong-m"},
                        "cheap": {"provider": "deepseek", "model": "deepseek-chat"},
                    },
                }
            }
        )
        client = RoutedLLMClient(cfg)
        events = []
        status_events = []

        client.set_event_sink(lambda event, **kwargs: events.append((event, kwargs)))
        client.set_status_listener(lambda ev: status_events.append(ev))

        client.complete([{"role": "user", "content": "hello"}], tier="strong")
        self.assertTrue(any(ev.tier == "strong" for ev in status_events))

    def test_routed_client_complete_json_updates_last_response(self):
        from trans_novel.config import Config
        from trans_novel.llm.providers.fake import FakeClient
        from trans_novel.llm.router import RoutedLLMClient

        cfg = Config.from_dict(
            {
                "llm": {
                    "provider": "fake",
                    "tiers": {
                        "strong": {"provider": "fake", "model": "strong-m"},
                    },
                }
            }
        )
        client = RoutedLLMClient(cfg)
        sub_fake = client.sub_clients["fake"]
        assert isinstance(sub_fake, FakeClient)
        sub_fake.handler = lambda msgs, tier, json_mode: '{"result": "ok"}'

        res = client.complete_json([{"role": "user", "content": "json request"}])
        self.assertEqual(res, {"result": "ok"})
        self.assertEqual(client.last_json_response(), '{"result": "ok"}')
        self.assertIsNotNone(client.last_json_request_id())

    def test_routed_client_validate_credentials_only_validates_requested_tiers(self):
        from unittest.mock import MagicMock

        from trans_novel.config import Config
        from trans_novel.llm.router import RoutedLLMClient

        cfg = Config.from_dict(
            {
                "llm": {
                    "provider": "fake",
                    "tiers": {
                        "strong": {"provider": "fake", "model": "strong-m"},
                        "cheap": {"provider": "fake", "model": "cheap-m"},
                    },
                }
            }
        )
        client = RoutedLLMClient(cfg)
        mock_sub = MagicMock()
        client.sub_clients["fake"] = mock_sub

        client.validate_credentials(["strong"])
        mock_sub.validate_credentials.assert_called_once_with(["strong"])


if __name__ == "__main__":
    unittest.main()
