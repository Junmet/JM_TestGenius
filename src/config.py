from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from dotenv import load_dotenv
import os


Language = Literal["zh", "en"]
LLMProvider = Literal["deepseek", "qwen"]


@dataclass(frozen=True)
class AppConfig:
    """
    应用运行所需的配置集合，从 .env / 环境变量中加载。
    支持 OpenAI 兼容接口：DeepSeek、通义千问（DashScope）等。
    """
    provider: LLMProvider
    api_key: str
    base_url: str
    model: str
    language: Language = "zh"
    timeout: int = 120
    max_tokens: int = 16384


def _normalize_base_url(base_url: str) -> str:
    base_url = base_url.strip().rstrip("/")
    # 兼容用户填写 https://api.deepseek.com/v1 或 .../compatible-mode/v1
    if base_url.endswith("/v1"):
        base_url = base_url[: -len("/v1")]
    return base_url


def _parse_provider(raw: str) -> LLMProvider:
    v = (raw or "deepseek").strip().lower()
    if v == "qwen":
        return "qwen"
    return "deepseek"


def load_config(override_language: Optional[str] = None) -> AppConfig:
    """
    从环境变量加载配置。

    模型提供方（必填其一逻辑）：
    - LLM_PROVIDER=deepseek（默认）：需 DEEPSEEK_API_KEY
    - LLM_PROVIDER=qwen：需 DASHSCOPE_API_KEY 或 QWEN_API_KEY

    通用覆盖（可选）：
    - LLM_API_KEY：覆盖上述任一 Key，便于脚本切换
    - LLM_BASE_URL：覆盖默认 base（不要以 /v1 结尾；程序会拼接 /v1）
    - LLM_MODEL：覆盖默认模型名
    - LLM_TIMEOUT / LLM_MAX_TOKENS：若未设置则回退到 DEEPSEEK_TIMEOUT / DEEPSEEK_MAX_TOKENS

    DeepSeek 专用：
    - DEEPSEEK_BASE_URL（默认 https://api.deepseek.com）
    - DEEPSEEK_MODEL（默认 deepseek-chat）

    通义千问（DashScope OpenAI 兼容）：
    - DASHSCOPE_BASE_URL（默认 https://dashscope.aliyuncs.com/compatible-mode）
    - QWEN_MODEL（默认 qwen-plus，也可用 qwen-turbo、qwen-max 等）
    """
    load_dotenv(override=True)

    provider = _parse_provider(os.getenv("LLM_PROVIDER", "deepseek"))

    api_key = (os.getenv("LLM_API_KEY") or "").strip()
    if not api_key:
        if provider == "qwen":
            api_key = (os.getenv("DASHSCOPE_API_KEY") or os.getenv("QWEN_API_KEY") or "").strip()
        else:
            api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()

    if not api_key:
        if provider == "qwen":
            raise ValueError(
                "通义千问：请在 .env 中配置 DASHSCOPE_API_KEY（或 QWEN_API_KEY），"
                "或使用通用 LLM_API_KEY"
            )
        raise ValueError("缺少 DEEPSEEK_API_KEY：请在 .env 中配置，或使用 LLM_PROVIDER=qwen 切换千问")

    llm_base = (os.getenv("LLM_BASE_URL") or "").strip()
    if llm_base:
        base_url = _normalize_base_url(llm_base)
    elif provider == "qwen":
        base_url = _normalize_base_url(
            os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode")
        )
    else:
        base_url = _normalize_base_url(os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))

    llm_model = (os.getenv("LLM_MODEL") or "").strip()
    if llm_model:
        model = llm_model
    elif provider == "qwen":
        model = (os.getenv("QWEN_MODEL") or "qwen-plus").strip() or "qwen-plus"
    else:
        model = (os.getenv("DEEPSEEK_MODEL") or "deepseek-chat").strip() or "deepseek-chat"

    language_raw = (override_language or os.getenv("APP_LANGUAGE", "zh")).strip().lower()
    language: Language = "zh" if language_raw not in ("en", "zh") else language_raw  # type: ignore[assignment]

    timeout_raw = (os.getenv("LLM_TIMEOUT") or os.getenv("DEEPSEEK_TIMEOUT", "120")).strip()
    max_tokens_raw = (os.getenv("LLM_MAX_TOKENS") or os.getenv("DEEPSEEK_MAX_TOKENS", "16384")).strip()
    try:
        timeout = int(timeout_raw)
    except ValueError:
        timeout = 120
    try:
        max_tokens = int(max_tokens_raw)
    except ValueError:
        max_tokens = 16384

    return AppConfig(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        language=language,
        timeout=timeout,
        max_tokens=max_tokens,
    )
