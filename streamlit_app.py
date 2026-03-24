"""
本地 Web UI：选择输入/输出目录、查看进度、预览用例表。

在项目根目录执行：
  streamlit run streamlit_app.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st

from src.logging_config import setup_generation_logging
from src.parsers import iter_input_files
from src.pipeline import PipelineConfig, init_llm_from_env, run_pipeline
from src.usage import UsageTracker

logger = logging.getLogger(__name__)


def _strip_rich_markup(msg: str) -> str:
    """pipeline 与 CLI 共用带 Rich 标记的文案；Web 状态行转为纯文本。"""
    try:
        from rich.text import Text

        return Text.from_markup(msg, emoji=False).plain
    except Exception:
        import re

        return re.sub(r"\[[^[\]]*]", "", msg)


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
        run_btn = st.button("开始生成", type="primary")

    if not run_btn:
        st.info("在左侧填写目录与参数后，点击「开始生成」。")
        return

    inp = Path(input_dir).resolve()
    out = Path(output_dir).resolve()
    if not inp.is_dir():
        st.error(f"输入目录不存在或不是目录：{inp}")
        return
    files = list(iter_input_files(inp))
    if not files:
        st.warning("该目录下没有支持的文档（.docx / .md / .txt / .pdf）。")
        return
    out.mkdir(parents=True, exist_ok=True)

    log_file = setup_generation_logging(stream=True)
    logger.info("已启动 Web UI 生成任务")
    logger.info(
        "参数：input=%s output=%s encoding=%s max_cases=%s batch_size=%s max_chars=%s "
        "sleep_after_call=%s sleep_between_files=%s max_total_tokens=%s",
        inp,
        out,
        encoding,
        max_cases,
        batch_size,
        max_chars,
        sleep_call,
        sleep_file,
        max_tokens if max_tokens > 0 else None,
    )

    progress = st.progress(0.0)
    status = st.empty()

    def cb(msg: str, frac: float) -> None:
        progress.progress(min(1.0, max(0.0, frac)))
        status.markdown(f"**状态** {_strip_rich_markup(msg)}")

    try:
        cfg, llm = init_llm_from_env(None)
    except Exception as e:
        logger.exception("加载配置失败")
        st.error(f"加载配置失败：{e}")
        return

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
    )

    usage = UsageTracker()
    try:
        result = run_pipeline(
            files=files,
            cfg=cfg,
            llm=llm,
            config=pcfg,
            usage=usage,
            progress_callback=cb,
        )
    except Exception as e:
        logger.exception("Web UI 生成任务异常终止")
        st.exception(e)
        return

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
    u = result.usage
    st.success(
        f"完成：成功 {result.success_count}，失败 {result.fail_count}。"
        f" LLM 调用 {u.calls} 次。"
        f" **Token 总预估 ≈ {u.estimated_tokens}**（{u.token_estimate_source}）。"
        f" 耗时 {result.total_elapsed_seconds / 60:.1f} 分钟。"
    )
    st.caption(f"日志文件：`{log_file}`（与命令行相同，写入项目根目录 `log/`）")

    for o in result.outcomes:
        if o.ok:
            st.write("**✓**", o.path.name)
        else:
            st.write("**✗**", o.path.name, f"（{o.error_kind or '错误'}）")

    st.subheader("表格预览")
    xlsx_files = sorted(out.glob("*.testcases.xlsx"))
    if xlsx_files:
        pick = st.selectbox("选择文件", [p.name for p in xlsx_files], key="xlsx_pick")
        path = out / pick
        try:
            df = pd.read_excel(path, engine="openpyxl")
            st.dataframe(df, use_container_width=True, height=400)
        except Exception as e:
            st.warning(f"读取 Excel 失败：{e}")
    else:
        st.info("输出目录中暂无 *.testcases.xlsx（生成成功后刷新或检查输出路径）。")

    md_files = sorted(out.glob("*.testcases.md"))
    if md_files:
        with st.expander("Markdown 用例（节选）"):
            mp = st.selectbox("选择文件", [p.name for p in md_files], key="md_pick")
            text = (out / mp).read_text(encoding="utf-8")
            st.markdown(text[:12000] + ("…" if len(text) > 12000 else ""))


if __name__ == "__main__":
    main()
