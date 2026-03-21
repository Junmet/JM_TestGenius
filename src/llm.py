from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
import logging

from langchain_openai import ChatOpenAI
from pydantic import TypeAdapter, BaseModel, Field

from .config import AppConfig
from .models import GenerationResult, TestCase
from .prompts import (
    SYSTEM_EN,
    SYSTEM_ZH,
    USER_TEMPLATE,
    OUTLINE_USER_TEMPLATE,
    CASES_BATCH_USER_TEMPLATE,
)


logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """LLM 调用相关基础异常。"""


class LLMConnectionError(LLMError):
    """网络连接或网关异常。"""


class LLMLengthLimitError(LLMError):
    """模型输出达到长度上限。"""


class LLMJSONParseError(LLMError):
    """模型 JSON 解析异常。"""


class OutlineResult(BaseModel):
    """
    大纲阶段的 LLM 输出：
    - 上下文摘要（context_summary）用于后续多批用例复用，减少重复提示；
    - mindmap_mermaid / test_points / assumptions / risks / out_of_scope 等用于直接写结果。
    """

    source_name: str
    language: str
    context_summary: str = ""
    mindmap_mermaid: str
    test_points: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class CasesBatchResult(BaseModel):
    """
    单次“批量生成用例”调用的输出，只关心 test_cases 列表。
    """

    test_cases: list[TestCase] = Field(default_factory=list)


def build_llm(cfg: AppConfig) -> ChatOpenAI:
    """
    使用 DeepSeek 的 OpenAI 兼容接口初始化 LangChain Chat 模型。
    
    这里通过 base_url + model + api_key 把 DeepSeek 暴露为 OpenAI ChatCompletion 接口，
    并强制要求返回 JSON 对象，减少后续解析失败的概率。
    """
    logger.info(
        "正在初始化 LLM 客户端：provider=%s，base_url=%s，模型=%s，超时=%s，最大 token=%s",
        cfg.provider,
        cfg.base_url,
        cfg.model,
        cfg.timeout,
        cfg.max_tokens,
    )
    return ChatOpenAI(
        api_key=cfg.api_key,
        base_url=f"{cfg.base_url}/v1",
        model=cfg.model,
        temperature=0.2,
        timeout=cfg.timeout,
        max_tokens=cfg.max_tokens,
        # 尝试强制模型以 JSON 对象形式返回，降低语法错误概率
        model_kwargs={"response_format": {"type": "json_object"}},
    )


def generate_from_text(
    *,
    cfg: AppConfig,
    llm: ChatOpenAI,
    source_name: str,
    document_text: str,
    max_cases: int,
) -> GenerationResult:
    """
    旧版“一次性生成测试点 + 全部用例”的接口，目前保留以兼容/复用。

    由于容易触发 token/长度限制，新流程改为：
    - generate_outline：先拿摘要 + 测试点 + 思维导图；
    - generate_cases_batch：再按测试点分批补齐大量用例。
    """
    system = SYSTEM_ZH if cfg.language == "zh" else SYSTEM_EN

    schema = _generation_schema_json()
    user = USER_TEMPLATE.format(
        source_name=source_name,
        language=cfg.language,
        max_cases=max_cases,
        document_text=document_text,
        schema=schema,
    )

    logger.info(
        "正在调用 LLM 进行完整生成：来源=%s，最大用例数=%d，语言=%s",
        source_name,
        max_cases,
        cfg.language,
    )
    # 使用 messages API，避免 prompt 注入造成格式跑偏
    msg = _invoke_llm_with_classification(
        llm=llm,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        source_name=source_name,
        stage="完整生成",
    )

    raw = (msg.content or "").strip()
    data = _parse_or_debug(raw, debug_stem=f"full_{Path(source_name).stem}")
    
    # pydantic 强校验，失败会直接抛出异常，便于定位
    adapter = TypeAdapter(GenerationResult)
    result = adapter.validate_python(data)
    logger.info(
        "完整生成完成：来源=%s，用例数量=%d，测试点数量=%d",
        source_name,
        len(result.test_cases),
        len(result.test_points),
    )
    return result


def generate_outline(
    *,
    cfg: AppConfig,
    llm: ChatOpenAI,
    source_name: str,
    document_text: str,
) -> OutlineResult:
    """
    第 1 阶段：基于原始需求文档生成“测试大纲”：
    - context_summary 摘要：后续所有批次共用，减少重复 prompt；
    - mindmap_mermaid：最终写入思维导图文件；
    - test_points + assumptions/risks/out_of_scope：用于 meta 信息和后续用例扩展。
    """
    system = SYSTEM_ZH if cfg.language == "zh" else SYSTEM_EN
    schema = _outline_schema_json()
    user = OUTLINE_USER_TEMPLATE.format(
        source_name=source_name,
        language=cfg.language,
        document_text=document_text,
        schema=schema,
    )
    logger.info("正在生成大纲：来源=%s，语言=%s", source_name, cfg.language)
    msg = _invoke_llm_with_classification(
        llm=llm,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        source_name=source_name,
        stage="大纲生成",
    )
    raw = (msg.content or "").strip()
    data = _parse_or_debug(raw, debug_stem=f"outline_{Path(source_name).stem}")
    outline = TypeAdapter(OutlineResult).validate_python(data)
    logger.info(
        "大纲生成完成：来源=%s，测试点数量=%d",
        source_name,
        len(outline.test_points),
    )
    return outline


def generate_cases_batch(
    *,
    cfg: AppConfig,
    llm: ChatOpenAI,
    source_name: str,
    context_summary: str,
    test_point: str,
    batch_size: int,
    existing_titles: list[str],
) -> CasesBatchResult:
    """
    第 2 阶段：围绕单个测试点，批量生成若干条测试用例。

    - context_summary：来自第 1 阶段的大纲摘要；
    - test_point：当前要展开的测试点；
    - batch_size：本批次期望的用例条数；
    - existing_titles：用于避免生成重复标题（仅传入末尾若干条以控制 prompt 长度）。
    """
    system = SYSTEM_ZH if cfg.language == "zh" else SYSTEM_EN
    schema = _cases_batch_schema_json()
    existing_titles_str = "\n".join([f"- {t}" for t in existing_titles[-120:]]) if existing_titles else "（无）"
    user = CASES_BATCH_USER_TEMPLATE.format(
        source_name=source_name,
        language=cfg.language,
        context_summary=context_summary,
        test_point=test_point,
        batch_size=batch_size,
        existing_titles=existing_titles_str,
        schema=schema,
    )

    # 批量生成时模型偶尔返回不合法 JSON（缺逗号等），解析失败时自动重试最多 2 次
    last_error: Exception | None = None
    for attempt in range(3):
        logger.info(
            "正在生成用例批次（第 %d/3 次）：来源=%s，测试点=%.30s，每批大小=%d，已有标题数=%d",
            attempt + 1,
            source_name,
            test_point,
            batch_size,
            len(existing_titles),
        )
        msg = _invoke_llm_with_classification(
            llm=llm,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            source_name=source_name,
            stage=f"批量用例生成-第{attempt + 1}次",
        )
        raw = (msg.content or "").strip()
        try:
            data = _parse_or_debug(raw, debug_stem=f"cases_{Path(source_name).stem}")
            result = TypeAdapter(CasesBatchResult).validate_python(data)
            logger.info(
                "用例批次生成完成：来源=%s，测试点=%.30s，本批生成=%d",
                source_name,
                test_point,
                len(result.test_cases),
            )
            return result
        except LLMJSONParseError as e:
            last_error = e
            logger.warning(
                "第 %d/3 次批量用例 JSON 解析失败（来源=%s，测试点=%.30s）：%s",
                attempt + 1,
                source_name,
                test_point,
                e,
            )
            if attempt < 2:
                continue
            raise
    if last_error is not None:
        raise last_error
    raise LLMJSONParseError("批量用例 JSON 解析失败（已重试多次仍失败）。")


def _parse_or_debug(raw: str, *, debug_stem: str) -> Any:
    """
    统一的 JSON 解析入口：
    - 优先尝试用 _safe_json_loads 提取 JSON；
    - 如失败，把原始内容写入 debug/ 目录，方便排查且不影响 output；
    - 抛出 RuntimeError 给上层，避免静默失败。
    """
    try:
        return _safe_json_loads(raw)
    except Exception as e:  # noqa: BLE001
        # P0-1：在批量用例场景尝试“部分恢复”，尽量避免整批作废
        if debug_stem.startswith("cases_"):
            recovered = _recover_cases_batch_from_raw(raw)
            if recovered.get("test_cases"):
                logger.warning(
                    "批量用例 JSON 整体解析失败，已恢复可用用例 %d 条并继续。",
                    len(recovered["test_cases"]),
                )
                return recovered

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_dir = Path("debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_path = debug_dir / f"debug_{debug_stem}_{ts}.txt"
        debug_path.write_text(raw or "<空响应>", encoding="utf-8")
        logger.error(
            "模型返回 JSON 解析失败，已将原始内容保存到 %s：%s",
            debug_path,
            e,
        )
        raise LLMJSONParseError(
            f"模型返回 JSON 解析失败。原始内容已保存到 {debug_path}。"
        ) from e


def _invoke_llm_with_classification(
    *,
    llm: ChatOpenAI,
    messages: list[dict[str, str]],
    source_name: str,
    stage: str,
):
    try:
        return llm.invoke(messages)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        msg_lower = msg.lower()
        logger.error("LLM 调用失败（阶段=%s，来源=%s）：%s", stage, source_name, msg)
        if (
            "length limit" in msg_lower
            or "maximum context length" in msg_lower
            or "completion_tokens=4096" in msg_lower
            or "context_length_exceeded" in msg_lower
        ):
            raise LLMLengthLimitError(f"{stage}失败：模型输出达到长度限制。") from e
        if (
            "connection error" in msg_lower
            or "timeout" in msg_lower
            or "timed out" in msg_lower
            or "connection reset" in msg_lower
            or "network" in msg_lower
        ):
            raise LLMConnectionError(f"{stage}失败：网络连接异常。") from e
        raise LLMError(f"{stage}失败：{msg}") from e


def _recover_cases_batch_from_raw(raw: str) -> dict[str, list[dict[str, Any]]]:
    """
    从损坏的批量响应中尽量恢复可用 test_cases。
    规则：先提取 test_cases 数组片段，再逐个对象尝试解析与校验。
    """
    if not raw:
        return {"test_cases": []}

    array_text = _extract_test_cases_array(raw)
    if not array_text:
        return {"test_cases": []}

    recovered: list[dict[str, Any]] = []
    adapter = TypeAdapter(TestCase)
    for obj_text in _extract_json_objects(array_text):
        try:
            obj = _safe_json_loads(obj_text)
            validated = adapter.validate_python(obj)
            recovered.append(validated.model_dump())
        except Exception:
            continue
    return {"test_cases": recovered}


def _extract_test_cases_array(raw: str) -> str:
    key_pos = raw.find('"test_cases"')
    if key_pos == -1:
        return ""
    start = raw.find("[", key_pos)
    if start == -1:
        return ""

    in_string = False
    escape = False
    depth = 0
    for i in range(start, len(raw)):
        ch = raw[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return raw[start : i + 1]
    return raw[start:]


def _extract_json_objects(text: str) -> list[str]:
    objects: list[str] = []
    in_string = False
    escape = False
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                objects.append(text[start : i + 1])
                start = -1
    return objects


def _repair_json(json_str: str) -> str:
    """
    尝试修复模型返回中常见的 JSON 语法错误：尾逗号、相邻对象间缺逗号。
    """
    # 尾逗号：, ] 或 , } 改为 ] / }
    json_str = re.sub(r",\s*]", "]", json_str)
    json_str = re.sub(r",\s*}", "}", json_str)
    # 数组/对象之间缺逗号：} 后面紧跟 { 时补逗号
    json_str = re.sub(r"}\s*{", "}, {", json_str)
    return json_str


def _safe_json_loads(s: str) -> Any:
    """
    尝试从模型返回内容中提取 JSON：
    - 去除 BOM 和首尾空白
    - 自动从包含 ```json ... ``` 或解释性文字的长文本中“截取”第一个完整的 JSON 对象
    - 解析失败时尝试简单修复（尾逗号、缺逗号）后再解析
    """
    if not s:
        raise ValueError("模型返回为空响应。")

    # 去掉 BOM / 首尾空白
    s = s.strip("\ufeff \t\r\n")

    # 若整体是 markdown 代码块，先粗略剥离 ``` 包裹及 json 语言标签
    if s.startswith("```"):
        # 删除首尾成对的 ```
        s = s.strip("`")
        # 删除第一行可能的语言标识
        if "\n" in s:
            first_line, rest = s.split("\n", 1)
            if first_line.lower().startswith("json"):
                s = rest
            else:
                s = first_line + "\n" + rest

    s = s.strip()

    # 确定待解析的 JSON 片段
    if s.startswith("{") and s.endswith("}"):
        json_str = s
    else:
        start = s.find("{")
        end = s.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("模型返回中未找到 JSON 对象。")
        json_str = s[start : end + 1]

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass
    # 尝试修复常见语法错误后再解析
    repaired = _repair_json(json_str)
    return json.loads(repaired)


def _generation_schema_json() -> str:
    """
    给模型一份“目标结构”的强提示，显著提升结构化输出稳定性。
    """
    schema_obj: dict[str, Any] = {
        "type": "object",
        "required": [
            "source_name",
            "language",
            "mindmap_mermaid",
            "test_points",
            "test_cases",
            "assumptions",
            "risks",
            "out_of_scope",
        ],
        "properties": {
            "source_name": {"type": "string"},
            "language": {"type": "string"},
            "mindmap_mermaid": {"type": "string", "description": "Mermaid mindmap content only. First line must be mindmap."},
            "test_points": {"type": "array", "items": {"type": "string"}},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "out_of_scope": {"type": "array", "items": {"type": "string"}},
            "test_cases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "id",
                        "priority",
                        "module",
                        "title",
                        "summary",
                        "preconditions",
                        "steps",
                        "expected",
                        "actual_result",
                        "test_type",
                        "data",
                        "remarks",
                    ],
                    "properties": {
                        "id": {"type": "string"},
                        "priority": {"type": "string"},
                        "module": {"type": "string"},
                        "title": {"type": "string"},
                        "summary": {"type": "string", "description": "摘要，一句话概括本用例验证点"},
                        "preconditions": {"type": "string"},
                        "steps": {"type": "array", "items": {"type": "string"}},
                        "expected": {"type": "array", "items": {"type": "string"}},
                        "actual_result": {"type": "string", "description": "实际结果，执行后填写，生成时可为空字符串"},
                        "test_type": {"type": "string"},
                        "data": {"type": "string"},
                        "remarks": {"type": "string"},
                    },
                },
            },
        },
    }
    return json.dumps(schema_obj, ensure_ascii=False, indent=2)


def _outline_schema_json() -> str:
    schema_obj: dict[str, Any] = {
        "type": "object",
        "required": [
            "source_name",
            "language",
            "context_summary",
            "mindmap_mermaid",
            "test_points",
            "assumptions",
            "risks",
            "out_of_scope",
        ],
        "properties": {
            "source_name": {"type": "string"},
            "language": {"type": "string"},
            "context_summary": {"type": "string"},
            "mindmap_mermaid": {"type": "string"},
            "test_points": {"type": "array", "items": {"type": "string"}},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "out_of_scope": {"type": "array", "items": {"type": "string"}},
        },
    }
    return json.dumps(schema_obj, ensure_ascii=False, indent=2)


def _cases_batch_schema_json() -> str:
    schema_obj: dict[str, Any] = {
        "type": "object",
        "required": ["test_cases"],
        "properties": {
            "test_cases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "id",
                        "priority",
                        "module",
                        "title",
                        "summary",
                        "preconditions",
                        "steps",
                        "expected",
                        "actual_result",
                        "test_type",
                        "data",
                        "remarks",
                    ],
                    "properties": {
                        "id": {"type": "string"},
                        "priority": {"type": "string"},
                        "module": {"type": "string"},
                        "title": {"type": "string"},
                        "summary": {"type": "string", "description": "摘要，一句话概括本用例验证点"},
                        "preconditions": {"type": "string"},
                        "steps": {"type": "array", "items": {"type": "string"}},
                        "expected": {"type": "array", "items": {"type": "string"}},
                        "actual_result": {"type": "string", "description": "实际结果，执行后填写，生成时可为空字符串"},
                        "test_type": {"type": "string"},
                        "data": {"type": "string"},
                        "remarks": {"type": "string"},
                    },
                },
            }
        },
    }
    return json.dumps(schema_obj, ensure_ascii=False, indent=2)

