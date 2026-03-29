from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import time

from rich.console import Console
from rich.table import Table

from .export_templates import parse_export_formats
from .llm import set_llm_io_logging
from .logging_config import setup_generation_logging
from .parsers import iter_input_files
from .pipeline import PipelineConfig, init_llm_from_env, run_pipeline
from .usage import UsageTracker


console = Console()
logger = logging.getLogger(__name__)


def main() -> int:
    """
    命令行入口：
    1. 扫描 input 目录中的需求文档；
    2. 为每个文档抽取文本，调用 LLM 生成大纲（摘要 + 测试点 + 思维导图）；
    3. 按测试点分批生成大量测试用例并汇总；
    4. 把用例表格 / meta 信息写入 output 目录。
    """
    args = _parse_args()
    start_ts = time.time()

    log_file = setup_generation_logging(stream=False, verbose=args.verbose)
    set_llm_io_logging(args.llm_log_io or _env_bool("LLM_LOG_IO"))
    logger.info("已启动用例生成命令行")
    logger.info("日志文件：%s", log_file)
    logger.info("已解析参数：%s", vars(args))

    input_dir = Path(args.input).resolve()
    output_dir = Path(args.output).resolve()
    if not input_dir.exists():
        input_dir.mkdir(parents=True, exist_ok=True)
        msg = f"未找到输入目录，已自动创建：{input_dir}"
        console.print(
            f"[yellow]{msg}[/yellow]\n请把需求文档放入该目录后重新运行。"
        )
        logger.warning(msg)
        return 0
    if not input_dir.is_dir():
        logger.error("输入路径存在但不是目录：%s", input_dir)
        raise ValueError(f"输入路径存在但不是目录：{input_dir}")

    files = list(iter_input_files(input_dir))
    if not files:
        msg = f"输入目录中未找到支持的文档类型：{input_dir}"
        console.print(f"[yellow]{msg}[/yellow]")
        logger.warning(msg)
        return 0

    logger.info("正在加载配置并初始化 LLM 客户端")
    cfg, llm = init_llm_from_env(args.language)
    logger.info("已使用模型：provider=%s，model=%s", cfg.provider, cfg.model)
    console.print(
        f"[bold]模型[/bold] [cyan]{cfg.provider}[/cyan] / [cyan]{cfg.model}[/cyan]"
    )

    max_total_tokens = args.max_total_tokens if args.max_total_tokens > 0 else None

    usage = UsageTracker()
    pcfg = PipelineConfig(
        output_dir=output_dir,
        encoding=args.encoding,
        language=args.language,
        max_cases=args.max_cases,
        batch_size=args.batch_size,
        max_chars=args.max_chars,
        sleep_after_call=args.sleep_after_call,
        sleep_between_files=args.sleep_between_files,
        max_total_tokens=max_total_tokens,
        export_formats=parse_export_formats(args.exports),
    )

    def _cli_progress(msg: str, frac: float) -> None:
        """控制台仅输出关键进度；其余见 log 文件。"""
        console.print(msg)

    console.print(
        f"[bold]任务[/bold] [cyan]{len(files)}[/cyan] 个文件 → [cyan]{output_dir}[/cyan]"
    )

    result = run_pipeline(
        files=files,
        cfg=cfg,
        llm=llm,
        config=pcfg,
        usage=usage,
        progress_callback=_cli_progress,
    )

    summary = Table(title="生成汇总")
    summary.add_column("来源", style="cyan")
    summary.add_column("输出文件", style="green")

    for o in result.outcomes:
        if o.ok and o.output_paths:
            summary.add_row(o.path.name, "\n".join([p.name for p in o.output_paths]))
        elif o.ok:
            summary.add_row(o.path.name, "（无输出路径）")

    for o in result.outcomes:
        if o.ok:
            continue
        name = o.path.name
        if o.error_kind == "budget":
            console.print(
                f"[red]已停止 {name}：累计用量超过 --max-total-tokens 上限。[/red]"
            )
        elif o.error_kind == "length":
            console.print(
                f"[red]跳过 {name}：模型输出长度超限。建议减小 --batch-size 或提高模型输出上限。[/red]"
            )
        elif o.error_kind == "connection":
            console.print(
                f"[red]跳过 {name}：网络连接异常。请检查网络/代理并重试。[/red]"
            )
        elif o.error_kind == "json":
            console.print(
                f"[red]跳过 {name}：模型返回格式异常。已写入 debug 文件，详见日志。[/red]"
            )
        elif o.error_kind == "auth":
            console.print(
                f"[red]跳过 {name}：API Key 无效或未授权。[/red]\n"
                "[yellow]通义千问请在阿里云百炼/Model Studio 创建 API Key，写入 DASHSCOPE_API_KEY；"
                "使用 DeepSeek 时设置 LLM_PROVIDER=deepseek 并配置 DEEPSEEK_API_KEY。"
                "若设置了 LLM_API_KEY，请确认其对应所选厂商。[/yellow]"
            )
        else:
            console.print(f"[red]跳过 {name}：处理失败（详细信息见日志）[/red]")

    u = result.usage
    logger.info(
        "LLM 用量汇总：调用=%d，Token总预估≈%d（%s），接口上报累计=%d，请求字符=%d，回复字符=%d",
        u.calls,
        u.estimated_tokens,
        u.token_estimate_source,
        u.total_tokens_reported,
        u.prompt_chars,
        u.completion_chars,
    )

    console.print(summary)
    if result.total_files:
        console.print(
            f"[bold]结果: 成功 {result.success_count}、失败 {result.fail_count}[/bold]"
        )
    console.print(
        f"[bold]Token 总预估 ≈ {u.estimated_tokens}[/bold] "
        f"[dim]（{u.token_estimate_source}，{u.calls} 次调用）[/dim]"
    )
    console.print(f"[bold green]完成。[/bold green] 输出已写入：{output_dir}")
    total_elapsed_min = (time.time() - start_ts) / 60.0
    console.print(f"[bold green]任务总耗时 {total_elapsed_min:.2f} 分钟[/bold green]")
    logger.info(
        "处理完成：总文件=%d，成功=%d，失败=%d，日志文件=%s",
        result.total_files,
        result.success_count,
        result.fail_count,
        log_file,
    )
    logger.info("任务总耗时：%.2f 分钟", total_elapsed_min)
    return 0


def _parse_args() -> argparse.Namespace:
    class ChineseArgumentParser(argparse.ArgumentParser):
        def format_help(self) -> str:
            text = super().format_help()
            return (
                text.replace("usage:", "用法：")
                .replace("options:", "选项：")
                .replace("show this help message and exit", "显示此帮助信息并退出")
            )

    p = ChineseArgumentParser(description="解析需求文档并生成测试点/测试用例。")
    p.add_argument("--input", default="input", help="输入目录路径（默认：input）")
    p.add_argument("--output", default="output", help="输出目录路径（默认：output）")
    p.add_argument("--language", default=None, help="输出语言：zh/en（覆盖 APP_LANGUAGE）")
    p.add_argument("--encoding", default="utf-8", help="文本/Markdown 编码（默认：utf-8）")
    p.add_argument("--max-cases", type=int, default=80, help="每个文档最多生成的测试用例数")
    p.add_argument("--batch-size", type=int, default=10, help="每次请求模型生成的用例数（建议更小以避免输出过长）")
    p.add_argument("--max-chars", type=int, default=15000, help="提取文本参与生成的最大字符数（超出会截断）")
    p.add_argument(
        "--sleep-after-call",
        type=float,
        default=0.0,
        help="每次 LLM 调用成功后休眠秒数，用于限流（默认 0）",
    )
    p.add_argument(
        "--sleep-between-files",
        type=float,
        default=0.0,
        help="处理完单个文件后、处理下一个文件前休眠秒数（默认 0）",
    )
    p.add_argument(
        "--max-total-tokens",
        type=int,
        default=0,
        help="单次任务累计 token 上限（估算：优先用接口返回，否则按字符/4）。0 表示不限制",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="将本项目日志（含关键步骤的 DEBUG/INFO）输出到控制台，便于排障；详细内容仍以 log/ 文件为准",
    )
    p.add_argument(
        "--llm-log-io",
        action="store_true",
        help="在日志中记录每次 LLM 调用的请求/响应摘要（长度与各段前若干字符，已做密钥形态遮蔽）；也可用环境变量 LLM_LOG_IO=1",
    )
    p.add_argument(
        "--exports",
        default="csv,zentao,testlink,jira",
        metavar="LIST",
        help="额外导出：逗号分隔 csv,zentao,testlink,jira（与 Excel 同源列映射）；none 表示不导出这些模板",
    )
    return p.parse_args()


def _env_bool(name: str) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


if __name__ == "__main__":
    raise SystemExit(main())
