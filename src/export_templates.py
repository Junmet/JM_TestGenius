"""
在 canonical 用例行（与 Excel 列一致）之上做列映射，导出 CSV / 禅道 / TestLink / Jira 等模板。

各系统版本差异较大，导入前请在目标系统中核对列名与样例；此处提供常见可编辑模板。
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pandas as pd

from .models import GenerationResult

logger = logging.getLogger(__name__)

ALLOWED_EXPORTS = frozenset({"csv", "zentao", "testlink", "jira"})


def parse_export_formats(raw: str) -> frozenset[str]:
    """
    解析 --exports：逗号分隔；none 表示不生成额外模板文件（仍保留 xlsx/md/meta/xmind）。
    """
    t = (raw or "").strip().lower()
    if t in ("", "none", "no"):
        return frozenset()
    parts = [p.strip().lower() for p in t.split(",") if p.strip()]
    unknown = set(parts) - ALLOWED_EXPORTS
    if unknown:
        raise ValueError(
            f"不支持的导出格式: {sorted(unknown)}；允许: {sorted(ALLOWED_EXPORTS)}"
        )
    return frozenset(parts)


# 与 writers.build_testcase_rows 列名一致（便于文档化）
CANONICAL_KEYS = [
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


def write_template_exports(
    result: GenerationResult,
    output_dir: Path,
    stem: str,
    formats: frozenset[str],
    rows: list[dict[str, Any]],
) -> list[Path]:
    """按 formats 写入额外文件；rows 已与 Excel 一致。"""
    out: list[Path] = []
    if "csv" in formats:
        p = output_dir / f"{stem}.testcases.csv"
        _write_csv(rows, p)
        out.append(p)
    if "zentao" in formats:
        p = output_dir / f"{stem}.zentao.csv"
        _write_zentao(rows, p)
        out.append(p)
    if "testlink" in formats:
        p = output_dir / f"{stem}.testlink.xml"
        _write_testlink_xml(result, rows, p)
        out.append(p)
    if "jira" in formats:
        p = output_dir / f"{stem}.jira.csv"
        _write_jira(rows, p)
        out.append(p)
    return out


def _write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    df = pd.DataFrame(rows, columns=CANONICAL_KEYS)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("已导出 CSV（UTF-8 BOM）：%s", path)


def _write_zentao(rows: list[dict[str, Any]], path: Path) -> None:
    """
    禅道开源版常见「用例」CSV 导入字段（中文列名，可按需在 Excel 中微调后导入）。
    参考：产品-测试-用例-导入，模板列名因版本可能略有差异。
    """
    mapped: list[dict[str, Any]] = []
    for r in rows:
        mapped.append(
            {
                "用例编号": r.get("编号", ""),
                "用例标题": r.get("测试标题", ""),
                "所属模块": r.get("模块", ""),
                "前置条件": r.get("前置条件", ""),
                "步骤": r.get("测试步骤", ""),
                "预期": r.get("期望结果", ""),
                "优先级": r.get("优先级", ""),
                "用例类型": r.get("类型", "") or "功能测试",
                "关键词": r.get("测试数据", ""),
                "备注": r.get("备注", ""),
            }
        )
    pd.DataFrame(mapped).to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("已导出禅道风格 CSV：%s", path)


def _write_testlink_xml(result: GenerationResult, rows: list[dict[str, Any]], path: Path) -> None:
    """
    TestLink 1.9.x 常用 testsuite/testcase XML 结构（步骤合并为多条 step）。
    导入：测试规范 → 导入测试套件（具体菜单因版本而异）。
    """
    suite = ET.Element("testsuite")
    suite.set("name", Path(result.source_name).stem or "ImportedSuite")
    details = ET.SubElement(suite, "details")
    details.text = (result.context_summary or "")[:2000]

    for r in rows:
        tc_el = ET.SubElement(suite, "testcase")
        tc_el.set("name", (r.get("测试标题") or "untitled")[:500])
        summ = ET.SubElement(tc_el, "summary")
        summ.text = r.get("摘要", "") or ""
        pre = ET.SubElement(tc_el, "preconditions")
        pre.text = r.get("前置条件", "") or ""

        steps_el = ET.SubElement(tc_el, "steps")
        step_lines = _split_numbered_lines(r.get("测试步骤", "") or "")
        exp_lines = _split_numbered_lines(r.get("期望结果", "") or "")
        n = max(len(step_lines), len(exp_lines), 1)
        for i in range(n):
            step = ET.SubElement(steps_el, "step")
            sn = ET.SubElement(step, "step_number")
            sn.text = str(i + 1)
            act = ET.SubElement(step, "actions")
            act.text = step_lines[i] if i < len(step_lines) else ""
            er = ET.SubElement(step, "expectedresults")
            er.text = exp_lines[i] if i < len(exp_lines) else ""

    tree = ET.ElementTree(suite)
    ET.indent(tree, space="  ")
    path.write_bytes(ET.tostring(suite, encoding="utf-8", xml_declaration=True))
    logger.info("已导出 TestLink XML：%s", path)


def _split_numbered_lines(block: str) -> list[str]:
    """与 Excel 中「1. xxx」分行一致，拆成每条步骤字符串。"""
    if not block.strip():
        return []
    lines = []
    for part in re.split(r"\n+", block.strip()):
        part = re.sub(r"^\d+\.\s*", "", part.strip())
        if part:
            lines.append(part)
    return lines if lines else [block.strip()]


def _jira_priority(raw: str) -> str:
    p = (raw or "").upper()
    if "P0" in p or "最高" in raw:
        return "Highest"
    if "P1" in p:
        return "High"
    if "P2" in p:
        return "Medium"
    if "P3" in p:
        return "Low"
    return "Medium"


def _write_jira(rows: list[dict[str, Any]], path: Path) -> None:
    """
    Jira 通用 CSV 思路：Summary + Description（合并步骤/期望），便于 Zephyr/Xray 或自定义脚本再加工。
    列名使用英文，适配「从文件创建事务」类导入或二次转换。
    """
    mapped: list[dict[str, Any]] = []
    for r in rows:
        desc_parts = [
            f"**Summary (摘要)**: {r.get('摘要', '')}",
            f"**Preconditions**: {r.get('前置条件', '')}",
            f"**Steps**:\n{r.get('测试步骤', '')}",
            f"**Expected**:\n{r.get('期望结果', '')}",
            f"**Type**: {r.get('类型', '')}",
            f"**Data**: {r.get('测试数据', '')}",
            f"**Remarks**: {r.get('备注', '')}",
        ]
        body = "\n\n".join(desc_parts)
        mapped.append(
            {
                "Issue Type": "Test",
                "Summary": (r.get("测试标题", "") or "")[:254],
                "Description": body,
                "Priority": _jira_priority(str(r.get("优先级", ""))),
                "Component": r.get("模块", ""),
                "Labels": r.get("编号", ""),
            }
        )
    pd.DataFrame(mapped).to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("已导出 Jira 风格 CSV：%s", path)
