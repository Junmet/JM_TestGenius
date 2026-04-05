"""
单次任务结构化摘要：便于日志采集、运维看板与成本核对。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .pipeline import PipelineConfig, PipelineResult


def build_task_summary(
    *,
    result: PipelineResult,
    config: PipelineConfig,
    log_file: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    exports = sorted(config.export_formats) if config.export_formats else []
    outcomes: list[dict[str, Any]] = []
    for o in result.outcomes:
        outcomes.append(
            {
                "name": o.path.name,
                "ok": o.ok,
                "error_kind": o.error_kind,
                "output_count": len(o.output_paths or []),
            }
        )
    body: dict[str, Any] = {
        "schema_version": 1,
        "success_count": result.success_count,
        "fail_count": result.fail_count,
        "total_sources": result.total_files,
        "elapsed_seconds": round(result.total_elapsed_seconds, 3),
        "pipeline": {
            "max_cases": config.max_cases,
            "batch_size": config.batch_size,
            "max_chars": config.max_chars,
            "chunked_outline": config.chunked_outline,
            "outline_chunk_overlap": config.outline_chunk_overlap,
            "sleep_after_call": config.sleep_after_call,
            "sleep_between_files": config.sleep_between_files,
            "max_total_tokens": config.max_total_tokens,
            "exports": exports,
        },
        "usage": result.usage.token_comparison_dict(),
        "outcomes": outcomes,
        "log_file": str(log_file.resolve()) if log_file else None,
    }
    if extra:
        body["extra"] = extra
    return body


def log_task_summary_line(logger: logging.Logger, summary: dict[str, Any]) -> None:
    """单行 JSON，便于 grep / 日志平台解析。"""
    logger.info("TASK_SUMMARY %s", json.dumps(summary, ensure_ascii=False))


def write_task_summary_json(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
