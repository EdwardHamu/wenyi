"""文档加载分发 + 翻译批次切分。

- load_document：按扩展名分发到 EPUB / FB2 / TXT / Markdown / HTML / PDF
  读取器；可选把超长 Segment 按句拆分。
- batch_segments：把一章的 Segment 按共享 token 预算（tiktoken cl100k_base）打包成批次，
  一个批次整体发给翻译模型；模型须返回等长译文数组以做对齐校验。
- split_long_segments：单个 Segment 超过 max_tokens 时按句切成多段（续段标 cont=True），
  回填时由 writer 把续段并回同一段落/同一 EPUB 元素，保持结构一一对应。
"""

from __future__ import annotations

import os
import re
from copy import deepcopy

from .epub_reader import read_epub
from .fb2_reader import read_fb2
from .html_reader import read_html
from .models import KIND_TEXT, Chapter, Document, Segment
from .pdf_reader import read_pdf
from .text_reader import read_text
from .tokens import count_tokens

# 常见句末标点，用于超长段的按句拆分
_SENT_SPLIT = re.compile(r"(?<=[。．.!！？!?…\n])")


def _prefix_within_tokens(text: str, max_tokens: int) -> int:
    """在 count_tokens(text[:end]) <= max_tokens 的最大 end 下标中，优先在空白处切分。"""
    if not text:
        return 0
    if max_tokens <= 0:
        return 0
    if count_tokens(text) <= max_tokens:
        return len(text)

    low, high = 1, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if count_tokens(text[:mid]) <= max_tokens:
            low = mid
        else:
            high = mid - 1
    end = low
    # 优先在安全前缀内的空白处切分，避免拆散英文单词
    for sep in (" ", "\t", "\n"):
        cut = text.rfind(sep, 0, end + 1)
        if cut > 0:
            return cut
    return end


def _split_oversized_sentence(text: str, max_tokens: int) -> list[str]:
    """兜底拆分单个超长句：优先在 token 安全前缀内找空白，找不到才硬切。"""
    chunks: list[str] = []
    rest = text
    while rest and count_tokens(rest) > max_tokens:
        cut = _prefix_within_tokens(rest, max_tokens)
        if cut <= 0:
            # 单个字符/码点已超过预算时，至少推进一个字符
            cut = max(1, min(len(rest), 1))
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks


def _split_text(text: str, max_tokens: int) -> list[str]:
    """把超长文本按句末标点贪心打包；单句超过 token 预算时才按空白或硬切兜底。"""
    chunks: list[str] = []
    cur = ""
    for p in _SENT_SPLIT.split(text):
        if not p:
            continue
        part_tokens = count_tokens(p)
        if part_tokens > max_tokens:  # 单句本身超长 → 兜底拆
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.extend(_split_oversized_sentence(p, max_tokens))
            continue
        if cur and count_tokens(cur + p) > max_tokens:
            chunks.append(cur)
            cur = ""
        cur += p
    if cur:
        chunks.append(cur)
    return chunks or [text]


def split_long_segments(chapters: list[Chapter], max_tokens: int) -> None:
    """就地把各章里超过 max_tokens 的 Segment 拆成多段；续段 cont=True、不带 anchor。"""
    if not max_tokens or max_tokens <= 0:
        return
    for ch in chapters:
        new_segs: list[Segment] = []
        idx = 0
        for s in ch.segments:
            if count_tokens(s.source) <= max_tokens:
                s.index = idx
                new_segs.append(s)
                idx += 1
                continue
            for k, piece in enumerate(_split_text(s.source, max_tokens)):
                if k == 0:
                    new_segs.append(
                        Segment(
                            index=idx,
                            source=piece,
                            kind=s.kind,
                            anchor=s.anchor,
                            resource_href=s.resource_href,
                            cont=False,
                            meta=deepcopy(s.meta),
                        )
                    )
                else:  # 续段：并回首段，无独立 anchor
                    new_segs.append(
                        Segment(
                            index=idx,
                            source=piece,
                            kind=KIND_TEXT,
                            anchor=None,
                            resource_href=s.resource_href,
                            cont=True,
                        )
                    )
                idx += 1
        ch.segments = new_segs


def load_document(
    path: str,
    source_lang: str,
    target_lang: str,
    split_segments: int = 0,
    *,
    cache_dir: str | None = None,
    source_hash: str | None = None,
    pdf_backend: str = "mineru",
    babeldoc_bridge_url: str = "http://127.0.0.1:8765",
    babeldoc_pages: str | None = None,
    babeldoc_timeout: float = 600.0,
) -> Document:
    """按文件扩展名读取文档，并按需拆分超过上限的翻译段。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".epub":
        doc = read_epub(path, source_lang, target_lang)
    elif ext in (".md", ".markdown", ".txt", ".text"):
        doc = read_text(path, source_lang, target_lang)
    elif ext == ".fb2":
        doc = read_fb2(path, source_lang, target_lang)
    elif ext in (".html", ".htm", ".xhtml"):
        doc = read_html(path, source_lang, target_lang)
    elif ext == ".pdf":
        if cache_dir is None:
            raise ValueError("PDF 读取需要指定运行状态缓存目录")
        if pdf_backend == "babeldoc":
            from .pdf_babeldoc import read_pdf_babeldoc

            doc = read_pdf_babeldoc(
                path,
                source_lang,
                target_lang,
                bridge_url=babeldoc_bridge_url,
                pages=babeldoc_pages,
                cache_dir=cache_dir,
                timeout=babeldoc_timeout,
            )
        else:
            doc = read_pdf(
                path,
                source_lang,
                target_lang,
                cache_dir=cache_dir,
                source_hash=source_hash,
            )
    elif ext == ".docx":
        from .docx_reader import read_docx

        doc = read_docx(path, source_lang, target_lang)
    else:
        raise ValueError(
            f"不支持的格式：{ext}（支持 .epub / .txt / .md / .fb2 / .html / .xhtml / .pdf / .docx）"
        )

    # BabelDOC 段 id 与排版绑定，禁止再按 token 拆碎。
    if split_segments and split_segments > 0 and not (doc.meta or {}).get("babeldoc"):
        split_long_segments(doc.chapters, split_segments)
    return doc


def batch_segments(segments: list[Segment], max_tokens: int) -> list[list[Segment]]:
    """把 Segment 列表按共享 token 预算（tiktoken cl100k_base）分批。"""
    batches: list[list[Segment]] = []
    cur: list[Segment] = []
    cur_tokens = 0
    for s in segments:
        slen = count_tokens(s.source)
        if cur and cur_tokens + slen > max_tokens:
            batches.append(cur)
            cur, cur_tokens = [], 0
        cur.append(s)
        cur_tokens += slen
    if cur:
        batches.append(cur)
    return batches


def chapter_batches(chapter: Chapter, max_tokens: int) -> list[list[Segment]]:
    """对一章的可翻译 Segment 按 token 预算分批。"""
    return batch_segments(chapter.text_segments, max_tokens)
