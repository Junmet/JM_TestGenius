from __future__ import annotations

from datetime import datetime
from pathlib import Path
import logging
from typing import Any

import pandas as pd

from .models import GenerationResult, TestCase


logger = logging.getLogger(__name__)


def build_testcase_rows(result: GenerationResult) -> list[dict[str, Any]]:
    """与 Excel 表头一致的行数据，供导出模板复用。"""
    rows: list[dict[str, Any]] = []
    for tc in result.test_cases:
        rows.append(
            {
                "编号": tc.id,
                "优先级": tc.priority,
                "模块": tc.module,
                "测试标题": tc.title,
                "摘要": tc.summary,
                "前置条件": tc.preconditions,
                "测试步骤": _join_list(tc.steps),
                "期望结果": _join_list(tc.expected),
                "实际结果（可为空）": tc.actual_result,
                "类型": tc.test_type,
                "测试数据": tc.data,
                "备注": tc.remarks,
            }
        )
    return rows


def write_outputs(
    result: GenerationResult,
    output_dir: Path,
    *,
    export_formats: frozenset[str] | None = None,
) -> list[Path]:
    """
    将一次生成结果写入磁盘：
    - 思维导图 XMind（测试点 + 测试用例两个分支）
    - 测试用例 Markdown 表格
    - 测试用例 Excel
    - meta 信息（Mermaid 思维导图 / 测试点 / 假设 / 风险 / 范围）
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(result.source_name).stem
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result, id_renamed = _deduplicate_test_case_ids(result)
    if id_renamed:
        logger.info("已自动重编号 %d 条用例（重复或空编号），避免导入 TMS 时 ID 冲突", id_renamed)
    quality_warnings = _validate_result_quality(result)
    for w in quality_warnings:
        logger.warning("质量校验告警：%s", w)

    paths: list[Path] = []

    # 思维导图 XMind：根节点下两个分支「测试点」「测试用例」
    xmind_path = output_dir / f"{stem}.xmind"
    logger.info("正在写入 XMind 文件：%s", xmind_path)
    _write_xmind(result, xmind_path)
    paths.append(xmind_path)

    testcases_md_path = output_dir / f"{stem}.testcases.md"
    testcases_xlsx_path = output_dir / f"{stem}.testcases.xlsx"
    meta_path = output_dir / f"{stem}.meta.md"

    logger.info("正在写入 Markdown 测试用例：%s", testcases_md_path)
    testcases_md_path.write_text(_render_testcases_md(result, ts), encoding="utf-8")

    logger.info("正在写入 Excel 测试用例：%s", testcases_xlsx_path)
    _write_testcases_xlsx(result, testcases_xlsx_path)

    rows = build_testcase_rows(result)
    if export_formats is None:
        export_formats = frozenset({"csv", "zentao", "testlink", "jira"})
    if export_formats:
        from .export_templates import write_template_exports

        extra = write_template_exports(result, output_dir, stem, export_formats, rows)
        paths.extend(extra)

    logger.info("正在写入元信息文件：%s", meta_path)
    meta_path.write_text(_render_meta_md(result, ts, quality_warnings), encoding="utf-8")
    paths.extend([testcases_md_path, testcases_xlsx_path, meta_path])

    logger.info(
        "文件输出完成：来源=%s，总用例数=%d",
        result.source_name,
        len(result.test_cases),
    )
    return paths


def _render_testcases_md(result: GenerationResult, ts: str) -> str:
    header = f"<!-- 生成时间：{ts}；来源：{result.source_name}；输出语言：{result.language} -->\n\n"
    table = _markdown_table(result.test_cases)
    return header + table + "\n"


def _markdown_table(cases: list[TestCase]) -> str:
    cols = [
        "编号",
        "优先级",
        "模块",
        "测试标题",
        "摘要",
        "前置条件",
        "测试步骤",
        "期望结果",
        "实际结果（可为空）",
        "类型",
        "测试数据",
        "备注",
    ]
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]

    for tc in cases:
        row = [
            _escape_md(tc.id),
            _escape_md(tc.priority),
            _escape_md(tc.module),
            _escape_md(tc.title),
            _escape_md(tc.summary),
            _escape_md(tc.preconditions),
            _escape_md(_join_list(tc.steps)),
            _escape_md(_join_list(tc.expected)),
            _escape_md(tc.actual_result),
            _escape_md(tc.test_type),
            _escape_md(tc.data),
            _escape_md(tc.remarks),
        ]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _write_testcases_xlsx(result: GenerationResult, path: Path) -> None:
    rows = build_testcase_rows(result)
    # 用 pandas 写入 Excel，方便评审和导入测试管理系统
    df = pd.DataFrame(rows)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="测试用例")
    logger.info("Excel 已保存：%s（行数=%d）", path, len(rows))


def _xmind_truncate(s: str, max_len: int = 120) -> str:
    s = (s or "").strip().replace("\r\n", " ").replace("\n", " ")
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def _write_xmind(result: GenerationResult, path: Path) -> None:
    """
    将测试点与测试用例写入 XMind 思维导图：
    - 根节点：来源名称
    - 分支一「测试点」：每个测试点一个子节点
    - 分支二「测试用例」：每个用例含「测试标题」「前置条件」「测试步骤」；
      前置条件与测试步骤同级，期望结果为测试步骤的下一级。
    """
    try:
        from py_xmind16 import Workbook
    except ImportError:
        raise RuntimeError("生成 XMind 需要安装 py-xmind16，请执行: pip install py-xmind16")

    workbook = Workbook()
    sheet = workbook.create_sheet(Path(result.source_name).stem or "测试大纲")
    root = sheet.get_root_topic()
    root.title = Path(result.source_name).stem or "测试大纲"

    # 分支一：测试点
    branch_points = root.add_subtopic("测试点")
    for pt in (result.test_points or []):
        title = (pt or "").strip()
        if title:
            branch_points.add_subtopic(_xmind_truncate(title, 200))

    # 分支二：测试用例（含测试标题、前置条件、测试步骤、期望结果）
    branch_cases = root.add_subtopic("测试用例")
    for tc in (result.test_cases or []):
        case_title = (tc.title or "").strip() or "(无标题)"
        case_id = (tc.id or "").strip()
        node_title = f"{case_id} {case_title}"[:200] if case_id else case_title[:200]
        case_node = branch_cases.add_subtopic(node_title)

        # 前置条件（与测试步骤同级）
        pre_node = case_node.add_subtopic("前置条件")
        if (tc.preconditions or "").strip():
            pre_node.add_subtopic(_xmind_truncate(tc.preconditions, 150))

        # 测试步骤（与前置条件同级），其下一级为「期望结果」
        steps_node = case_node.add_subtopic("测试步骤")
        for i, step in enumerate(tc.steps or [], 1):
            steps_node.add_subtopic(f"{i}. {_xmind_truncate(step, 100)}")
        expected_node = steps_node.add_subtopic("期望结果")
        for i, exp in enumerate(tc.expected or [], 1):
            expected_node.add_subtopic(f"{i}. {_xmind_truncate(exp, 100)}")

    workbook.save(str(path))
    logger.info(
        "XMind 已保存：%s（测试点=%d，用例=%d）",
        path,
        len(result.test_points or []),
        len(result.test_cases or []),
    )


def _render_meta_md(result: GenerationResult, ts: str, quality_warnings: list[str]) -> str:
    def bullets(items: list[str]) -> str:
        if not items:
            return "- （无）\n"
        return "".join([f"- {i}\n" for i in items])

    mm = (result.mindmap_mermaid or "").strip()
    if mm:
        # 大纲阶段已生成 mindmap 语法；此处加围栏便于 GitHub/GitLab 等直接渲染
        mermaid_block = f"```mermaid\n{mm}\n```\n\n"
    else:
        mermaid_block = "_（本稿未生成 Mermaid 思维导图内容）_\n\n"

    return (
        f"## 生成信息\n\n"
        f"- **生成时间**: {ts}\n"
        f"- **来源文件**: {result.source_name}\n"
        f"- **输出语言**: {result.language}\n\n"
        f"## 思维导图（Mermaid）\n\n"
        f"{mermaid_block}"
        f"## 测试点（列表）\n\n"
        + bullets(result.test_points)
        + "\n## 假设\n\n"
        + bullets(result.assumptions)
        + "\n## 风险\n\n"
        + bullets(result.risks)
        + "\n## 不在范围\n\n"
        + bullets(result.out_of_scope)
        + "\n## 质量检查告警\n\n"
        + bullets(quality_warnings)
    )


def _next_free_case_id(used: set[str]) -> str:
    n = 1
    while True:
        cand = f"TC-{n:03d}"
        if cand not in used:
            return cand
        n += 1


def _deduplicate_test_case_ids(result: GenerationResult) -> tuple[GenerationResult, int]:
    """
    按出现顺序保留首次出现的编号；空编号或与已占用编号重复的用例自动分配 TC-001 起未占用编号。
    返回 (新结果, 重编号条数)。
    """
    used: set[str] = set()
    renamed = 0
    new_cases: list[TestCase] = []
    for tc in result.test_cases or []:
        raw = (tc.id or "").strip()
        if raw and raw not in used:
            used.add(raw)
            new_cases.append(tc)
            continue
        new_id = _next_free_case_id(used)
        used.add(new_id)
        renamed += 1
        new_cases.append(tc.model_copy(update={"id": new_id}))
    if not renamed:
        return result, 0
    return result.model_copy(update={"test_cases": new_cases}), renamed


def _join_list(items: list[str]) -> str:
    items = [i.strip() for i in items if i and i.strip()]
    if not items:
        return ""
    # Excel/Markdown 都比较友好的分隔方式
    return "\n".join([f"{idx+1}. {it}" for idx, it in enumerate(items)])


def _escape_md(s: str) -> str:
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    # 表格中避免破坏列：把 | 转义
    s = s.replace("|", "\\|")
    return s.strip()


def _validate_result_quality(result: GenerationResult) -> list[str]:
    warnings: list[str] = []
    seen_ids: set[str] = set()
    duplicate_ids: list[str] = []
    empty_titles = 0
    empty_steps = 0
    empty_expected = 0
    short_titles = 0

    for tc in result.test_cases or []:
        tc_id = (tc.id or "").strip()
        if tc_id:
            if tc_id in seen_ids:
                duplicate_ids.append(tc_id)
            else:
                seen_ids.add(tc_id)

        title = (tc.title or "").strip()
        if not title:
            empty_titles += 1
        elif len(title) < 4:
            short_titles += 1

        if not (tc.steps or []):
            empty_steps += 1
        if not (tc.expected or []):
            empty_expected += 1

    if duplicate_ids:
        uniq = sorted(set(duplicate_ids))
        preview = "、".join(uniq[:10])
        suffix = "..." if len(uniq) > 10 else ""
        warnings.append(f"检测到重复用例编号 {len(uniq)} 个：{preview}{suffix}")
    if empty_titles:
        warnings.append(f"检测到空测试标题 {empty_titles} 条")
    if short_titles:
        warnings.append(f"检测到过短测试标题 {short_titles} 条（长度小于 4）")
    if empty_steps:
        warnings.append(f"检测到无测试步骤的用例 {empty_steps} 条")
    if empty_expected:
        warnings.append(f"检测到无期望结果的用例 {empty_expected} 条")
    if not warnings:
        warnings.append("未发现明显质量问题")
    return warnings

