"""模型 JSON 输出的宽松解析。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from json_repair import repair_json


def _skip_json_whitespace(text: str, start: int) -> int:
    while start < len(text) and text[start] in " \t\r\n":
        start += 1
    return start


def _json_string_end(text: str, start: int) -> int:
    """Return the closing quote for a JSON string, or ``-1`` if unfinished."""
    if start >= len(text) or text[start] != '"':
        return -1
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text[i] == '"':
            return i
        else:
            i += 1
    return -1


def _has_array_close_before_object_close(text: str, start: int) -> bool:
    """Check for a structural ``]`` before the next structural ``}``."""
    i = start
    while i < len(text):
        if text[i] == '"':
            end = _json_string_end(text, i)
            if end == -1:
                return False
            i = end + 1
            continue
        if text[i] == "]":
            return True
        if text[i] == "}":
            return False
        i += 1
    return False


def _repair_premature_array_close(text: str) -> str:
    """Repair an object-valued array closed before later string items.

    A recurring malformed response is equivalent to::

        {"translations":["a"],"b","c"]}

    The first ``]`` is premature; the later strings are intended to remain in
    the array. Repair only this recognizable shape. In particular, a normal
    object property after an array (``...,"note":"value"``) is left alone.
    """
    stack: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == '"':
            end = _json_string_end(text, i)
            if end == -1:
                return text
            i = end + 1
            continue
        if char in "[{":
            stack.append(char)
            i += 1
            continue
        if char == "]":
            if stack and stack[-1] == "[":
                if len(stack) >= 2 and stack[-2] == "{":
                    comma = _skip_json_whitespace(text, i + 1)
                    if comma < len(text) and text[comma] == ",":
                        item_start = _skip_json_whitespace(text, comma + 1)
                        if item_start < len(text) and text[item_start] == '"':
                            item_end = _json_string_end(text, item_start)
                            if item_end != -1:
                                next_token = _skip_json_whitespace(text, item_end + 1)
                                if (
                                    next_token >= len(text) or text[next_token] != ":"
                                ) and _has_array_close_before_object_close(text, item_end + 1):
                                    return text[:i] + text[i + 1 :]
                stack.pop()
            i += 1
            continue
        if char == "}":
            if stack and stack[-1] == "{":
                stack.pop()
        i += 1
    return text


def _repair_unescaped_quotes(text: str) -> str:
    """转义 JSON 字符串值内部未转义的 ASCII 双引号。

    部分模型（尤其无原生 JSON 模式的 provider）会在译文里原样输出英文引号。
    启发式：字符串内的 `"` 后面（跳过空白）若不是 `,:]}`，视为内容引号转义之。
    中文译文以全角标点为主，误判面极小；仅作为常规解析失败后的兜底。
    """
    out: list[str] = []
    in_str = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if not in_str:
            if c == '"':
                in_str = True
            out.append(c)
        elif c == "\\" and i + 1 < n:
            out.append(text[i : i + 2])
            i += 1
        elif c == '"':
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n or text[j] in ",:]}":
                in_str = False
                out.append(c)
            else:
                out.append('\\"')
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _parse_json_targeted_repairs(text: str) -> Any:
    """Parse the malformed shapes that need ordering beyond json-repair."""
    repaired = _repair_unescaped_quotes(text)
    structurally_repaired = _repair_premature_array_close(repaired)
    if structurally_repaired != repaired:
        starts = [
            i
            for i in (
                structurally_repaired.find("{"),
                structurally_repaired.find("["),
            )
            if i != -1
        ]
        if starts:
            value, _ = json.JSONDecoder().raw_decode(structurally_repaired[min(starts) :])
            return value

    if repaired != text:
        starts = [i for i in (repaired.find("{"), repaired.find("[")) if i != -1]
        if starts:
            value, _ = json.JSONDecoder().raw_decode(repaired[min(starts) :])
            return value
    raise ValueError("没有匹配的定向 JSON 修复")


class JsonParseError(ValueError):
    """模型回复在本地修复后仍不是可用 JSON。"""


@dataclass(frozen=True)
class JsonParseResult:
    """模型 JSON 的解析结果，以及是否经过语法修复。"""

    value: Any
    repaired: bool


def _parse_json_legacy(text: str) -> Any:
    """保留既有 provider 兼容行为的解析兜底。"""
    try:
        return json.loads(text)
    except Exception:
        pass

    # 去掉 markdown 代码围栏
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        inner = fenced.group(1).strip()
        try:
            return json.loads(inner)
        except Exception:
            text = inner

    # 截取首个 JSON 数组或对象
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                continue

    # 从首个 {/[ 起解析第一个完整 JSON 值，忽略尾部多余字符。
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if starts:
        try:
            value, _ = json.JSONDecoder().raw_decode(text[min(starts) :])
            return value
        except Exception:
            pass

    # 处理模型把对象属性数组提前关闭、随后继续输出字符串数组项的情况。
    # 先修复结构，再用 raw_decode 保留外层对象并忽略可能存在的尾部文本。
    structurally_repaired = _repair_premature_array_close(text)
    if structurally_repaired != text:
        starts = [
            i
            for i in (
                structurally_repaired.find("{"),
                structurally_repaired.find("["),
            )
            if i != -1
        ]
        if starts:
            try:
                value, _ = json.JSONDecoder().raw_decode(structurally_repaired[min(starts) :])
                return value
            except Exception:
                pass

    # 最后兜底：修复字符串内未转义的引号，再从完整文本解析首个 JSON 值。
    # 必须先做这一步：若同时有未转义引号和尾部多余字符，直接截取内部数组
    # 会丢掉外层对象（如 {"translations": [...]}）。
    repaired = _repair_unescaped_quotes(text)
    repaired_structurally = _repair_premature_array_close(repaired)
    if repaired_structurally != repaired:
        starts = [
            i
            for i in (
                repaired_structurally.find("{"),
                repaired_structurally.find("["),
            )
            if i != -1
        ]
        if starts:
            try:
                value, _ = json.JSONDecoder().raw_decode(repaired_structurally[min(starts) :])
                return value
            except Exception:
                pass
    starts = [i for i in (repaired.find("{"), repaired.find("[")) if i != -1]
    if starts:
        try:
            value, _ = json.JSONDecoder().raw_decode(repaired[min(starts) :])
            return value
        except Exception:
            pass

    # 修复后仍无法解析时，依次尝试完整文本和对象/数组片段。
    for candidate in (
        text,
        *(
            text[s : e + 1]
            for o, c in (("[", "]"), ("{", "}"))
            for s, e in [(text.find(o), text.rfind(c))]
            if s != -1 and e > s
        ),
    ):
        try:
            return json.loads(_repair_unescaped_quotes(candidate))
        except Exception:
            continue
    raise ValueError(f"无法解析为 JSON：{text[:200]!r}")


def parse_json_result(text: str) -> JsonParseResult:
    """解析模型 JSON，并准确标记结果是否经过语法修复。

    先保留已有的 provider 兼容修复，再由 ``json-repair`` 统一兜底；显式
    执行一次 ``json.loads`` 是为了生成准确的 ``repaired`` 标志。
    """
    raw = (text or "").strip()
    try:
        return JsonParseResult(json.loads(raw), repaired=False)
    except (json.JSONDecodeError, TypeError):
        pass

    try:
        return JsonParseResult(_parse_json_targeted_repairs(raw), repaired=True)
    except Exception:
        pass

    repair_error: Exception | None = None
    try:
        value = repair_json(
            raw,
            return_objects=True,
            skip_json_loads=True,
        )
    except Exception as error:
        repair_error = error
    else:
        # json-repair 对空文本和纯自然语言返回空串；它们不属于可恢复 JSON。
        if value != "":
            return JsonParseResult(value, repaired=True)

    try:
        return JsonParseResult(_parse_json_legacy(raw), repaired=True)
    except Exception as legacy_error:
        error = repair_error or legacy_error
        raise JsonParseError(f"无法解析为 JSON：{raw[:200]!r}") from error


def parse_json_loose(text: str) -> Any:
    """返回模型 JSON 的值；语法容错由兼容修复和 json-repair 共同实现。"""
    return parse_json_result(text).value
