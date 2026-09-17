"""原生 CLI conversation 的隔离、路由、协议与回退测试。"""

from __future__ import annotations

import concurrent.futures
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from trans_novel.agents.polisher import Polisher
from trans_novel.agents.translator import Translator
from trans_novel.config import Config, LLMConfig
from trans_novel.llm.base import (
    ConversationCompletion,
    JsonConversationCompletion,
    LLMClient,
    Messages,
    NativeConversation,
    NativeConversationBusyError,
    NativeConversationInvalidError,
    NativeConversationRouteChangedError,
    NativeConversationUnsupportedError,
    conversation_fingerprint,
)
from trans_novel.llm.priority import PriorityLLMClient
from trans_novel.llm.providers.agy import AgyClient, _parse_agy_events_full
from trans_novel.llm.providers.pi import PiClient
from trans_novel.llm.router import RoutedLLMClient


def _native_handle(
    owner: LLMClient,
    *,
    native_id: str = "session-1",
    config_index: int | None = None,
) -> NativeConversation:
    return NativeConversation(
        provider=owner.provider_name or type(owner).__name__,
        config_index=owner.config_index if config_index is None else config_index,
        tier="strong",
        model="model-1",
        native_id=native_id,
        cwd=os.path.abspath(os.getcwd()),
        prefix_hash="prefix",
        _owner=owner,
    )


def _pi_output(text: str) -> str:
    return json.dumps(
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


def _pi_error_output(message: str) -> str:
    return json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [],
                "usage": None,
                "stopReason": "error",
                "errorMessage": message,
            },
        },
        ensure_ascii=False,
    )


def _write_pi_session(argv: list[str], *, cwd: str | None) -> str:
    session_id = argv[argv.index("--session-id") + 1]
    session_dir = Path(argv[argv.index("--session-dir") + 1])
    session_file = session_dir / f"20260917_{session_id}.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": session_id,
                "cwd": cwd,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return str(session_file)


def _agy_output(
    conversation_id: str | None,
    text: str,
    *,
    result_conversation_id: str | None = None,
    include_init: bool = True,
) -> str:
    events: list[dict[str, object]] = []
    if include_init:
        init: dict[str, object] = {"event": "init"}
        if conversation_id is not None:
            init["conversation_id"] = conversation_id
        events.append(init)
    result: dict[str, object] = {"status": "SUCCESS", "response": text}
    result_id = conversation_id if result_conversation_id is None else result_conversation_id
    if result_id is not None:
        result["conversation_id"] = result_id
    events.append({"event": "result", "result": result})
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)


class _CompatClient(LLMClient):
    def __init__(self) -> None:
        super().__init__()
        self.provider_name = "compat"
        self.calls: list[tuple[Messages, str, bool]] = []

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> str:
        del max_tokens, stage
        self.calls.append((messages, tier, json_mode))
        return '{"ok": true}'


class _RouteClient(LLMClient):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.provider_name = name
        self.continued: list[str] = []

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> str:
        del messages, tier, json_mode, max_tokens, stage
        return "unused"

    def continue_conversation(
        self,
        handle: NativeConversation,
        user_message: str,
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> ConversationCompletion:
        del json_mode, max_tokens, stage
        handle.claim(self)
        self.continued.append(user_message)
        self.close_conversation(handle)
        return ConversationCompletion(f"{self.provider_name}:{user_message}")


def test_default_start_conversation_is_backward_compatible() -> None:
    client = _CompatClient()
    messages = [{"role": "user", "content": "hello"}]

    completion = client.start_json_conversation(messages, tier="strong")

    assert completion.data == {"ok": True}
    assert completion.text == '{"ok": true}'
    assert completion.handle is None
    assert client.calls == [(messages, "strong", True)]


def test_native_handle_allows_only_one_concurrent_consumer() -> None:
    client = _CompatClient()
    handle = _native_handle(client)
    start_barrier = threading.Barrier(3)
    close_barrier = threading.Barrier(3)

    def claim() -> str:
        start_barrier.wait()
        try:
            handle.claim(client)
        except NativeConversationBusyError:
            with pytest.raises(NativeConversationBusyError):
                handle.mark_closed(client)
            outcome = "busy"
        else:
            outcome = "claimed"
        close_barrier.wait()
        if outcome == "claimed":
            handle.mark_closed(client)
        return outcome

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim) for _ in range(2)]
        start_barrier.wait()
        close_barrier.wait()
        outcomes = [future.result() for future in futures]

    assert sorted(outcomes) == ["busy", "claimed"]
    assert handle.state == "closed"
    assert not handle.mark_closed(client)


def test_router_continuation_returns_to_concrete_owner() -> None:
    first = _RouteClient("first")
    second = _RouteClient("second")
    router = object.__new__(RoutedLLMClient)
    LLMClient.__init__(router)
    router.sub_clients = {"first": first, "second": second}
    handle = _native_handle(second)

    completion = router.continue_conversation(handle, "increment")

    assert completion.text == "second:increment"
    assert first.continued == []
    assert second.continued == ["increment"]


def test_priority_rejects_continuation_after_route_change() -> None:
    config = Config.from_dict(
        {
            "llm_priority": "01",
            "llm_list": [{"provider": "fake"}, {"provider": "fake"}],
        }
    )
    first = _RouteClient("first")
    second = _RouteClient("second")
    with patch(
        "trans_novel.llm.priority._build_single_client",
        side_effect=[first, second],
    ):
        priority = PriorityLLMClient(config)
    handle = _native_handle(first)
    priority._current_step = 1

    with pytest.raises(NativeConversationRouteChangedError):
        priority.continue_conversation(handle, "must-not-be-rerouted")

    assert first.continued == []
    assert second.continued == []
    priority.close_conversation(handle)
    assert handle.state == "closed"


def test_pi_start_and_incremental_continue_reuse_exact_session() -> None:
    client = PiClient(
        LLMConfig(
            cli_path="pi-test",
            tiers={
                "strong": {
                    "model": "pi-model",
                    "options": {"reasoning_effort": "medium"},
                }
            },
            max_retries=0,
        )
    )
    calls: list[dict[str, object]] = []

    def fake_run(
        argv,
        *,
        input_text=None,
        timeout=None,
        provider=None,
        cwd=None,
    ):
        del timeout, provider
        argv = list(argv)
        prompt = ""
        if "--system-prompt" in argv:
            prompt = Path(argv[argv.index("--system-prompt") + 1]).read_text(encoding="utf-8")
        calls.append({"argv": argv, "input": input_text, "cwd": cwd, "prompt": prompt})
        if "--session-id" in argv:
            _write_pi_session(argv, cwd=cwd)
            text = '{"translations":["译文"]}'
        else:
            text = '{"polished":["润色"]}'
        return SimpleNamespace(returncode=0, stdout=_pi_output(text), stderr="")

    messages = [
        {"role": "system", "content": "fixed system"},
        {"role": "user", "content": "first user"},
    ]
    with patch("trans_novel.llm.providers.pi.run_cli_process", side_effect=fake_run):
        started = client.start_conversation(messages, json_mode=True)
        assert started.handle is not None
        session_dir = calls[0]["argv"][calls[0]["argv"].index("--session-dir") + 1]
        continued = client.continue_conversation(
            started.handle,
            "only the new user turn",
            json_mode=True,
        )

    start_argv = calls[0]["argv"]
    continue_argv = calls[1]["argv"]
    assert "--session-id" in start_argv
    assert "--session-dir" in start_argv
    assert "--no-session" not in start_argv
    assert "--session" in continue_argv
    assert "--session-id" not in continue_argv
    assert "--continue" not in continue_argv
    assert calls[0]["input"] == "first user"
    assert calls[1]["input"] == "only the new user turn"
    assert calls[0]["cwd"] == calls[1]["cwd"]
    assert calls[0]["prompt"] == calls[1]["prompt"]
    assert start_argv[start_argv.index("--model") + 1] == "pi-model"
    assert continue_argv[continue_argv.index("--model") + 1] == "pi-model"
    assert start_argv[start_argv.index("--thinking") + 1] == "medium"
    assert continue_argv[continue_argv.index("--thinking") + 1] == "medium"
    assert continued.text == '{"polished":["润色"]}'
    assert started.handle.state == "closed"
    assert not Path(session_dir).exists()


def test_pi_fresh_retry_uses_new_id_and_private_directory() -> None:
    client = PiClient(
        LLMConfig(
            cli_path="pi-test",
            tiers={"strong": {"model": "pi-model"}},
            max_retries=1,
        )
    )
    ids: list[str] = []
    dirs: list[Path] = []

    def fake_run(argv, **kwargs):
        argv = list(argv)
        ids.append(argv[argv.index("--session-id") + 1])
        dirs.append(Path(argv[argv.index("--session-dir") + 1]))
        if len(ids) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="transient")
        _write_pi_session(argv, cwd=kwargs.get("cwd"))
        return SimpleNamespace(returncode=0, stdout=_pi_output("ok"), stderr="")

    with (
        patch("trans_novel.llm.providers.pi.run_cli_process", side_effect=fake_run),
        patch("trans_novel.llm.providers.pi._log_pi_error"),
        patch("trans_novel.llm.providers.pi.wait_exponential", return_value=lambda _state: 0),
    ):
        completion = client.start_conversation([{"role": "user", "content": "x"}])

    assert completion.handle is not None
    assert len(ids) == 2
    assert ids[0] != ids[1]
    assert dirs[0] != dirs[1]
    assert not dirs[0].exists()
    assert dirs[1].exists()
    client.close_conversation(completion.handle)
    assert not dirs[1].exists()


def test_pi_continuation_is_not_retried_and_always_cleans_up() -> None:
    client = PiClient(
        LLMConfig(
            cli_path="pi-test",
            tiers={"strong": {"model": "pi-model"}},
            max_retries=3,
        )
    )
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        argv = list(argv)
        calls.append(argv)
        if "--session-id" in argv:
            _write_pi_session(argv, cwd=kwargs.get("cwd"))
            return SimpleNamespace(returncode=0, stdout=_pi_output("first"), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="resume failed")

    with (
        patch("trans_novel.llm.providers.pi.run_cli_process", side_effect=fake_run),
        patch("trans_novel.llm.providers.pi._log_pi_error"),
    ):
        started = client.start_conversation([{"role": "user", "content": "x"}])
        assert started.handle is not None
        session_dir = Path(calls[0][calls[0].index("--session-dir") + 1])
        with pytest.raises(RuntimeError, match="resume failed"):
            client.continue_conversation(started.handle, "new")

    assert len(calls) == 2
    assert started.handle.state == "closed"
    assert not session_dir.exists()


def test_pi_rejects_missing_session_file_before_resume() -> None:
    client = PiClient(
        LLMConfig(
            cli_path="pi-test",
            tiers={"strong": {"model": "pi-model"}},
            max_retries=0,
        )
    )
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        argv = list(argv)
        calls.append(argv)
        _write_pi_session(argv, cwd=kwargs.get("cwd"))
        return SimpleNamespace(returncode=0, stdout=_pi_output("first"), stderr="")

    with patch("trans_novel.llm.providers.pi.run_cli_process", side_effect=fake_run):
        started = client.start_conversation([{"role": "user", "content": "x"}])
        assert started.handle is not None
        session_dir = Path(calls[0][calls[0].index("--session-dir") + 1])
        for session_file in session_dir.rglob("*.jsonl"):
            session_file.unlink()
        with pytest.raises(NativeConversationInvalidError):
            client.continue_conversation(started.handle, "new")

    assert len(calls) == 1
    assert started.handle.state == "closed"
    assert not session_dir.exists()


def test_agy_parser_validates_conversation_identity() -> None:
    parsed = _parse_agy_events_full(
        _agy_output("conversation-1", "ok"),
        require_conversation_id=True,
    )
    assert parsed.conversation_id == "conversation-1"

    with patch("trans_novel.llm.providers.agy._log_agy_error"):
        with pytest.raises(RuntimeError, match="did not return a conversation ID"):
            _parse_agy_events_full(
                _agy_output(None, "ok", include_init=False),
                require_conversation_id=True,
            )
        with pytest.raises(RuntimeError, match="conflicting conversation IDs"):
            _parse_agy_events_full(
                _agy_output("conversation-1", "ok", result_conversation_id="conversation-2"),
                require_conversation_id=True,
            )
        with pytest.raises(RuntimeError, match="invalid init conversation_id"):
            _parse_agy_events_full(
                _agy_output("--not-an-id", "ok"),
                require_conversation_id=True,
            )
        with pytest.raises(RuntimeError, match="resumed a different conversation ID"):
            _parse_agy_events_full(
                _agy_output("conversation-2", "ok"),
                require_conversation_id=True,
                expected_conversation_id="conversation-1",
            )


def test_agy_start_and_continue_use_explicit_conversation_id() -> None:
    client = AgyClient(
        LLMConfig(
            cli_path="agy-test",
            tiers={
                "strong": {
                    "model": "agy-model",
                    "options": {"reasoning_effort": "low"},
                }
            },
            max_retries=0,
        )
    )
    calls: list[dict[str, object]] = []

    def fake_run(
        argv,
        *,
        input_text=None,
        timeout=None,
        provider=None,
        cwd=None,
    ):
        del timeout, provider
        calls.append({"argv": list(argv), "input": input_text, "cwd": cwd})
        text = '{"translations":["译文"]}' if len(calls) == 1 else '{"polished":["润色"]}'
        return SimpleNamespace(
            returncode=0,
            stdout=_agy_output("conversation-1", text),
            stderr="",
        )

    messages = [
        {"role": "system", "content": "fixed system"},
        {"role": "user", "content": "first user"},
    ]
    with patch("trans_novel.llm.providers.agy.run_cli_process", side_effect=fake_run):
        started = client.start_conversation(messages, json_mode=True)
        assert started.handle is not None
        continued = client.continue_conversation(
            started.handle,
            "only the new user turn",
            json_mode=True,
        )

    start_argv = calls[0]["argv"]
    continue_argv = calls[1]["argv"]
    assert "--conversation" not in start_argv
    assert continue_argv[continue_argv.index("--conversation") + 1] == "conversation-1"
    assert "--continue" not in continue_argv
    first_payload = json.loads(calls[0]["input"])
    continue_payload = json.loads(calls[1]["input"])
    assert "fixed system" in first_payload["message"]["content"]
    assert continue_payload["message"]["content"] == "only the new user turn"
    assert calls[0]["cwd"] == calls[1]["cwd"]
    assert start_argv[start_argv.index("--model") + 1] == "agy-model"
    assert continue_argv[continue_argv.index("--model") + 1] == "agy-model"
    assert start_argv[start_argv.index("--effort") + 1] == "low"
    assert continue_argv[continue_argv.index("--effort") + 1] == "low"
    assert continued.text == '{"polished":["润色"]}'
    assert started.handle.state == "closed"


def test_agy_continuation_is_not_retried() -> None:
    client = AgyClient(
        LLMConfig(
            cli_path="agy-test",
            tiers={"strong": {"model": "agy-model"}},
            max_retries=3,
        )
    )
    calls = 0

    def fake_run(argv, **kwargs):
        nonlocal calls
        del argv, kwargs
        calls += 1
        if calls == 1:
            return SimpleNamespace(
                returncode=0,
                stdout=_agy_output("conversation-1", "first"),
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="resume failed")

    with (
        patch("trans_novel.llm.providers.agy.run_cli_process", side_effect=fake_run),
        patch("trans_novel.llm.providers.agy._log_agy_error"),
    ):
        started = client.start_conversation([{"role": "user", "content": "x"}])
        assert started.handle is not None
        with pytest.raises(RuntimeError, match="resume failed"):
            client.continue_conversation(started.handle, "new")

    assert calls == 2
    assert started.handle.state == "closed"


class _FallbackClient(LLMClient):
    def __init__(self, *, standalone_success: bool = True) -> None:
        super().__init__()
        self.provider_name = "fallback"
        self.standalone_success = standalone_success
        self.complete_calls: list[Messages] = []

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> str:
        del tier, json_mode, max_tokens, stage
        self.complete_calls.append(messages)
        if len(self.complete_calls) == 1:
            return '{"unexpected":[]}'
        if self.standalone_success:
            return '{"polished":["standalone"]}'
        raise RuntimeError("standalone failed")

    def continue_conversation(
        self,
        handle: NativeConversation,
        user_message: str,
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> ConversationCompletion:
        del user_message, json_mode, max_tokens, stage
        handle.claim(self)
        self.close_conversation(handle)
        raise NativeConversationUnsupportedError("native unavailable")


def test_polisher_fallback_order_native_transcript_then_standalone() -> None:
    config = Config.from_dict({"source_lang": "ja", "target_lang": "zh"})
    client = _FallbackClient()
    polisher = Polisher(client, config)
    handle = _native_handle(client)
    turn: Messages = [
        {"role": "system", "content": "translator system"},
        {"role": "user", "content": "source"},
        {"role": "assistant", "content": '{"translations":["译文"]}'},
    ]

    continued = polisher.polish_continue(turn, n=1, native_handle=handle)
    assert continued is None
    polished = polisher.polish(["译文"])

    assert polished == ["standalone"]
    assert handle.state == "closed"
    assert len(client.complete_calls) == 2
    assert len(client.complete_calls[0]) == 4
    assert len(client.complete_calls[1]) == 2


def test_standalone_polish_failure_preserves_original_translation() -> None:
    config = Config.from_dict({"source_lang": "ja", "target_lang": "zh"})
    client = _FallbackClient(standalone_success=False)
    # Skip the transcript slot so the first standalone call exercises its exception fallback.
    client.complete_calls.append([])
    polisher = Polisher(client, config)

    assert polisher.polish(["原译"]) == ["原译"]


class _ConcurrentTranslationClient(LLMClient):
    def __init__(self) -> None:
        super().__init__()
        self.provider_name = "concurrent"
        self._counter = 0
        self._lock = threading.Lock()

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> str:
        del messages, tier, json_mode, max_tokens, stage
        return "unused"

    def start_json_conversation(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> JsonConversationCompletion:
        del max_tokens, stage
        with self._lock:
            self._counter += 1
            native_id = f"batch-{self._counter}"
        user = messages[-1]["content"]
        marker = "甲" if "猫甲" in user else "乙"
        data = {"translations": [f"译{marker}"]}
        text = json.dumps(data, ensure_ascii=False)
        handle = NativeConversation(
            provider=self.provider_name,
            config_index=self.config_index,
            tier=tier,
            model="model",
            native_id=native_id,
            cwd=os.path.abspath(os.getcwd()),
            prefix_hash=conversation_fingerprint(messages),
            _owner=self,
        )
        return JsonConversationCompletion(data, text, handle)


def test_translator_returns_batch_local_context_under_concurrency() -> None:
    config = Config.from_dict({"source_lang": "ja", "target_lang": "zh"})
    client = _ConcurrentTranslationClient()
    translator = Translator(client, config)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                translator.translate_batch_result,
                [source, "123"],
                capture_conversation=True,
            )
            for source in ("猫甲", "猫乙")
        ]
        results = [future.result() for future in futures]

    assert [result.targets for result in results] == [["译甲", "123"], ["译乙", "123"]]
    assert all(result.continuation is not None for result in results)
    continuations = [result.continuation for result in results if result.continuation is not None]
    assert [continuation.translated_indices for continuation in continuations] == [[0], [0]]
    handles = [continuation.native_handle for continuation in continuations]
    assert all(handle is not None for handle in handles)
    assert len({handle.native_id for handle in handles if handle is not None}) == 2
    assert translator.last_batch_turn is None
    assert translator.last_batch_indices is None
    for handle in handles:
        if handle is not None:
            client.close_conversation(handle)


def test_pi_502_native_continuation_fails_over_then_replays_transcript() -> None:
    config = Config.from_dict(
        {
            "source_lang": "ja",
            "target_lang": "zh",
            "llm_priority": "01",
            "llm_list": [
                {
                    "provider": "pi",
                    "cli_path": "pi-test",
                    "max_retries": 3,
                    "tiers": {"strong": {"model": "pi-model"}},
                },
                {"provider": "fake"},
            ],
        }
    )
    priority = PriorityLLMClient(config)
    fallback_messages: list[Messages] = []
    priority._clients[1].handler = lambda messages, tier, json_mode: (
        fallback_messages.append(messages) or '{"polished":["回退润色"]}'
    )
    pi_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        argv = list(argv)
        pi_calls.append(argv)
        if "--session-id" in argv:
            _write_pi_session(argv, cwd=kwargs.get("cwd"))
            return SimpleNamespace(
                returncode=0,
                stdout=_pi_output('{"translations":["译文"]}'),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=_pi_error_output("API Error: 502 status code (no body)"),
            stderr="",
        )

    messages: Messages = [
        {"role": "system", "content": "translator system"},
        {"role": "user", "content": "source"},
    ]
    with (
        patch("trans_novel.llm.providers.pi.run_cli_process", side_effect=fake_run),
        patch("trans_novel.llm.providers.pi._log_pi_error"),
        patch("trans_novel.llm.priority._dispatch_audit_notifications"),
    ):
        first = priority.start_json_conversation(messages)
        assert first.handle is not None
        session_dir = Path(pi_calls[0][pi_calls[0].index("--session-dir") + 1])
        turn = [*messages, {"role": "assistant", "content": first.text}]
        result = Polisher(priority, config).polish_continue(
            turn,
            n=1,
            native_handle=first.handle,
        )

    assert result == ["回退润色"]
    assert priority.current_config_index == 1
    assert len(pi_calls) == 2
    assert first.handle.state == "closed"
    assert not session_dir.exists()
    assert len(fallback_messages) == 1
    assert [message["role"] for message in fallback_messages[0]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]


def test_pi_502_exception_object_fails_over_without_retry() -> None:
    class Status502Error(RuntimeError):
        status_code = 502

    config = Config.from_dict(
        {
            "llm_priority": "01",
            "llm_list": [
                {
                    "provider": "pi",
                    "cli_path": "pi-test",
                    "max_retries": 3,
                    "tiers": {"strong": {"model": "pi-model"}},
                },
                {"provider": "fake"},
            ],
        }
    )
    priority = PriorityLLMClient(config)
    priority._clients[1].handler = lambda messages, tier, json_mode: "fallback"

    with (
        patch(
            "trans_novel.llm.providers.pi.run_cli_process",
            side_effect=Status502Error("upstream returned 502"),
        ) as mock_run,
        patch("trans_novel.llm.providers.pi._log_pi_error"),
        patch("trans_novel.llm.priority._dispatch_audit_notifications"),
    ):
        result = priority.complete([{"role": "user", "content": "source"}])

    assert result == "fallback"
    assert mock_run.call_count == 1
    assert priority.current_config_index == 1
