"""润色 Agent（强档）。

在审校通过的直译稿上做中文文学性二次加工：不增删信息、保持段数不变。
对齐失败（段数不符）时保守地返回原译文，绝不因润色而引入漏译。
"""

from __future__ import annotations

from ..glossary.store import GlossaryTerm
from . import prompts
from .base import Agent, Messages


class Polisher(Agent):
    def polish(
        self,
        targets: list[str],
        *,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        next_source: str = "",
    ) -> list[str]:
        """润色等长译文列表；调用失败或数量不符时原样返回输入。"""
        if not targets:
            return []
        n = len(targets)
        system = prompts.render("polisher_system", src=self.src, tgt=self.tgt, n=n)
        user = prompts.render(
            "polisher_user",
            src=self.src,
            tgt=self.tgt,
            glossary=prompts.render_glossary(glossary_terms or []),
            style=style or "（无）",
            n=n,
            numbered_target=prompts.numbered(targets),
            next_source=prompts.render_source_reference(next_source),
        )
        items = self._ask_json(system, user, tier="strong", key="polished", default=None)
        if isinstance(items, list) and len(items) == n:
            return [str(x) for x in items]
        return list(targets)  # 失败/段数不符 → 保守保留原译

    def polish_continue(
        self,
        turn: Messages,
        *,
        n: int,
        next_source: str = "",
    ) -> list[str] | None:
        """在整批翻译成功后的消息 transcript 上追加润色 user 轮次。

        成功时返回润色后的字符串列表；失败、异常或段数不符时返回 None，
        供调用方回退到独立润色或保留原译。
        """
        if n <= 0 or len(turn) < 3:
            return None
        continue_user = prompts.render(
            "polisher_continue_user",
            src=self.src,
            tgt=self.tgt,
            n=n,
            next_source=prompts.render_source_reference(next_source),
        )
        messages: Messages = [
            *turn,
            {"role": "user", "content": continue_user},
        ]
        items = self._ask_json_messages(
            messages,
            tier="strong",
            key="polished",
            default=None,
        )
        if isinstance(items, list) and len(items) == n:
            return [str(x) for x in items]
        return None
