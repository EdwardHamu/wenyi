"""翻译 Agent（强档）。

核心保证：句段对齐——输入 N 段，输出必须是 N 段，一一对应。
策略：
1. 整批翻译并要求等长 JSON 数组；
2. 段数不符则重试（最多 align_retry_limit 次）；
3. 仍不符则逐段单独翻译兜底，从结构上保证 1:1，杜绝整段漏译。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..glossary.store import GlossaryTerm
from ..llm.base import LLMClient, NativeConversation
from ..llm.json_parser import JsonParseError
from . import langprofile, prompts
from .base import Agent, Messages


class AlignmentError(Exception):
    """模型译文未满足 Translator 的一一对应输出协议。"""

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


@dataclass(slots=True)
class BatchContinuation:
    """成功整批翻译后立即润色所需的批次局部上下文。"""

    transcript: Messages
    translated_indices: list[int]
    native_handle: NativeConversation | None = None


@dataclass(slots=True)
class TranslationBatchResult:
    targets: list[str]
    continuation: BatchContinuation | None = None


class Translator(Agent):
    def __init__(self, client: LLMClient, config: Config):
        super().__init__(client, config)
        self.last_batch_turn: Messages | None = None
        self.last_batch_indices: list[int] | None = None

    @staticmethod
    def _needs_translation(source: str) -> bool:
        """仅把含语言文字的非空段落发送给模型。

        PDF 表格经常把 ``-``、纯数字或其它占位符解析为独立段落。模型可能
        把这些内容返回为空字符串，进而触发对齐失败；这类段落原样保留即可。
        ``str.isalpha`` 覆盖拉丁、中文、日文、韩文等 Unicode 字母。
        """
        stripped = source.strip()
        return bool(stripped) and any(character.isalpha() for character in stripped)

    @staticmethod
    def _validate_annotation_contexts(
        sources: list[str],
        annotation_contexts: list[list[dict[str, str]]] | None,
    ) -> list[list[dict[str, str]]]:
        """校验逐段注释资料，并裁剪为提示词实际使用的稳定字段。"""
        if annotation_contexts is None:
            return [[] for _ in sources]
        if not isinstance(annotation_contexts, list) or len(annotation_contexts) != len(sources):
            actual = len(annotation_contexts) if isinstance(annotation_contexts, list) else "非列表"
            raise ValueError(f"注释上下文数量不匹配：期望 {len(sources)} 组，实际 {actual} 组")

        normalized: list[list[dict[str, str]]] = []
        for segment_index, items in enumerate(annotation_contexts):
            if not isinstance(items, list):
                raise ValueError(f"第 {segment_index} 段的注释上下文必须是列表")
            segment_items: list[dict[str, str]] = []
            for item_index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValueError(f"第 {segment_index} 段第 {item_index} 条注释上下文必须是对象")
                target_key = item.get("target_key")
                source = item.get("source")
                if not isinstance(target_key, str) or not target_key.strip():
                    raise ValueError(
                        f"第 {segment_index} 段第 {item_index} 条注释上下文缺少有效 target_key"
                    )
                if not isinstance(source, str):
                    raise ValueError(
                        f"第 {segment_index} 段第 {item_index} 条注释上下文缺少字符串 source"
                    )
                segment_items.append({"target_key": target_key, "source": source})
            normalized.append(segment_items)
        return normalized

    def _call_batch(
        self,
        sources: list[str],
        glossary_terms: list[GlossaryTerm],
        style: str,
        context: str,
        book_synopsis: str = "",
        chapter_digest: str = "",
        annotation_contexts: list[list[dict[str, str]]] | None = None,
        next_source: str = "",
        capture_conversation: bool = False,
    ) -> tuple[list[str], Messages, NativeConversation | None]:
        """调用一次批量翻译，并严格校验输出类型、数量和非空性。"""
        n = len(sources)
        system = prompts.render(
            "translator_system",
            src=self.src,
            tgt=self.tgt,
            lang_guidance=langprofile.translate_guidance(self.src, self.config.honorific_strategy),
        )
        user = prompts.render(
            "translator_user",
            src=self.src,
            tgt=self.tgt,
            style=style or "（无）",
            book_synopsis=book_synopsis or "（无）",
            glossary=prompts.render_glossary(glossary_terms),
            annotation_contexts=prompts.render_annotation_contexts(
                annotation_contexts or [[] for _ in sources]
            ),
            chapter_digest=chapter_digest or "（无）",
            context=context or "（无）",
            n=n,
            n_minus_1=n - 1,
            numbered_source=prompts.numbered(sources),
            next_source=prompts.render_source_reference(next_source),
        )
        messages: Messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        native_handle: NativeConversation | None = None
        try:
            # Provider 瞬时错误只由传输层重试；这里只把成功响应中的 JSON
            # 协议错误归入对齐恢复，避免 401/403/5xx 被业务层再次放大。
            try:
                if capture_conversation:
                    completion = self._start_json_conversation_turn(
                        messages,
                        tier="strong",
                    )
                    data = completion.data
                    raw = completion.text
                    native_handle = completion.handle
                else:
                    data, raw = self._complete_json_turn(messages, tier="strong")
            except JsonParseError as error:
                raise AlignmentError(
                    "模型返回的译文 JSON 无法解析",
                    reason="invalid_json",
                ) from error
            items = data.get("translations") if isinstance(data, dict) else data
            if not isinstance(items, list):
                raise AlignmentError("模型未返回译文数组", reason="translations_not_list")
            if len(items) != n:
                raise AlignmentError(
                    f"译文数量不匹配：期望 {n} 段，实际 {len(items)} 段",
                    reason="translations_count_mismatch",
                )
            if any(not isinstance(item, str) or not item.strip() for item in items):
                raise AlignmentError(
                    "模型返回了空译文或非字符串译文",
                    reason="translations_empty_or_non_string",
                )
            turn: Messages = [
                *messages,
                {"role": "assistant", "content": raw},
            ]
            return items, turn, native_handle
        except BaseException:
            if native_handle is not None:
                try:
                    self.client.close_conversation(native_handle)
                except BaseException:
                    pass
            raise

    def _log_rejected_response(
        self,
        error: AlignmentError,
        *,
        attempt: int,
        attempt_limit: int,
        source_indices: list[int],
        mode: str,
        next_action: str,
    ) -> None:
        """记录导致 Translator 再请求的原始响应和协议拒绝原因。"""
        response = self.client.last_json_response()
        self.client.emit_event(
            "translator_response_rejected",
            stage=type(self).__name__,
            reason=error.reason,
            detail=str(error),
            mode=mode,
            attempt=attempt,
            attempt_limit=attempt_limit,
            source_indices=source_indices,
            source_count=len(source_indices),
            request_id=self.client.last_json_request_id(),
            response=response,
            response_chars=len(response) if isinstance(response, str) else None,
            next_action=next_action,
        )

    def _translate_one(
        self,
        source: str,
        glossary_terms: list[GlossaryTerm],
        style: str,
        context: str,
        book_synopsis: str,
        chapter_digest: str,
        annotation_context: list[dict[str, str]],
        next_source: str = "",
    ) -> str:
        """借用批量协议翻译单段，作为批量对齐失败后的最终兜底。"""
        out, _turn, _handle = self._call_batch(
            [source],
            glossary_terms,
            style,
            context,
            book_synopsis,
            chapter_digest,
            [annotation_context],
            next_source=next_source,
        )
        return out[0]

    def translate_batch_result(
        self,
        sources: list[str],
        *,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        context: str = "",
        book_synopsis: str = "",
        chapter_digest: str = "",
        annotation_contexts: list[list[dict[str, str]]] | None = None,
        next_source: str = "",
        capture_conversation: bool = False,
    ) -> TranslationBatchResult:
        """翻译一批源段，并显式返回仅属于该批次的润色上下文。"""
        glossary_terms = glossary_terms or []
        n = len(sources)
        annotation_contexts = self._validate_annotation_contexts(sources, annotation_contexts)
        if n == 0:
            return TranslationBatchResult([])

        translated_indices = [
            index for index, source in enumerate(sources) if self._needs_translation(source)
        ]
        if not translated_indices:
            return TranslationBatchResult(list(sources))
        translated_sources = [sources[index] for index in translated_indices]
        translated_annotation_contexts = [
            annotation_contexts[index] for index in translated_indices
        ]

        # 批内末尾被过滤的纯数字/符号段优先作为直接后文。
        following_index = translated_indices[-1] + 1
        batch_next_source = sources[following_index] if following_index < n else next_source

        attempts = self.config.pipeline.align_retry_limit + 1
        for attempt in range(1, attempts + 1):
            try:
                translated, turn, native_handle = self._call_batch(
                    translated_sources,
                    glossary_terms,
                    style,
                    context,
                    book_synopsis,
                    chapter_digest,
                    translated_annotation_contexts,
                    next_source=batch_next_source,
                    capture_conversation=capture_conversation,
                )
                targets = list(sources)
                for index, target in zip(translated_indices, translated):
                    targets[index] = target
                return TranslationBatchResult(
                    targets=targets,
                    continuation=BatchContinuation(
                        transcript=turn,
                        translated_indices=list(translated_indices),
                        native_handle=native_handle,
                    ),
                )
            except AlignmentError as error:
                self._log_rejected_response(
                    error,
                    attempt=attempt,
                    attempt_limit=attempts,
                    source_indices=translated_indices,
                    mode="batch",
                    next_action="retry_batch" if attempt < attempts else "fallback_per_segment",
                )

        # 兜底逐段翻译不会创建原生会话，避免生成无法安全合并的多个 handle。
        targets = list(sources)
        for index, source, annotation_context in zip(
            translated_indices,
            translated_sources,
            translated_annotation_contexts,
        ):
            seg_next_source = sources[index + 1] if index + 1 < n else next_source
            try:
                targets[index] = self._translate_one(
                    source,
                    glossary_terms,
                    style,
                    context,
                    book_synopsis,
                    chapter_digest,
                    annotation_context,
                    next_source=seg_next_source,
                )
            except AlignmentError as error:
                self._log_rejected_response(
                    error,
                    attempt=1,
                    attempt_limit=1,
                    source_indices=[index],
                    mode="single_fallback",
                    next_action="abort_batch",
                )
                raise AlignmentError(
                    f"逐段兜底翻译在第 {index} 段失败",
                    reason=error.reason,
                ) from error
            except Exception as error:
                raise AlignmentError(
                    f"逐段兜底翻译在第 {index} 段失败",
                    reason="single_fallback_error",
                ) from error
        return TranslationBatchResult(targets)

    def translate_batch(
        self,
        sources: list[str],
        *,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        context: str = "",
        book_synopsis: str = "",
        chapter_digest: str = "",
        annotation_contexts: list[list[dict[str, str]]] | None = None,
        next_source: str = "",
    ) -> list[str]:
        """兼容旧调用：返回译文，并保留 transcript 诊断属性。"""
        self.last_batch_turn = None
        self.last_batch_indices = None
        result = self.translate_batch_result(
            sources,
            glossary_terms=glossary_terms,
            style=style,
            context=context,
            book_synopsis=book_synopsis,
            chapter_digest=chapter_digest,
            annotation_contexts=annotation_contexts,
            next_source=next_source,
        )
        if result.continuation is not None:
            self.last_batch_turn = result.continuation.transcript
            self.last_batch_indices = result.continuation.translated_indices
        return result.targets
