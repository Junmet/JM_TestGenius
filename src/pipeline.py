from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from langchain_openai import ChatOpenAI

from .config import AppConfig, load_config
from .llm import (
    build_llm,
    generate_outline,
    generate_cases_batch,
    merge_outline_results,
    OutlineResult,
    LLMConnectionError,
    LLMLengthLimitError,
    LLMJSONParseError,
    LLMAuthenticationError,
)
from .text_segments import slice_segments
from .models import GenerationResult
from .parsers import ParsedDocument
from .usage import UsageBudgetExceeded, UsageTracker, check_usage_budget
from .writers import write_outputs


logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, float], None]


@dataclass
class PipelineConfig:
    output_dir: Path
    encoding: str
    language: str | None
    max_cases: int
    batch_size: int
    max_chars: int
    sleep_after_call: float = 0.0
    sleep_between_files: float = 0.0
    max_total_tokens: int | None = None
    # None 与空 frozenset 均表示不生成 csv/zentao/testlink/jira；非空则按集合写出
    export_formats: frozenset[str] | None = None
    # 长文档：全文超过 max_chars 时对每段分别生成大纲再合并（增加 LLM 调用，减少截断盲区）
    chunked_outline: bool = False
    outline_chunk_overlap: int = 400


def _source_kind_label(path: Path) -> str:
    n = path.name.lower()
    if ".remote." in n or n.endswith(".remote.md"):
        if n.startswith("feishu_"):
            return "feishu-remote"
        return "remote-url"
    suf = path.suffix.lower()
    if suf == ".pdf":
        return "pdf"
    return suf.lstrip(".") or "local"


def _log_truncation_kept_window(path: Path, full: str, max_chars: int) -> None:
    kept = full[:max_chars]
    head = kept[:120].replace("\n", "↵")
    tail = kept[-120:].replace("\n", "↵") if len(kept) > 120 else head
    logger.warning(
        "正文截断：%s 类型=%s 原长=%d 保留区间=[0,%d) max_chars=%d；"
        "保留段开头(120字)=%r 结尾(120字)=%r",
        path.name,
        _source_kind_label(path),
        len(full),
        max_chars,
        max_chars,
        head,
        tail,
    )


@dataclass
class FileOutcome:
    path: Path
    ok: bool
    error_kind: str | None = None
    output_paths: list[Path] = field(default_factory=list)


@dataclass
class PipelineResult:
    success_count: int
    fail_count: int
    total_files: int
    usage: UsageTracker
    outcomes: list[FileOutcome]
    total_elapsed_seconds: float


def run_pipeline(
    *,
    documents: list[ParsedDocument],
    cfg: AppConfig,
    llm: ChatOpenAI,
    config: PipelineConfig,
    usage: UsageTracker | None = None,
    progress_callback: ProgressCallback | None = None,
) -> PipelineResult:
    """
    核心生成流水线：解析 → 大纲 → 分批用例 → 写出。
    供 CLI 与 Web UI 共用。documents 可由本地文件或远程 URL/Wiki 解析得到。
    """
    usage = usage or UsageTracker()
    outcomes: list[FileOutcome] = []
    success_count = 0
    fail_count = 0
    total_files = len(documents)
    start_ts = time.time()

    def _prog(msg: str, frac: float) -> None:
        if progress_callback:
            progress_callback(msg, max(0.0, min(1.0, frac)))

    for i, parsed in enumerate(documents, 1):
        path = parsed.path
        base = (i - 1) / max(1, total_files)
        _prog(
            f"({i}/{total_files}) [bold]解析[/bold] {path.name}",
            base + 0.01 / max(1, total_files),
        )
        try:
            file_start_ts = time.time()
            logger.info("正在处理来源 %d/%d：%s", i, total_files, path)
            full_text = parsed.text
            full_len = len(full_text)
            sk = _source_kind_label(path)

            if full_len <= config.max_chars:
                logger.info(
                    "正文未截断：%s 类型=%s 长度=%d（≤ max_chars=%d）",
                    path.name,
                    sk,
                    full_len,
                    config.max_chars,
                )
                logger.info(
                    "生成大纲：%s（provider=%s model=%s）",
                    path.name,
                    cfg.provider,
                    cfg.model,
                )
                doc_for_outline = full_text
                outline = generate_outline(
                    cfg=cfg,
                    llm=llm,
                    source_name=path.name,
                    document_text=doc_for_outline,
                    usage=usage,
                    sleep_after_call=config.sleep_after_call,
                )
            elif config.chunked_outline:
                overlap = max(0, config.outline_chunk_overlap)
                segs = slice_segments(full_text, config.max_chars, overlap)
                logger.info(
                    "长文档分段大纲：%s 类型=%s 全文=%d 字 段长=%d 重叠=%d 共 %d 段",
                    path.name,
                    sk,
                    full_len,
                    config.max_chars,
                    overlap,
                    len(segs),
                )
                logger.info(
                    "生成大纲（分段）：%s（provider=%s model=%s）",
                    path.name,
                    cfg.provider,
                    cfg.model,
                )
                parts: list[OutlineResult] = []
                for idx, (start, end, chunk) in enumerate(segs, 1):
                    logger.info(
                        "分段边界：%s 第 %d/%d 段 字符区间=[%d,%d) 长度=%d",
                        path.name,
                        idx,
                        len(segs),
                        start,
                        end,
                        end - start,
                    )
                    hdr = (
                        f"[系统说明：全文共 {len(segs)} 段，当前为第 {idx} 段，"
                        f"字符半开区间=[{start},{end})，本段长度 {end - start}。"
                        f"请仅根据本段正文提炼大纲与测试点，勿编造本段未出现的功能。]\n\n"
                    )
                    o = generate_outline(
                        cfg=cfg,
                        llm=llm,
                        source_name=f"{path.name} [段{idx}/{len(segs)}]",
                        document_text=hdr + chunk,
                        usage=usage,
                        sleep_after_call=config.sleep_after_call,
                    )
                    parts.append(o)
                    check_usage_budget(usage, config.max_total_tokens)
                outline = merge_outline_results(parts, final_source_name=path.name)
                logger.info(
                    "分段大纲已合并：%s 合并后测试点=%d",
                    path.name,
                    len(outline.test_points),
                )
            else:
                _log_truncation_kept_window(path, full_text, config.max_chars)
                doc_for_outline = full_text[: config.max_chars]
                logger.info(
                    "生成大纲（截断后）：%s 类型=%s（provider=%s model=%s）",
                    path.name,
                    sk,
                    cfg.provider,
                    cfg.model,
                )
                outline = generate_outline(
                    cfg=cfg,
                    llm=llm,
                    source_name=path.name,
                    document_text=doc_for_outline,
                    usage=usage,
                    sleep_after_call=config.sleep_after_call,
                )
            check_usage_budget(usage, config.max_total_tokens)

            logger.info(
                "分批生成用例：目标=%d 每批=%d 文件=%s",
                config.max_cases,
                config.batch_size,
                path.name,
            )

            all_cases: list = []
            existing_titles: list[str] = []
            seen_titles: set[str] = set()
            total_candidates = 0
            discarded_duplicates = 0
            consecutive_zero_new_batches = 0
            tp_idx = 0
            total_batches = 0

            max_batches_limit = max(3, (config.max_cases // max(1, config.batch_size)) * 3)
            max_consecutive_zero_new = 5

            while (
                len(all_cases) < config.max_cases
                and outline.test_points
                and total_batches < max_batches_limit
            ):
                check_usage_budget(usage, config.max_total_tokens)
                test_point = outline.test_points[tp_idx % len(outline.test_points)]
                tp_idx += 1
                remaining = config.max_cases - len(all_cases)
                batch_size = min(config.batch_size, remaining)
                total_batches += 1

                logger.info(
                    "请求第 %d 批用例：当前数量=%d，测试点=%.80s",
                    total_batches,
                    len(all_cases),
                    test_point,
                )
                batch = generate_cases_batch(
                    cfg=cfg,
                    llm=llm,
                    source_name=path.name,
                    context_summary=outline.context_summary,
                    test_point=test_point,
                    batch_size=batch_size,
                    existing_titles=existing_titles,
                    usage=usage,
                    sleep_after_call=config.sleep_after_call,
                )
                check_usage_budget(usage, config.max_total_tokens)

                new_count = 0
                batch_candidates = len(batch.test_cases or [])
                total_candidates += batch_candidates
                for tc in batch.test_cases:
                    title_key = (tc.title or "").strip()
                    key = title_key if title_key else f"ID:{tc.id}".strip()

                    if key in seen_titles:
                        discarded_duplicates += 1
                        continue

                    seen_titles.add(key)
                    all_cases.append(tc)
                    if title_key:
                        existing_titles.append(title_key)
                    new_count += 1
                    if len(all_cases) >= config.max_cases:
                        break

                logger.info(
                    "批次已完成：本批新增=%d，累计数量=%d，测试点=%.80s",
                    new_count,
                    len(all_cases),
                    test_point,
                )
                tp_short = (test_point or "").strip().replace("\n", " ")
                if len(tp_short) > 48:
                    tp_short = tp_short[:45] + "…"
                _prog(
                    f"[cyan]批次 {total_batches}[/cyan] | 新增 [green]{new_count}[/green] | "
                    f"累计 [bold]{len(all_cases)}[/bold] | [dim]{tp_short}[/dim]",
                    base
                    + min(
                        0.92 / max(1, total_files),
                        (0.1 + 0.75 * total_batches / max_batches_limit) / max(1, total_files),
                    ),
                )
                if new_count == 0:
                    consecutive_zero_new_batches += 1
                    if consecutive_zero_new_batches >= max_consecutive_zero_new:
                        logger.warning(
                            "触发兜底：连续 %d 批新增为 0，停止继续生成。",
                            max_consecutive_zero_new,
                        )
                        break
                else:
                    consecutive_zero_new_batches = 0

            if len(all_cases) < config.max_cases:
                logger.info(
                    "未达到目标条数：目标=%d 实际=%d 文件=%s",
                    config.max_cases,
                    len(all_cases),
                    path.name,
                )
            else:
                logger.info(
                    "已达到目标条数：%d 文件=%s",
                    len(all_cases),
                    path.name,
                )

            result = GenerationResult.model_validate({
                "source_name": outline.source_name,
                "language": outline.language,
                "context_summary": outline.context_summary,
                "mindmap_mermaid": outline.mindmap_mermaid,
                "test_points": outline.test_points,
                "test_cases": [tc.model_dump() for tc in all_cases],
                "assumptions": outline.assumptions,
                "risks": outline.risks,
                "out_of_scope": outline.out_of_scope,
            })
            # 输出文件名以本地/远程解析名为准，避免模型返回的 source_name 与磁盘不一致
            result = result.model_copy(update={"source_name": path.name})

            logger.info("写入输出文件：%s", path.name)
            out_paths = write_outputs(
                result,
                config.output_dir,
                export_formats=config.export_formats,
            )
            file_elapsed_min = (time.time() - file_start_ts) / 60.0
            logger.info(
                "文档处理完成：source=%s，用时=%.2f 分钟",
                path.name,
                file_elapsed_min,
            )
            outcomes.append(FileOutcome(path=path, ok=True, output_paths=out_paths))
            success_count += 1
            _prog(
                f"[bold green]完成[/bold green] [cyan]{path.name}[/cyan] | 耗时 [bold]{file_elapsed_min:.2f}[/bold] 分钟",
                i / max(1, total_files),
            )

        except UsageBudgetExceeded as e:
            fail_count += 1
            logger.warning("用量上限：%s", e)
            outcomes.append(FileOutcome(path=path, ok=False, error_kind="budget"))
            break
        except LLMLengthLimitError:
            fail_count += 1
            logger.exception("处理失败：%s", path)
            outcomes.append(FileOutcome(path=path, ok=False, error_kind="length"))
        except LLMConnectionError:
            fail_count += 1
            logger.exception("处理失败：%s", path)
            outcomes.append(FileOutcome(path=path, ok=False, error_kind="connection"))
        except LLMJSONParseError:
            fail_count += 1
            logger.exception("处理失败：%s", path)
            outcomes.append(FileOutcome(path=path, ok=False, error_kind="json"))
        except LLMAuthenticationError:
            fail_count += 1
            logger.exception("处理失败：%s", path)
            outcomes.append(FileOutcome(path=path, ok=False, error_kind="auth"))
        except Exception:
            fail_count += 1
            logger.exception("处理失败：%s", path)
            outcomes.append(FileOutcome(path=path, ok=False, error_kind="other"))

        if i < total_files and config.sleep_between_files > 0:
            time.sleep(config.sleep_between_files)

    elapsed = time.time() - start_ts
    return PipelineResult(
        success_count=success_count,
        fail_count=fail_count,
        total_files=total_files,
        usage=usage,
        outcomes=outcomes,
        total_elapsed_seconds=elapsed,
    )


def init_llm_from_env(language_override: str | None) -> tuple[AppConfig, ChatOpenAI]:
    cfg = load_config(override_language=language_override)
    llm = build_llm(cfg)
    return cfg, llm
