"""
本地 Web UI：选择输入/输出目录、查看进度、预览用例表。

在项目根目录执行：
  streamlit run streamlit_app.py
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st

from src.input_loader import collect_parsed_documents
from src.llm import set_llm_io_logging
from src.logging_config import setup_generation_logging
from src.parsers import iter_input_files
from src.pipeline import PipelineConfig, init_llm_from_env, run_pipeline
from src.usage import UsageTracker

logger = logging.getLogger(__name__)

SESSION_GEN = "jm_last_generation"

_fragment = getattr(st, "fragment", None)
if _fragment is None:

    def _fragment(fn):  # type: ignore[no-untyped-def]
        return fn


def _strip_rich_markup(msg: str) -> str:
    """pipeline 与 CLI 共用带 Rich 标记的文案；Web 状态行转为纯文本。"""
    try:
        from rich.text import Text

        return Text.from_markup(msg, emoji=False).plain
    except Exception:
        import re

        return re.sub(r"\[[^[\]]*]", "", msg)


def _serialize_gen_session(result: Any, *, output_dir: Path, log_file: Path) -> dict[str, Any]:
    """上次生成结果写入 session_state，切换下拉框重跑脚本时仍能展示。"""
    u = result.usage
    outcomes: list[dict[str, Any]] = []
    for o in result.outcomes:
        outcomes.append(
            {
                "name": o.path.name,
                "ok": o.ok,
                "error_kind": o.error_kind,
                "output_paths": [p.name for p in (o.output_paths or [])],
            }
        )
    return {
        "output_dir": str(output_dir.resolve()),
        "log_file": str(log_file),
        "success_count": result.success_count,
        "fail_count": result.fail_count,
        "total_elapsed_seconds": result.total_elapsed_seconds,
        "usage_calls": u.calls,
        "estimated_tokens": u.estimated_tokens,
        "token_estimate_source": u.token_estimate_source,
        "outcomes": outcomes,
    }


def _render_results_from_session(gen: dict[str, Any]) -> None:
    out = Path(gen["output_dir"])
    st.success(
        f"完成：成功 {gen['success_count']}，失败 {gen['fail_count']}。"
        f" LLM 调用 {gen['usage_calls']} 次。"
        f" **Token 总预估 ≈ {gen['estimated_tokens']}**（{gen['token_estimate_source']}）。"
        f" 耗时 {gen['total_elapsed_seconds'] / 60:.1f} 分钟。"
    )
    st.caption(
        f"日志文件：`{gen['log_file']}`（与命令行相同，写入项目根目录 `log/`）\n\n"
        f"**预览目录**（上次生成写入）：`{out}`"
    )

    for row in gen["outcomes"]:
        if row["ok"]:
            st.write("**✓**", row["name"])
        else:
            st.write("**✗**", row["name"], f"（{row.get('error_kind') or '错误'}）")

    _file_preview_fragment()


@_fragment
def _file_preview_fragment() -> None:
    """仅预览区在切换文件时局部重跑（Streamlit >=1.33），减轻前端 DOM 异常。"""
    gen = st.session_state.get(SESSION_GEN)
    if not gen:
        return
    out = Path(gen["output_dir"])
    st.subheader("表格预览")
    xlsx_files = sorted(out.glob("*.testcases.xlsx"))
    if xlsx_files:
        pick = st.selectbox("选择 Excel 文件", [p.name for p in xlsx_files], key="jm_xlsx_pick")
        path = out / pick
        try:
            df = pd.read_excel(path, engine="openpyxl")
            st.dataframe(df, use_container_width=True, height=400)
        except Exception as e:
            st.warning(f"读取 Excel 失败：{e}")
    else:
        st.info("该输出目录中暂无 *.testcases.xlsx。")

    md_files = sorted(out.glob("*.testcases.md"))
    if md_files:
        st.subheader("Markdown 用例（节选）")
        mp = st.selectbox("选择 Markdown 文件", [p.name for p in md_files], key="jm_md_pick")
        text = (out / mp).read_text(encoding="utf-8")
        st.markdown(text[:12000] + ("…" if len(text) > 12000 else ""))


def main() -> None:
    st.set_page_config(page_title="JM_TestGenius", layout="wide")
    st.title("JM_TestGenius · 需求 → 测试用例")
    st.caption("在项目根目录执行：streamlit run streamlit_app.py（需已配置 .env 中的 API Key）")

    with st.sidebar:
        input_dir = st.text_input("输入目录", value="input", help="放置 .docx / .md / .txt / .pdf 的文件夹")
        output_dir = st.text_input("输出目录", value="output")
        encoding = st.text_input("文本编码", value="utf-8")
        max_cases = st.number_input("每文档最多用例数", min_value=1, value=80, step=1)
        batch_size = st.number_input("每批生成条数", min_value=1, value=10, step=1)
        max_chars = st.number_input("正文最大字符数", min_value=1000, value=15000, step=500)
        sleep_call = st.slider("每次 LLM 调用后休眠(秒)", 0.0, 5.0, 0.0, 0.5)
        sleep_file = st.slider("文件之间休眠(秒)", 0.0, 30.0, 0.0, 1.0)
        max_tokens = st.number_input("累计 token 上限（0=不限制）", min_value=0, value=0, step=10000)
        verbose_console = st.checkbox(
            "控制台详细日志（终端输出 src 包 DEBUG/INFO）",
            value=False,
            help="与命令行 --verbose 类似；完整日志仍在 log/ 文件。",
        )
        llm_log_io = st.checkbox(
            "LLM 请求/响应摘要（仅长度与截断预览，已遮蔽常见密钥形态）",
            value=False,
            help="也可用环境变量 LLM_LOG_IO=1；关闭则不记录。",
        )
        export_choices = st.multiselect(
            "额外导出格式（与 Excel 同源，列映射为各工具模板）",
            ["csv", "zentao", "testlink", "jira"],
            default=[],
            help="禅道=CSV；TestLink=XML；Jira=CSV。默认不选，仅生成 xlsx/md/meta/xmind。",
        )
        url_text = st.text_area(
            "远程需求 URL（可选）",
            height=96,
            help="每行一条：http(s) 网页；或 confluence:页面URL、feishu:文档URL（需在 .env 配置凭证）。可与本地文件同时使用。",
            placeholder="https://example.com/wiki/page\nconfluence:https://xxx.atlassian.net/wiki/spaces/SPACE/pages/123/...",
        )
        run_btn = st.button("开始生成", type="primary")
        if st.button("清除结果", help="仅清空本页展示，不删除 output 目录中的文件"):
            st.session_state.pop(SESSION_GEN, None)
            st.rerun()

    if run_btn:
        inp = Path(input_dir).resolve()
        out = Path(output_dir).resolve()
        if not inp.is_dir():
            st.error(f"输入目录不存在或不是目录：{inp}")
        else:
            files = list(iter_input_files(inp))
            url_lines = [ln.strip() for ln in (url_text or "").splitlines() if ln.strip() and not ln.strip().startswith("#")]
            if not files and not url_lines:
                st.warning("请至少提供：输入目录中的支持文档，或上方「远程需求 URL」。")
            else:
                out.mkdir(parents=True, exist_ok=True)

                log_file = setup_generation_logging(stream=True, verbose=verbose_console)
                _env_io = (os.getenv("LLM_LOG_IO") or "").strip().lower() in ("1", "true", "yes", "on")
                set_llm_io_logging(llm_log_io or _env_io)
                logger.info("已启动 Web UI 生成任务")
                logger.info(
                    "参数：input=%s output=%s encoding=%s max_cases=%s batch_size=%s max_chars=%s "
                    "sleep_after_call=%s sleep_between_files=%s max_total_tokens=%s exports=%s",
                    inp,
                    out,
                    encoding,
                    max_cases,
                    batch_size,
                    max_chars,
                    sleep_call,
                    sleep_file,
                    max_tokens if max_tokens > 0 else None,
                    export_choices,
                )

                progress = st.progress(0.0)
                status = st.empty()

                def cb(msg: str, frac: float) -> None:
                    progress.progress(min(1.0, max(0.0, frac)))
                    status.markdown(f"**状态** {_strip_rich_markup(msg)}")

                try:
                    documents = collect_parsed_documents(
                        local_files=files,
                        url_lines=url_lines,
                        encoding=encoding,
                    )
                except Exception as e:
                    logger.exception("加载需求失败")
                    st.error(f"加载需求失败：{e}")
                else:
                    try:
                        cfg, llm = init_llm_from_env(None)
                    except Exception as e:
                        logger.exception("加载配置失败")
                        st.error(f"加载配置失败：{e}")
                    else:
                        pcfg = PipelineConfig(
                            output_dir=out,
                            encoding=encoding,
                            language=None,
                            max_cases=max_cases,
                            batch_size=batch_size,
                            max_chars=max_chars,
                            sleep_after_call=sleep_call,
                            sleep_between_files=sleep_file,
                            max_total_tokens=max_tokens if max_tokens > 0 else None,
                            export_formats=frozenset(export_choices),
                        )

                        usage = UsageTracker()
                        try:
                            result = run_pipeline(
                                documents=documents,
                                cfg=cfg,
                                llm=llm,
                                config=pcfg,
                                usage=usage,
                                progress_callback=cb,
                            )
                        except Exception as e:
                            logger.exception("Web UI 生成任务异常终止")
                            st.exception(e)
                        else:
                            progress.progress(1.0)
                            status.empty()
                            logger.info(
                                "Web UI 任务结束：成功=%d 失败=%d LLM 调用=%d 估算 token≈%d 耗时=%.2f 分钟 日志=%s",
                                result.success_count,
                                result.fail_count,
                                result.usage.calls,
                                result.usage.estimated_tokens,
                                result.total_elapsed_seconds / 60.0,
                                log_file,
                            )
                            st.session_state[SESSION_GEN] = _serialize_gen_session(
                                result, output_dir=out, log_file=log_file
                            )

    gen = st.session_state.get(SESSION_GEN)
    if not gen:
        st.info(
            "在左侧填写目录与参数后，点击「开始生成」。生成完成后可切换下方「选择文件」查看不同需求的用例，无需重新生成。"
        )
        return

    _render_results_from_session(gen)


if __name__ == "__main__":
    main()
