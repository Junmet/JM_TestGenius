"""
合并本地文件与远程 URL/Wiki 行为一组 ParsedDocument，供流水线消费。
"""
from __future__ import annotations

import logging
from pathlib import Path

from .parsers import ParsedDocument, parse_document, parsed_document_from_text
from .remote_sources import resolve_remote_line

logger = logging.getLogger(__name__)


def normalize_url_lines(raw_lines: list[str]) -> list[str]:
    """
    去空行、以 # 开头的注释行，与 collect_parsed_documents 中过滤规则一致。
    用于 CLI 在统计与校验前与真实远程条数对齐（urls.txt 里常见空行分隔）。
    """
    out: list[str] = []
    for line in raw_lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def collect_parsed_documents(
    *,
    local_files: list[Path],
    url_lines: list[str],
    encoding: str,
    http_timeout: float = 60.0,
) -> list[ParsedDocument]:
    out: list[ParsedDocument] = []
    for path in local_files:
        out.append(parse_document(path, encoding=encoding))

    for line in url_lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            stem, text = resolve_remote_line(line, timeout=http_timeout)
        except Exception as e:
            logger.exception("远程拉取失败：%s", line[:120])
            raise RuntimeError(f"远程拉取失败（{line[:80]}…）：{e}") from e
        if not (text or "").strip():
            raise RuntimeError(f"远程内容为空：{line[:120]}")
        doc = parsed_document_from_text(stem, text)
        out.append(doc)
        logger.info("已拉取远程需求：%s → %s（%d 字）", line[:80], doc.path.name, len(text))

    return out
