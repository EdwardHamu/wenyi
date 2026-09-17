"""只读后文参考、Token 分批与同 transcript 润色测试。"""

from __future__ import annotations

import json
import re

import pytest

from tests.fake_llm import routing_handler
from trans_novel.agents.polisher import Polisher
from trans_novel.agents.translator import Translator
from trans_novel.config import Config
from trans_novel.ingest.models import Segment
from trans_novel.ingest.tokens import count_tokens
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator


@pytest.fixture
def config(tmp_path):
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {
                "provider": "fake",
                "tiers": {
                    "strong": {"model": "pro"},
                    "cheap": {"model": "flash"},
                },
            },
            "paths": {"state_dir": str(tmp_path / "state")},
            "segment": {"max_tokens_per_batch": 1, "max_tokens_per_segment": 0},
            "pipeline": {
                "review": False,
                "polish": False,
                "book_understanding": False,
                "annotation_alignment": False,
                "align_retry_limit": 1,
            },
        }
    )


def _next_source(user: str) -> str:
    marker = "【下一段原文（只读参考，无需翻译）】\n"
    if marker not in user:
        return ""
    reference = user.split(marker, 1)[1].split("\n\n", 1)[0].strip()
    return "" if reference == "无" else json.loads(reference)


def _numbered_sources(user: str) -> list[str]:
    return re.findall(r"^\[\d+\] (.*)$", user, flags=re.MULTILINE)


@pytest.mark.parametrize("source_lang,target_lang", [("en", "zh"), ("zh", "en"), ("ja", "fr")])
def test_following_source_is_quoted_reference_outside_translation_count(
    config, source_lang, target_lang
):
    config.source_lang = source_lang
    config.target_lang = target_lang
    client = FakeClient(handler=lambda m, t, j: '{"translations":["translated fragment"]}')
    reference = 'следующий фрагмент / 続き / 后文\n[99] "quoted"'

    result = Translator(client, config).translate_batch(
        ["unfinished source"], context="previous translation", next_source=reference
    )

    assert result == ["translated fragment"]
    user = client.calls[0]["messages"][-1]["content"]
    assert _next_source(user) == reference
    assert _numbered_sources(user) == ["unfinished source"]
    assert "【前文译文（最近）】\nprevious translation" in user
    assert user.index("【前文译文（最近）】") < user.index("[0] unfinished source")
    assert user.index("[0] unfinished source") < user.index("【下一段原文（只读参考，无需翻译）】")


def test_alignment_retries_and_singletons_use_the_actual_following_source(config):
    references = []

    def handler(messages, tier, json_mode):
        user = messages[-1]["content"]
        sources = _numbered_sources(user)
        references.append((sources, _next_source(user)))
        # 故意制造段数不符触发重试，最终触发单段逐段兜底
        targets = ["first", "second", "extra"] if len(sources) > 1 else ["translated"]
        return json.dumps({"translations": targets})

    result = Translator(FakeClient(handler=handler), config).translate_batch(
        ["first source", "42", "second source"], next_source="outside batch"
    )

    assert result == ["translated", "42", "translated"]
    assert references == [
        (["first source", "second source"], "outside batch"),
        (["first source", "second source"], "outside batch"),
        (["first source"], "42"),
        (["second source"], "outside batch"),
    ]


def test_filtered_trailing_source_takes_precedence_over_external_reference(config):
    client = FakeClient(handler=lambda m, t, j: '{"translations":["translated"]}')
    result = Translator(client, config).translate_batch(
        ["source", "42"], next_source="later paragraph"
    )

    assert result == ["translated", "42"]
    assert _next_source(client.calls[0]["messages"][-1]["content"]) == "42"


def test_polisher_receives_reference_without_extra_output(config):
    client = FakeClient(handler=lambda m, t, j: '{"polished":["unfinished translation"]}')
    result = Polisher(client, config).polish(
        ["unfinished translation"], next_source="continuation source"
    )

    assert result == ["unfinished translation"]
    user = client.calls[0]["messages"][-1]["content"]
    assert _next_source(user) == "continuation source"
    assert _numbered_sources(user) == ["unfinished translation"]


def test_polish_continue_reuses_translation_transcript(config):
    def handler(messages, tier, json_mode):
        user = messages[-1]["content"]
        if "续写润色指令" in user:
            return json.dumps({"polished": ["润色后"]})
        return json.dumps({"translations": ["初译"]})

    client = FakeClient(handler=handler)
    translator = Translator(client, config)
    targets = translator.translate_batch(["source"], next_source="continuation source")
    assert targets == ["初译"]
    assert translator.last_batch_turn is not None
    polished = Polisher(client, config).polish_continue(
        translator.last_batch_turn,
        n=1,
        next_source="continuation source",
    )
    assert polished == ["润色后"]
    assert len(client.calls) == 2
    assert [row["role"] for row in client.calls[1]["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert client.calls[1]["messages"][0]["content"] == client.calls[0]["messages"][0]["content"]
    assert _next_source(client.calls[1]["messages"][-1]["content"]) == "continuation source"


def test_continuation_with_filtered_symbols_covers_only_actual_indices(config):
    config.pipeline.polish = True
    client = FakeClient(handler=routing_handler)
    orch = Orchestrator(config, client=client)
    batch = [
        Segment(index=0, source="文本一"),
        Segment(index=1, source="42"),
        Segment(index=2, source="文本二"),
    ]
    targets = orch._translation.process_batch(
        batch,
        [],
        "",
        "",
        next_source="下一段",
    )
    assert targets == ["润0", "42", "润1"]
    assert [s.target_before_polish for s in batch] == ["译0", "42", "译1"]


def test_fallback_per_segment_causes_standalone_polish_call(config):
    config.pipeline.polish = True

    def handler(messages, tier, json_mode):
        user = messages[-1]["content"]
        n = _count_segments(user)
        if "文学翻译" in messages[0]["content"]:
            # 批量调用故意返回错误段数，强制进入逐段兜底
            if n > 1:
                return json.dumps({"translations": ["单一译文"]})
            return json.dumps({"translations": [f"逐段译{n}"]})
        if "中文润色编辑" in messages[0]["content"]:
            return json.dumps({"polished": [f"独立润{i}" for i in range(n)]})
        return routing_handler(messages, tier, json_mode)

    client = FakeClient(handler=handler)
    orch = Orchestrator(config, client=client)
    batch = [
        Segment(index=0, source="段落一"),
        Segment(index=1, source="段落二"),
    ]
    targets = orch._translation.process_batch(
        batch,
        [],
        "",
        "",
        next_source="下一段",
    )
    assert targets == ["独立润0", "独立润1"]
    polish_calls = [c for c in client.calls if c["stage"] == "Polisher"]
    assert len(polish_calls) == 1
    assert [row["role"] for row in polish_calls[0]["messages"]] == ["system", "user"]


def test_continuation_failure_falls_back_to_standalone_polish(config):
    config.pipeline.polish = True

    def handler(messages, tier, json_mode):
        user = messages[-1]["content"]
        if "续写润色指令" in user:
            # 续写返回段数不符触发回退
            return json.dumps({"polished": ["只有一段"]})
        if "中文润色编辑" in messages[0]["content"]:
            n = len(re.findall(r"^\[\d+\]", user, re.MULTILINE))
            return json.dumps({"polished": [f"兜底润{i}" for i in range(n)]})
        return routing_handler(messages, tier, json_mode)

    client = FakeClient(handler=handler)
    orch = Orchestrator(config, client=client)
    batch = [
        Segment(index=0, source="段落一"),
        Segment(index=1, source="段落二"),
    ]
    targets = orch._translation.process_batch(
        batch,
        [],
        "",
        "",
        next_source="下一段",
    )
    assert targets == ["兜底润0", "兜底润1"]
    polish_calls = [c for c in client.calls if c["stage"] == "Polisher"]
    # 第一次续写尝试失败，第二次独立润色成功
    assert len(polish_calls) == 2
    assert [row["role"] for row in polish_calls[0]["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert [row["role"] for row in polish_calls[1]["messages"]] == ["system", "user"]


def test_both_continuation_and_standalone_polish_failure_keeps_original_translation(config):
    config.pipeline.polish = True

    def handler(messages, tier, json_mode):
        user = messages[-1]["content"]
        if "续写润色指令" in user:
            raise RuntimeError("续写请求异常")
        if "中文润色编辑" in messages[0]["content"]:
            # 独立润色也返回畸形
            return json.dumps({"polished": ["数量不符"]})
        return routing_handler(messages, tier, json_mode)

    client = FakeClient(handler=handler)
    orch = Orchestrator(config, client=client)
    batch = [
        Segment(index=0, source="段落一"),
        Segment(index=1, source="段落二"),
    ]
    targets = orch._translation.process_batch(
        batch,
        [],
        "",
        "",
        next_source="下一段",
    )
    assert targets == ["译0", "译1"]
    assert [s.target_before_polish for s in batch] == ["译0", "译1"]


def _count_segments(text: str) -> int:
    return len(re.findall(r"^\[(\d+)\]", text, re.MULTILINE))


@pytest.mark.parametrize("recent_count", [0, 2])
def test_split_fragments_and_chapter_ends_supply_one_reference_to_both_stages(
    tmp_path, config, recent_count
):
    config.pipeline.polish = True
    config.pipeline.rolling_context_segments = recent_count
    # 10 token 预算强制拆分超长段
    config.segment.max_tokens_per_segment = 10
    source = tmp_path / "book.md"
    source.write_text(
        "# First\n\nShe knew that the answer would arrive after the long winter had ended."
        "\n\n# Second\n\nA different scene begins here.",
        encoding="utf-8",
    )
    client = FakeClient(handler=routing_handler)
    orch = Orchestrator(config, client=client)
    store = orch.prepare(str(source))
    before = [store.load_chapter(index) for index in (0, 1)]
    assert any(segment.cont for chapter in before for segment in chapter.text_segments)
    expected = [
        chapter.text_segments[index + 1].source if index + 1 < len(chapter.text_segments) else ""
        for chapter in before
        for index in range(len(chapter.text_segments))
    ]

    orch.run(str(source))

    translation_calls = [call for call in client.calls if call["stage"] == "Translator"]
    polish_calls = [call for call in client.calls if call["stage"] == "Polisher"]
    assert [_next_source(call["messages"][-1]["content"]) for call in translation_calls] == expected
    assert all(
        len(_numbered_sources(call["messages"][-1]["content"])) == 1 for call in translation_calls
    )
    assert [_next_source(call["messages"][-1]["content"]) for call in polish_calls] == expected
    for call in polish_calls:
        roles = [row["role"] for row in call["messages"]]
        assert roles == ["system", "user", "assistant", "user"]
        assert "续写润色指令" in call["messages"][-1]["content"]
    for chapter in before:
        after = store.load_chapter(chapter.index)
        assert [(s.index, s.source, s.cont) for s in after.segments] == [
            (s.index, s.source, s.cont) for s in chapter.segments
        ]
        assert all(s.target == "润0" for s in after.text_segments)
    context = store.load_context()
    assert context is not None
    assert set(context["recent_targets"]) == {"润0"}


def test_resume_rebuilds_reference_after_batch_budget_change_without_saving_it_early(
    tmp_path, config
):
    sources = [
        "First unfinished part",
        "Second source segment",
        "Third source sentence",
        "Final part.",
    ]
    source = tmp_path / "book.txt"
    source.write_text("\n\n".join(sources), encoding="utf-8")
    count = 0

    def interrupted(messages, tier, json_mode):
        nonlocal count
        if "文学翻译" in messages[0]["content"]:
            count += 1
            if count == 2:
                raise RuntimeError("simulated interruption")
        return routing_handler(messages, tier, json_mode)

    client = FakeClient(handler=interrupted)
    orch = Orchestrator(config, client=client)
    store = orch.prepare(str(source))
    with pytest.raises(RuntimeError, match="simulated interruption"):
        orch.run(str(source))
    partial = store.load_chapter(0)
    assert partial.text_segments[0].target == "译0"
    assert all(segment.target is None for segment in partial.text_segments[1:])
    first_call = next(call for call in client.calls if call["stage"] == "Translator")
    assert _next_source(first_call["messages"][-1]["content"]) == sources[1]

    config.segment.max_tokens_per_batch = count_tokens(sources[0]) + count_tokens(sources[1])
    resumed = FakeClient(handler=routing_handler)
    Orchestrator(config, client=resumed).run(str(source))
    calls = [call for call in resumed.calls if call["stage"] == "Translator"]
    assert [_numbered_sources(call["messages"][-1]["content"]) for call in calls] == [
        [sources[1]],
        sources[2:],
    ]
    assert [_next_source(call["messages"][-1]["content"]) for call in calls] == [sources[2], ""]
    assert "【前文译文（最近）】\n译0" in calls[0]["messages"][-1]["content"]
    assert store.load_chapter(0).text_segments[0].target == "译0"
    assert all(segment.target for segment in store.load_chapter(0).text_segments)

    completed = FakeClient(handler=routing_handler)
    Orchestrator(config, client=completed).run(str(source))
    assert not [call for call in completed.calls if call["stage"] == "Translator"]
