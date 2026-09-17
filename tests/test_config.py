"""配置文件创建与加载测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trans_novel.config import Config


class TestConfigFileCreation(unittest.TestCase):
    def test_create_default_file_can_be_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "nested" / "config.yaml"
            created = Config.create_default_file(str(path))
            cfg = Config.load(str(path))

            self.assertTrue(created)
            self.assertTrue(path.is_file())
            self.assertEqual(cfg.llm.provider, "deepseek")
            self.assertEqual(cfg.llm.base_url, "https://api.deepseek.com")
            self.assertEqual(cfg.llm.api_key_env, "DEEPSEEK_API_KEY")
            self.assertEqual(set(cfg.llm.tiers), {"strong", "cheap", "fast"})
            self.assertEqual(cfg.llm.tiers["strong"].model, "deepseek-v4-pro")
            self.assertEqual(cfg.llm.tiers["cheap"].model, "deepseek-v4-flash")
            self.assertEqual(cfg.llm.tiers["fast"].model, "deepseek-v4-flash")
            self.assertTrue(cfg.llm.tiers["fast"].options["thinking"])
            self.assertFalse(hasattr(cfg.llm, "api_key"))
            generated = path.read_text(encoding="utf-8")
            self.assertIn("# trans-novel 配置", generated)
            self.assertIn("  base_url: https://api.deepseek.com", generated)
            self.assertIn("  api_key_env: DEEPSEEK_API_KEY", generated)
            self.assertIn("  tiers:\n", generated)
            self.assertIn("output:\n", generated)
            self.assertTrue(cfg.output.mono)
            self.assertFalse(cfg.output.bilingual)
            self.assertEqual(cfg.output.bilingual_order, "target_first")
            self.assertFalse(cfg.output.bilingual_preserve_source_style)
            self.assertTrue(cfg.output.about_page)
            self.assertFalse(cfg.pipeline.review)
            self.assertTrue(cfg.pipeline.polish)
            self.assertTrue(cfg.pipeline.annotation_alignment)
            self.assertEqual(cfg.pipeline.review_concurrency, 4)
            self.assertEqual(cfg.pipeline.review_output_retries, 2)
            self.assertTrue(cfg.pipeline.review_agent_loop)
            self.assertEqual(cfg.pipeline.review_agent_tier, "strong")
            self.assertEqual(cfg.pipeline.review_agent_max_evidence_rounds, 2)
            self.assertTrue(cfg.pipeline.review_conflict_arbitration)
            self.assertTrue(cfg.pipeline.review_fix_loop)
            self.assertEqual(cfg.pipeline.review_fix_max_rounds, 2)
            self.assertEqual(cfg.pipeline.review_clean_confirmations, 2)
            self.assertFalse(cfg.pipeline.review_autofix)
            self.assertEqual(cfg.pipeline.pdf_backend, "mineru")
            self.assertEqual(cfg.segment.max_tokens_per_batch, 1800)
            self.assertEqual(cfg.segment.max_tokens_per_segment, 1200)
            self.assertIn("max_tokens_per_batch: 1800", generated)
            self.assertIn("max_tokens_per_segment: 1200", generated)
            self.assertNotIn("max_chars_per_batch", generated)

    def test_removed_segment_char_keys_are_rejected(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            Config.from_dict({"segment": {"max_chars_per_batch": 99}})
        with self.assertRaises(ValidationError):
            Config.from_dict({"segment": {"max_chars_per_segment": 99}})

    def test_root_configs_load_successfully(self):
        cfg1 = Config.load("config.yaml")
        self.assertEqual(cfg1.segment.max_tokens_per_batch, 1800)
        cfg2 = Config.load("config2.yaml")
        self.assertEqual(cfg2.segment.max_tokens_per_batch, 1800)

    def test_load_never_overwrites_existing_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.yaml"
            path.write_text("language:\n  source: en\n  target: zh\n", encoding="utf-8")

            cfg = Config.load(str(path))

            self.assertEqual(cfg.source_lang, "en")
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "language:\n  source: en\n  target: zh\n",
            )

    def test_partial_config_uses_yaml_pipeline_defaults(self):
        """缺失的流水线字段必须与自动生成的 YAML 默认值一致。"""
        cfg = Config.from_dict({"pipeline": {"review": False}})

        self.assertFalse(cfg.pipeline.review)
        self.assertTrue(cfg.pipeline.polish)
        self.assertTrue(cfg.pipeline.annotation_alignment)
        self.assertEqual(cfg.pipeline.review_concurrency, 4)
        self.assertEqual(cfg.pipeline.review_output_retries, 2)
        self.assertTrue(cfg.pipeline.review_agent_loop)
        self.assertEqual(cfg.pipeline.review_agent_tier, "strong")
        self.assertEqual(cfg.pipeline.review_agent_max_evidence_rounds, 2)
        self.assertTrue(cfg.pipeline.review_conflict_arbitration)
        self.assertTrue(cfg.pipeline.review_fix_loop)
        self.assertEqual(cfg.pipeline.review_fix_max_rounds, 2)
        self.assertEqual(cfg.pipeline.review_clean_confirmations, 2)
        self.assertFalse(cfg.pipeline.review_autofix)
        self.assertEqual(cfg.pipeline.pdf_backend, "mineru")

    def test_about_page_can_be_disabled(self):
        cfg = Config.from_dict({"output": {"about_page": False}})

        self.assertFalse(cfg.output.about_page)

    def test_compatible_reasoning_style_is_loaded(self):
        cfg = Config.from_dict(
            {
                "llm": {
                    "provider": "openai-compatible",
                    "reasoning_style": "deepseek",
                }
            }
        )

        self.assertEqual(cfg.llm.reasoning_style, "deepseek")

    def test_llm_cli_path_defaults_to_none_and_can_be_set(self):
        default_cfg = Config.from_dict({"llm": {"provider": "codex"}})
        self.assertIsNone(default_cfg.llm.cli_path)

        custom_cfg = Config.from_dict(
            {"llm": {"provider": "codex", "cli_path": r"C:\tools\codex.cmd"}}
        )
        self.assertEqual(custom_cfg.llm.cli_path, r"C:\tools\codex.cmd")

    def test_tier_provider_and_connection_overrides_loaded(self):
        cfg = Config.from_dict(
            {
                "llm": {
                    "provider": "deepseek",
                    "base_url": "https://api.deepseek.com",
                    "api_key_env": "DEEPSEEK_API_KEY",
                    "cli_path": r"C:\global\cli.cmd",
                    "reasoning_style": "deepseek",
                    "tiers": {
                        "strong": {
                            "provider": "codex",
                            "model": "gpt-5.6-sol",
                            "cli_path": r"C:\custom\codex.cmd",
                        },
                        "cheap": {
                            "provider": "openai-compatible",
                            "model": "custom-model",
                            "base_url": "https://custom.endpoint/v1",
                            "api_key_env": "CUSTOM_KEY",
                            "reasoning_style": "openai",
                        },
                        "fast": {
                            "model": "fast-model",
                        },
                    },
                }
            }
        )
        strong = cfg.llm.tiers["strong"]
        self.assertEqual(strong.provider, "codex")
        self.assertEqual(strong.model, "gpt-5.6-sol")
        self.assertEqual(strong.cli_path, r"C:\custom\codex.cmd")
        self.assertIsNone(strong.base_url)

        cheap = cfg.llm.tiers["cheap"]
        self.assertEqual(cheap.provider, "openai-compatible")
        self.assertEqual(cheap.base_url, "https://custom.endpoint/v1")
        self.assertEqual(cheap.api_key_env, "CUSTOM_KEY")
        self.assertEqual(cheap.reasoning_style, "openai")

        fast = cfg.llm.tiers["fast"]
        self.assertIsNone(fast.provider)
        self.assertIsNone(fast.base_url)

    def test_agy_provider_and_cli_path_loaded(self):
        cfg = Config.from_dict(
            {
                "llm": {
                    "provider": "agy",
                    "cli_path": r"C:\tools\agy.cmd",
                    "tiers": {
                        "strong": {
                            "model": "gemini-3.1-pro",
                            "options": {"reasoning_effort": "high"},
                        },
                        "cheap": {
                            "model": "gemini-3-flash",
                            "cli_path": r"C:\custom\agy.cmd",
                            "options": {"reasoning_effort": "low"},
                        },
                    },
                }
            }
        )
        self.assertEqual(cfg.llm.provider, "agy")
        self.assertEqual(cfg.llm.cli_path, r"C:\tools\agy.cmd")
        self.assertEqual(cfg.llm.tiers["strong"].model, "gemini-3.1-pro")
        self.assertEqual(cfg.llm.tiers["strong"].options["reasoning_effort"], "high")
        self.assertEqual(cfg.llm.tiers["cheap"].cli_path, r"C:\custom\agy.cmd")

    def test_tier_config_forbids_extra_fields(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            Config.from_dict(
                {
                    "llm": {
                        "tiers": {
                            "strong": {
                                "model": "test",
                                "unknown_field": "invalid",
                            }
                        }
                    }
                }
            )


class TestPriorityConfig(unittest.TestCase):
    def test_legacy_llm_format_converts_to_list_and_priority_0(self):
        cfg = Config.from_dict(
            {
                "llm": {
                    "provider": "deepseek",
                    "base_url": "https://api.deepseek.com",
                    "tiers": {"strong": {"model": "deepseek-chat"}},
                }
            }
        )
        self.assertEqual(len(cfg.llm_list), 1)
        self.assertEqual(cfg.llm_priority, "0")
        self.assertEqual(cfg.llm.provider, "deepseek")
        self.assertEqual(cfg.llm.tiers["strong"].model, "deepseek-chat")

    def test_llm_list_dynamic_default_priority(self):
        # 1 item
        cfg1 = Config.from_dict({"llm_list": [{"provider": "deepseek"}]})
        self.assertEqual(cfg1.llm_priority, "0")

        # 2 items
        cfg2 = Config.from_dict({"llm_list": [{"provider": "deepseek"}, {"provider": "agy"}]})
        self.assertEqual(cfg2.llm_priority, "01")

        # 3 items
        cfg3 = Config.from_dict(
            {
                "llm_list": [
                    {"provider": "deepseek"},
                    {"provider": "agy"},
                    {"provider": "pi"},
                ]
            }
        )
        self.assertEqual(cfg3.llm_priority, "012")

    def test_llm_priority_reordering(self):
        cfg = Config.from_dict(
            {
                "llm_priority": "10",
                "llm_list": [
                    {"provider": "pi", "tiers": {"strong": {"model": "pi-model"}}},
                    {"provider": "agy", "tiers": {"strong": {"model": "agy-model"}}},
                ],
            }
        )
        self.assertEqual(cfg.llm_priority, "10")
        # cfg.llm must return highest-priority item (index 1 = agy)
        self.assertEqual(cfg.llm.provider, "agy")
        self.assertEqual(cfg.llm.tiers["strong"].model, "agy-model")

    def test_llm_setter_updates_active_config(self):
        cfg = Config.from_dict(
            {
                "llm_priority": "10",
                "llm_list": [
                    {"provider": "pi"},
                    {"provider": "agy"},
                ],
            }
        )
        new_llm = cfg.llm.model_copy(deep=True)
        new_llm.provider = "codex"
        cfg.llm = new_llm
        self.assertEqual(cfg.llm.provider, "codex")
        self.assertEqual(cfg.llm_list[1].provider, "codex")
        self.assertEqual(cfg.llm_list[0].provider, "pi")

    def test_llm_and_llm_list_conflict_error(self):
        with self.assertRaisesRegex(ValueError, "不能同时配置 llm 与 llm_list"):
            Config.from_dict(
                {
                    "llm": {"provider": "deepseek"},
                    "llm_list": [{"provider": "agy"}],
                }
            )

    def test_llm_priority_duplicate_index_error(self):
        with self.assertRaisesRegex(ValueError, "重复索引"):
            Config.from_dict(
                {
                    "llm_priority": "00",
                    "llm_list": [{"provider": "pi"}, {"provider": "agy"}],
                }
            )

    def test_llm_priority_missing_index_error(self):
        with self.assertRaisesRegex(ValueError, "一致|缺失"):
            Config.from_dict(
                {
                    "llm_priority": "0",
                    "llm_list": [{"provider": "pi"}, {"provider": "agy"}],
                }
            )

    def test_llm_priority_out_of_bounds_error(self):
        with self.assertRaisesRegex(ValueError, "越界"):
            Config.from_dict(
                {
                    "llm_priority": "02",
                    "llm_list": [{"provider": "pi"}, {"provider": "agy"}],
                }
            )

    def test_llm_priority_unquoted_error(self):
        # In YAML, llm_priority: 01 parses as int 1, llm_priority: 10 parses as int 10
        with self.assertRaisesRegex(ValueError, "带引号的字符串"):
            Config.from_dict(
                {
                    "llm_priority": 1,
                    "llm_list": [{"provider": "pi"}, {"provider": "agy"}],
                }
            )

    def test_llm_list_over_ten_items_error(self):
        items = [{"provider": "deepseek"}] * 11
        with self.assertRaisesRegex(ValueError, "1 到 10 项之间"):
            Config.from_dict({"llm_list": items})

    def test_llm_list_empty_error(self):
        with self.assertRaisesRegex(ValueError, "1 到 10 项之间"):
            Config.from_dict({"llm_list": []})


if __name__ == "__main__":
    unittest.main()
