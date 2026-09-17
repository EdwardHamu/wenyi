"""配置加载。读取 config.yaml，提供带默认值的类型化访问（pydantic v2）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_DEFAULT_CONFIG_YAML = """\
# trans-novel 配置（多语言小说 → 中文）
# 修改后无需改代码；模型提供商、流水线和输出开关都在这里。

language:
  source: auto # auto 由模型识别来源语言；也可写死 ja / en / ko / ru / de 等语言代码
  target: zh # 译文语言

# ── LLM ──────────────────────────────────────────────────────────────────
# 多配置与优先级示例（多项配置故障时按优先级切换）：
# llm_priority: "01"  # 显式优先级，省略时按列表顺序动态生成（一项为 "0"，两项为 "01" 等）
# llm_list:
#   - provider: deepseek
#     base_url: https://api.deepseek.com
#     api_key_env: DEEPSEEK_API_KEY
#     timeout: 600
#     max_retries: 4
#     tiers:
#       strong:
#         model: deepseek-v4-pro
#       cheap:
#         model: deepseek-v4-flash
#       fast:
#         model: deepseek-v4-flash
#   - provider: agy
#     timeout: 600
#     max_retries: 4
#     tiers:
#       strong:
#         model: gemini-3.1-pro
#       cheap:
#         model: gemini-3-flash
#       fast:
#         model: gemini-3-flash

llm:

  # deepseek | openai | anthropic | codex | pi | codebuddy | agy | openrouter | orcarouter | openai-compatible | ollama | vllm | fake
  # agy 为本机 Agy CLI，不需要 base_url 或 api_key_env；
  # OrcaRouter 默认使用 https://api.orcarouter.ai/v1 和 ORCAROUTER_API_KEY；
  # 切换为 provider: orcarouter 时，请把 tiers.*.model 改为账户可用的模型 ID。
  provider: deepseek
  base_url: https://api.deepseek.com
  api_key_env: DEEPSEEK_API_KEY
  timeout: 600
  max_retries: 4
  tiers:
    # 各 tier 可单独指定 provider、base_url、api_key_env、cli_path、reasoning_style；未指定时继承 llm 顶层配置。
    strong:
      model: deepseek-v4-pro
      options:
        thinking: true
        reasoning_effort: high
    cheap:
      model: deepseek-v4-flash
      options:
        thinking: true
        reasoning_effort: high
    fast:
      model: deepseek-v4-flash
      options:
        thinking: true

# ── 切分 ─────────────────────────────────────────────────────────────────
segment:
  # 一个翻译批次（句群）的目标大小，按 token 计量（tiktoken cl100k_base）。
  max_tokens_per_batch: 1800
  # 单个段落超过该 token 预算时按句末标点再切成多段（续段回填时并回同段），避免超长段。
  max_tokens_per_segment: 1200

# ── 流水线开关（质量/成本平衡）───────────────────────────────────────────
pipeline:
  review: false # 默认关闭；开启后在全书翻译完成后自动执行最终审校
  align_retry_limit: 2
  polish: true # 润色（强档）：等于用 pro 把全书再翻一遍，最烧钱；默认开
  rolling_context_segments: 6 # 注入的前文译文尾段数
  book_understanding: true # 翻译前预扫源文，生成全书概览+逐章梗概注入翻译
  prescan_concurrency: 4 # 预扫逐章梗概的并发线程数（各章独立，1=串行）
  annotation_alignment: true # 逐段定位 EPUB 注释链接；关闭时仅译文侧退化为段末标记
  annotation_alignment_concurrency: 4 # 单段注释数>1时，按条并发定位的最大并发数
  review_concurrency: 4 # 最终审校连续分块的并发数（只读最终译文/术语快照，1=串行）
  review_output_retries: 2 # 单段审校输出畸形时额外重试次数（初次+2=最多 3 次）
  review_agent_loop: true # 初审发现候选后，使用强档按需取证并复核
  review_agent_tier: strong # 取证复核与全书冲突仲裁使用的模型档位
  review_agent_max_evidence_rounds: 2 # 最多两轮选择性取证，之后必须裁决
  review_conflict_arbitration: true # 全部审校块完成后仲裁互相矛盾的一致性建议
  review_fix_loop: true # 只在内存影子译文上暂改并盲复审，不写回正式正文
  review_fix_max_rounds: 2 # 最多生成两轮临时替换；完整 Review 轮数另受连续 clean 确认影响
  review_clean_confirmations: 2 # 连续两轮未发现问题才视为影子译文通过
  review_autofix: false # 可选将 Review 建议与终局 Agent 修订写回正式章节
  glossary_scope: chapter # chapter=本章相关词条；full=全量表
  # PDF 后端：mineru（默认）| babeldoc（外部 AGPL HTTP bridge，主仓不引入 babeldoc）
  pdf_backend: mineru
  babeldoc_bridge_url: http://127.0.0.1:8765
  # babeldoc_pages: "15"   # 可选；限制 bridge 处理页（1-based）
  babeldoc_timeout: 600

# ── 敬称策略（日语源文本时生效，其它语言通常不会用到）────────────────────
honorific:
  # keep_style: 体现语气（前辈/小X/X君…）; normalize: 按统一规则；drop: 省略
  strategy: keep_style

# ── 标点规范化（统一为简体中文大陆通用全角标点）────────────────────────────
punctuation:
  normalize: true

# ── 路径 ─────────────────────────────────────────────────────────────────
paths:
  state_dir: state # 运行状态、各章中间产物、术语库

# ── 双语输出 ───────────────────────────────────────────────────────────────
output:
  mono: true # 产出单语中文版（<书名>.zh.epub）
  bilingual: false # 产出原文与译文对照版（<书名>.zh-bi.epub）
  bilingual_order: target_first # target_first=译文在上；source_first=原文在上
  bilingual_preserve_source_style: false # true=原文继承原书样式；false=灰色淡化显示
  about_page: true # 在书末附加“关于此翻译”说明页
"""


ReasoningStyle = Literal["none", "deepseek", "openai", "openrouter"]


class TierConfig(BaseModel):
    """跨 provider 通用的档位覆盖；专属参数由 provider 解析 options。"""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    cli_path: str | None = None
    reasoning_style: ReasoningStyle | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class LLMConfig(BaseModel):
    provider: str = "deepseek"
    base_url: str | None = None
    api_key_env: str | None = None
    reasoning_style: ReasoningStyle = "none"
    timeout: int = 600
    max_retries: int = 4
    tiers: dict[str, TierConfig] = Field(default_factory=dict)
    cli_path: str | None = None  # anthropic/codex/pi/codebuddy/agy provider: 显式指定本机 CLI 路径


class SegmentConfig(BaseModel):
    """按 tiktoken ``cl100k_base`` token 预算切分和打包源文。"""

    model_config = ConfigDict(extra="forbid")

    max_tokens_per_batch: int = 1800
    max_tokens_per_segment: int = 1200


class PipelineConfig(BaseModel):
    review: bool = False
    align_retry_limit: int = 2  # 批次翻译段数不符时的整批重试次数，超限后逐段兜底
    polish: bool = True  # 默认开：润色=用强档把全书再翻一遍，可在配置中关闭以节省成本
    rolling_context_segments: int = 6
    # 翻译前预扫源文，生成全书概览+逐章梗概注入翻译 prompt；关掉可省去预扫成本。
    book_understanding: bool = True
    prescan_concurrency: int = 4  # 预扫逐章梗概的并发线程数（各章独立，1=串行）
    annotation_alignment: bool = True  # 每个含注释逻辑段定稿后串行定位链接
    # 单个逻辑段内注释数 >1 时，改为逐条并发请求（每请求只定位一条注释），
    # 避免单次响应要求模型同时摆对多条标记而整体回退到段末；此为并发上限。
    annotation_alignment_concurrency: int = 4
    review_concurrency: int = 4  # 最终审校连续分块并发数（结果按原块序合并，1=串行）
    review_output_retries: int = Field(
        default=2,
        ge=0,
        le=5,
    )  # 单段畸形输出的额外重试次数
    review_agent_loop: bool = True  # 初审发现候选后，启动有界取证 Agent Loop
    review_agent_tier: Literal["strong", "cheap", "fast"] = "strong"
    review_agent_max_evidence_rounds: int = Field(
        default=2,
        ge=0,
        le=2,
    )
    review_conflict_arbitration: bool = True  # 全部块完成后仲裁互相矛盾的一致性建议
    review_fix_loop: bool = True  # 仅在内存影子译文上生成临时替换并盲复审
    review_fix_max_rounds: int = Field(default=2, ge=0, le=4)
    review_clean_confirmations: int = Field(default=2, ge=1, le=2)
    review_autofix: bool = False  # Review 完成后由独立发布阶段写回正式译文
    glossary_scope: str = "chapter"  # chapter=只注入本章出现的词条（省 token）；full=全量表
    # PDF：mineru=现有 HTML 路径；babeldoc=外部 AGPL bridge（HTTP，主仓不 import babeldoc）
    pdf_backend: Literal["mineru", "babeldoc"] = "mineru"
    babeldoc_bridge_url: str = "http://127.0.0.1:8765"
    babeldoc_pages: str | None = None  # 如 "15" / "6-8"；None=全书
    babeldoc_timeout: float = 600.0


class OutputConfig(BaseModel):
    mono: bool = True  # 产出单语版
    bilingual: bool = False  # 产出双语版
    bilingual_order: str = (
        "target_first"  # target_first=译文在上原文在下(默认); source_first=原文在上
    )
    bilingual_preserve_source_style: bool = False
    about_page: bool = True  # 在书末附加项目说明页


class Config(BaseModel):
    source_lang: str = "auto"  # auto | ja | en | …（auto 时由模型检测）
    target_lang: str = "zh"
    llm_list: list[LLMConfig] = Field(default_factory=lambda: [LLMConfig()])
    llm_priority: str = "0"
    segment: SegmentConfig = Field(default_factory=SegmentConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    honorific_strategy: str = "keep_style"
    punctuation_normalize: bool = True  # 译文标点规范化为简体中文通用
    state_dir: str = "state"

    @model_validator(mode="before")
    @classmethod
    def _validate_llm_compatibility(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "llm" in data and "llm_list" in data:
                raise ValueError("不能同时配置 llm 与 llm_list")
            if "llm" in data and "llm_list" not in data:
                data = dict(data)
                llm_val = data.pop("llm")
                data["llm_list"] = [llm_val]
                if "llm_priority" not in data:
                    data["llm_priority"] = "0"
            if "llm_list" in data and "llm_priority" not in data and data.get("llm_list"):
                data = dict(data)
                data["llm_priority"] = "".join(str(i) for i in range(len(data["llm_list"])))
        return data

    @model_validator(mode="after")
    def _validate_priority_rules(self) -> Config:
        n = len(self.llm_list)
        if not (1 <= n <= 10):
            raise ValueError(f"llm_list 长度必须在 1 到 10 项之间，当前为 {n}")
        if not isinstance(self.llm_priority, str):
            raise ValueError("llm_priority 必须是带引号的字符串")
        if len(self.llm_priority) != n:
            raise ValueError(
                f"llm_priority 长度 ({len(self.llm_priority)}) 必须与 llm_list 项数 ({n}) 一致"
            )
        actual_chars = list(self.llm_priority)
        if len(actual_chars) != len(set(actual_chars)):
            raise ValueError(f"llm_priority 包含重复索引: {self.llm_priority}")
        expected_set = {str(i) for i in range(n)}
        for ch in actual_chars:
            if not ch.isdigit() or int(ch) >= n or int(ch) < 0:
                raise ValueError(f"llm_priority 索引越界: {ch}")
        if set(actual_chars) != expected_set:
            raise ValueError(
                f"llm_priority 存在缺失索引: 期望 {expected_set}，实际包含 {set(actual_chars)}"
            )
        return self

    @property
    def llm(self) -> LLMConfig:
        idx = int(self.llm_priority[0])
        return self.llm_list[idx]

    @llm.setter
    def llm(self, value: LLMConfig) -> None:
        idx = int(self.llm_priority[0])
        self.llm_list[idx] = value

    @staticmethod
    def create_default_file(path: str) -> bool:
        """在 path 不存在时原子创建默认配置，返回是否由本次创建。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("x", encoding="utf-8") as f:
                f.write(_DEFAULT_CONFIG_YAML)
            return True
        except FileExistsError:
            return False

    @classmethod
    def load(cls, path: str = "config.yaml") -> Config:
        """从 YAML 文件加载配置，并应用缺失字段的类型化默认值。"""
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls.from_dict(raw)

    @staticmethod
    def _parse_llm_config(llm_raw: dict[str, Any]) -> LLMConfig:
        tiers = {
            name: TierConfig.model_validate(t)
            for name, t in (llm_raw.get("tiers", {}) or {}).items()
        }
        return LLMConfig(
            provider=llm_raw.get("provider", "deepseek"),
            base_url=llm_raw.get("base_url"),
            api_key_env=llm_raw.get("api_key_env"),
            reasoning_style=llm_raw.get("reasoning_style", "none"),
            timeout=llm_raw.get("timeout", 600),
            max_retries=llm_raw.get("max_retries", 4),
            tiers=tiers,
            cli_path=llm_raw.get("cli_path"),
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        """把 YAML 对应的嵌套字典转换为运行时配置模型。"""
        if "llm" in raw and "llm_list" in raw:
            raise ValueError("不能同时配置 llm 与 llm_list")

        lang = raw.get("language", {})
        if "llm_list" in raw:
            llm_list_raw = raw.get("llm_list")
            if not isinstance(llm_list_raw, list):
                raise ValueError("llm_list 必须是列表")
            if not (1 <= len(llm_list_raw) <= 10):
                raise ValueError(f"llm_list 长度必须在 1 到 10 项之间，当前为 {len(llm_list_raw)}")
            llm_list = [cls._parse_llm_config(item or {}) for item in llm_list_raw]
        else:
            llm_raw = raw.get("llm", {}) or {}
            if not isinstance(llm_raw, dict):
                raise ValueError("llm 必须是字典")
            llm_list = [cls._parse_llm_config(llm_raw)]

        n = len(llm_list)
        if "llm_priority" in raw and raw["llm_priority"] is not None:
            p_val = raw["llm_priority"]
            if not isinstance(p_val, str):
                raise ValueError("llm_priority 必须是带引号的字符串")
            if len(p_val) != n:
                raise ValueError(
                    f"llm_priority 长度 ({len(p_val)}) 必须与 llm_list 项数 ({n}) 一致"
                )
            actual_chars = list(p_val)
            if len(actual_chars) != len(set(actual_chars)):
                raise ValueError(f"llm_priority 包含重复索引: {p_val}")
            expected_set = {str(i) for i in range(n)}
            for ch in actual_chars:
                if not ch.isdigit() or int(ch) >= n or int(ch) < 0:
                    raise ValueError(f"llm_priority 索引越界: {ch}")
            if set(actual_chars) != expected_set:
                raise ValueError(
                    f"llm_priority 存在缺失索引: 期望 {expected_set}，实际包含 {set(actual_chars)}"
                )
            llm_priority = p_val
        else:
            llm_priority = "".join(str(i) for i in range(n))

        segment = SegmentConfig.model_validate(raw.get("segment", {}) or {})
        pipeline = PipelineConfig.model_validate(raw.get("pipeline", {}) or {})
        output = OutputConfig.model_validate(raw.get("output", {}) or {})
        punct = raw.get("punctuation", {}) or {}
        return cls(
            source_lang=lang.get("source", "auto"),
            target_lang=lang.get("target", "zh"),
            llm_list=llm_list,
            llm_priority=llm_priority,
            segment=segment,
            pipeline=pipeline,
            output=output,
            honorific_strategy=raw.get("honorific", {}).get("strategy", "keep_style"),
            punctuation_normalize=bool(punct.get("normalize", True)),
            state_dir=raw.get("paths", {}).get("state_dir", "state"),
        )
