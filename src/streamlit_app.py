"""
本地 Web UI：选择输入/输出目录、查看进度、预览用例表。

在项目根目录执行：
  streamlit run src/streamlit_app.py
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

# 保证 `from src...` 可解析（与 `python -m src.main` 一致，工作目录为项目根）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st

from src.input_loader import collect_parsed_documents, normalize_url_lines
from src.llm import set_llm_io_logging
from src.logging_config import setup_generation_logging
from src.parsers import iter_input_files
from src.pipeline import PipelineConfig, init_llm_from_env, run_pipeline
from src.task_summary import build_task_summary, log_task_summary_line, write_task_summary_json
from src.ui_paths import reveal_path_in_os
from src.usage import UsageTracker

logger = logging.getLogger(__name__)

# 与命令行 main.py 中失败提示语义对齐，便于用户自助排查
_ERROR_KIND_HINTS_ZH: dict[str, str] = {
    "budget": "累计 token 超过「累计 token 上限」；可调高上限、减少文档数或降低「每文档最多用例数」。",
    "length": "模型单次输出过长被截断；可调小「每批生成条数」或查看日志；程序已自动拆批与减半重试。",
    "connection": "网络或代理异常；请检查网络后重试。",
    "json": "模型返回 JSON 无法解析；可查看 log/ 与项目下 debug/ 中保存的原始片段。",
    "auth": "API Key 无效或未授权；请检查 .env 中 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY 及 LLM_PROVIDER。",
    "other": "未分类错误；请查看终端与 log/ 完整堆栈。",
}

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


def _serialize_gen_session(
    result: Any,
    *,
    output_dir: Path,
    log_file: Path,
    n_local: int,
    n_remote: int,
    provider: str = "",
    model: str = "",
) -> dict[str, Any]:
    """上次生成结果写入 session_state，切换下拉框重跑脚本时仍能展示。"""
    u = result.usage
    outcomes: list[dict[str, Any]] = []
    for o in result.outcomes:
        outs = o.output_paths or []
        outcomes.append(
            {
                "name": o.path.name,
                "ok": o.ok,
                "error_kind": o.error_kind,
                "output_paths": [p.name for p in outs],
                "output_paths_full": [str(p.resolve()) for p in outs],
            }
        )
    return {
        "output_dir": str(output_dir.resolve()),
        "log_file": str(log_file.resolve()),
        "success_count": result.success_count,
        "fail_count": result.fail_count,
        "total_elapsed_seconds": result.total_elapsed_seconds,
        "usage_calls": u.calls,
        "estimated_tokens": u.estimated_tokens,
        "token_estimate_source": u.token_estimate_source,
        "outcomes": outcomes,
        "n_local": n_local,
        "n_remote": n_remote,
        "provider": provider,
        "model": model,
    }


def _render_results_from_session(gen: dict[str, Any]) -> None:
    out = Path(gen["output_dir"])
    log_fp = Path(gen["log_file"])
    prov = (gen.get("provider") or "").strip()
    mdl = (gen.get("model") or "").strip()
    model_line = f"模型：**{prov}** / **{mdl}**。" if prov and mdl else ""

    st.success(
        f"完成：成功 {gen['success_count']}，失败 {gen['fail_count']}。"
        f" LLM 调用 {gen['usage_calls']} 次。"
        f" **Token 总预估 ≈ {gen['estimated_tokens']}**（{gen['token_estimate_source']}）。"
        f" 耗时 {gen['total_elapsed_seconds'] / 60:.1f} 分钟。"
        + (f" {model_line}" if model_line else "")
    )

    n_loc = gen.get("n_local", 0)
    n_rem = gen.get("n_remote", 0)
    st.caption(f"本任务来源：**{n_loc + n_rem}** 个（本地 {n_loc} + 远程 {n_rem}），与命令行统计方式一致。")

    _path_key = hashlib.sha256(str(gen["log_file"]).encode()).hexdigest()[:16]
    with st.expander("输出目录、日志路径（可复制）", expanded=False):
        st.text_input(
            "输出目录（绝对路径）",
            value=str(out.resolve()),
            key=f"{_path_key}_out",
            disabled=True,
        )
        st.text_input(
            "日志文件（绝对路径）",
            value=str(log_fp.resolve()),
            key=f"{_path_key}_log",
            disabled=True,
        )
        b1, b2 = st.columns(2)
        with b1:
            if st.button("在系统中打开输出目录", key=f"{_path_key}_btn_out"):
                ok, msg = reveal_path_in_os(out)
                (st.success if ok else st.warning)(msg)
        with b2:
            if st.button("在系统中打开日志所在目录", key=f"{_path_key}_btn_logdir"):
                ok, msg = reveal_path_in_os(log_fp.parent)
                (st.success if ok else st.warning)(msg)

    failed = [r for r in gen["outcomes"] if not r["ok"]]
    if failed:
        with st.expander(f"失败来源（{len(failed)}）与处理建议", expanded=True):
            for row in failed:
                kind = row.get("error_kind") or "other"
                hint = _ERROR_KIND_HINTS_ZH.get(kind, _ERROR_KIND_HINTS_ZH["other"])
                st.markdown(f"**✗ {row['name']}**  · 类型：`{kind}`")
                st.caption(hint)

    st.subheader("各来源摘要")
    for row in gen["outcomes"]:
        if row["ok"]:
            names = row.get("output_paths") or []
            extra = f" → {', '.join(names[:4])}" + ("…" if len(names) > 4 else "") if names else ""
            st.markdown(f"**✓** `{row['name']}`{extra}")
        else:
            kind = row.get("error_kind") or "other"
            st.markdown(f"**✗** `{row['name']}`（{kind}）")

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
    st.caption(
        "在项目根目录执行：`streamlit run src/streamlit_app.py`（需已配置 `.env` 中的 API Key）。"
        "参数与行为与 `python -m src.main` 对齐（同目录扫描规则、远程 URL 行规则）。"
    )

    with st.sidebar:
        input_dir = st.text_input("输入目录", value="input", help="放置 .docx / .md / .txt / .pdf 的文件夹")
        output_dir = st.text_input("输出目录", value="output")
        encoding = st.text_input("文本编码", value="utf-8")
        max_cases = st.number_input("每文档最多用例数", min_value=1, value=80, step=1)
        batch_size = st.number_input(
            "每批生成条数",
            min_value=1,
            value=10,
            step=1,
            help="与 CLI --batch-size 一致；较大时会自动拆成多次请求合并，降低单次 JSON 截断风险。",
        )
        max_chars = st.number_input("正文最大字符数", min_value=1000, value=15000, step=500)
        sleep_call = st.slider("每次 LLM 调用后休眠(秒)", 0.0, 5.0, 0.0, 0.5)
        sleep_file = st.slider("文件之间休眠(秒)", 0.0, 30.0, 0.0, 1.0)
        max_tokens = st.number_input("累计 token 上限（0=不限制）", min_value=0, value=0, step=10000)
        chunked_outline = st.checkbox(
            "长文档分段大纲",
            value=False,
            help="对应 CLI --chunked-outline：正文超过「正文最大字符数」时按段生成大纲再合并，减少后半未读盲区（大纲调用次数增加）。",
        )
        outline_overlap = st.number_input(
            "分段重叠字符",
            min_value=0,
            max_value=5000,
            value=400,
            step=50,
            help="对应 --outline-chunk-overlap，减轻段边界丢上下文。",
        )
        task_summary_json = st.text_input(
            "任务摘要 JSON（可选）",
            value="",
            help="对应 --task-summary-json：相对项目根或绝对路径；留空则仅写入日志中的 TASK_SUMMARY 行。",
        )
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
            height=120,
            help="每行一条；空行与 # 开头注释会被忽略（与命令行 --url-file 一致）。可与本地文件同时使用。",
            placeholder="https://example.com/wiki/page\nfeishu:https://xxx.feishu.cn/docx/...\nconfluence:https://xxx.atlassian.net/wiki/...",
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
            raw_url_lines = (url_text or "").splitlines()
            url_lines = normalize_url_lines(raw_url_lines)
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
                    "sleep_after_call=%s sleep_between_files=%s max_total_tokens=%s exports=%s "
                    "chunked_outline=%s outline_overlap=%s task_summary_json=%s",
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
                    chunked_outline,
                    outline_overlap,
                    (task_summary_json or "").strip() or None,
                )

                progress = st.progress(0.0)
                status = st.empty()
                summary_bar = st.empty()

                def cb(msg: str, frac: float) -> None:
                    f = min(1.0, max(0.0, frac))
                    # 预留前 5% 给加载阶段，避免一上来进度条为 0 无反馈
                    progress.progress(0.05 + 0.95 * f)
                    plain = _strip_rich_markup(msg)
                    status.markdown(f"**进度** {f * 100:.0f}% · {plain}")

                try:
                    status.markdown("**进度** 正在拉取远程需求并解析本地文件…")
                    progress.progress(0.02)
                    documents = collect_parsed_documents(
                        local_files=files,
                        url_lines=url_lines,
                        encoding=encoding,
                    )
                except Exception as e:
                    logger.exception("加载需求失败")
                    progress.progress(0.0)
                    status.empty()
                    st.error(f"加载需求失败：{e}")
                else:
                    n_total = len(documents)
                    summary_bar.info(
                        f"**任务** {n_total} 个来源（本地 **{len(files)}** + 远程 **{len(url_lines)}**）"
                        f" → `{out}`（与命令行汇总行一致）"
                    )
                    try:
                        cfg, llm = init_llm_from_env(None)
                    except Exception as e:
                        logger.exception("加载配置失败")
                        progress.progress(0.0)
                        status.empty()
                        st.error(f"加载配置失败：{e}")
                    else:
                        progress.progress(0.05)
                        status.markdown(
                            f"**进度** 已就绪 · 模型 **{cfg.provider}** / **{cfg.model}** · 共 **{n_total}** 个来源待处理"
                        )
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
                            chunked_outline=chunked_outline,
                            outline_chunk_overlap=int(outline_overlap),
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
                            progress.progress(0.0)
                            status.empty()
                            st.error("任务异常终止（流水线内部通常已记录到各来源结果中；以下为未捕获异常）")
                            st.exception(e)
                            st.caption(f"完整日志：`{log_file.resolve()}`")
                        else:
                            progress.progress(1.0)
                            status.markdown("**进度** 全部完成。")
                            summary_bar.empty()
                            logger.info(
                                "Web UI 任务结束：成功=%d 失败=%d LLM 调用=%d 估算 token≈%d 耗时=%.2f 分钟 日志=%s",
                                result.success_count,
                                result.fail_count,
                                result.usage.calls,
                                result.usage.estimated_tokens,
                                result.total_elapsed_seconds / 60.0,
                                log_file,
                            )
                            cmp = result.usage.token_comparison_dict()
                            logger.info(
                                "Token 对比：char÷4粗估=%d 接口上报累计=%d 差值(上报-粗估)=%s",
                                cmp["char_div4_token_estimate"],
                                cmp["sum_reported_total_tokens"],
                                cmp["reported_minus_char_div4"],
                            )
                            ts = build_task_summary(result=result, config=pcfg, log_file=log_file)
                            log_task_summary_line(logger, ts)
                            tsp = (task_summary_json or "").strip()
                            if tsp:
                                write_task_summary_json(Path(tsp).resolve(), ts)
                            st.session_state[SESSION_GEN] = _serialize_gen_session(
                                result,
                                output_dir=out,
                                log_file=log_file,
                                n_local=len(files),
                                n_remote=len(url_lines),
                                provider=cfg.provider,
                                model=cfg.model,
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
