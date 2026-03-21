from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path
import time

from rich.console import Console
from rich.table import Table

from .config import load_config
from .llm import (
    build_llm,
    generate_outline,
    generate_cases_batch,
    LLMConnectionError,
    LLMLengthLimitError,
    LLMJSONParseError,
)
from .parsers import iter_input_files, parse_document
from .models import GenerationResult
from .writers import write_outputs


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

    # 初始化日志：按时间命名的日志文件，写入项目根目录下的 log/ 目录
    log_dir = Path("log").resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    logger.info("已启动用例生成命令行")
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

    # 仅保留支持的文档类型（docx / md / txt）
    files = list(iter_input_files(input_dir))
    if not files:
        msg = f"输入目录中未找到支持的文档类型：{input_dir}"
        console.print(f"[yellow]{msg}[/yellow]")
        logger.warning(msg)
        return 0

    # 加载 DeepSeek 配置并构建 LangChain ChatOpenAI 实例
    logger.info("正在加载配置并初始化 LLM 客户端")
    cfg = load_config(override_language=args.language)
    llm = build_llm(cfg)
    logger.info("已使用模型：provider=%s，model=%s", cfg.provider, cfg.model)

    summary = Table(title="生成汇总")
    summary.add_column("来源", style="cyan")
    summary.add_column("输出文件", style="green")

    total_files = len(files)
    success_count = 0
    fail_count = 0

    for i, path in enumerate(files, 1):
        try:
            file_start_ts = time.time()
            logger.info("正在处理文件 %d/%d：%s", i, total_files, path)
            console.print(f"[{i}/{total_files}] [bold]解析[/bold] {path.name}")
            parsed = parse_document(path, encoding=args.encoding)

            # 简单保护：超大文本截断（避免 token 过长导致接口报错）
            text = parsed.text
            if len(text) > args.max_chars:
                logger.info(
                    "文本长度 %d 超出最大字符数 %d，已截断",
                    len(text),
                    args.max_chars,
                )
                text = text[: args.max_chars]
                console.print(f"[yellow]文档已截断至 {args.max_chars} 字[/yellow]")

            logger.info("正在为 %s 生成大纲", path.name)
            console.print(
                f"[bold]生成[/bold]大纲（{cfg.provider}：{cfg.model}）"
            )
            # 第一步：让 LLM 基于整篇文档，生成摘要、测试点列表和思维导图
            outline = generate_outline(
                cfg=cfg,
                llm=llm,
                source_name=path.name,
                document_text=text,
            )

            # 第二步：按测试点分批生成用例，避免单次输出过长被截断
            logger.info(
                "正在分批生成测试用例：目标数量=%d，每批=%d",
                args.max_cases,
                args.batch_size,
            )
            console.print(f"[bold]分批生成[/bold]测试用例（目标={args.max_cases}，每批={args.batch_size}）")
            all_cases: list = []
            existing_titles: list[str] = []
            seen_titles: set[str] = set()
            total_candidates = 0
            discarded_duplicates = 0
            consecutive_zero_new_batches = 0
            tp_idx = 0
            total_batches = 0

            # 兜底：避免在模型持续重复时无限请求
            max_batches_limit = max(3, (args.max_cases // max(1, args.batch_size)) * 3)
            max_consecutive_zero_new = 5

            # 轮询测试点，直到达到目标用例数或触发兜底条件
            while (
                len(all_cases) < args.max_cases
                and outline.test_points
                and total_batches < max_batches_limit
            ):
                test_point = outline.test_points[tp_idx % len(outline.test_points)]
                tp_idx += 1
                remaining = args.max_cases - len(all_cases)
                batch_size = min(args.batch_size, remaining)
                total_batches += 1

                # 打印当前批次的进度信息，便于观察长时间运行情况
                console.print(
                    f"- [cyan]批次[/cyan] {len(all_cases)} -> {len(all_cases) + batch_size} "
                    f"（测试点：[magenta]{test_point[:30]}[/magenta]）..."
                )
                # 每一批只围绕一个测试点，向模型请求若干条用例
                logger.info(
                    "请求第 %d 批用例：当前数量=%d，预计数量=%d，测试点=%.30s",
                    (len(all_cases) // batch_size) + 1,
                    len(all_cases),
                    len(all_cases) + batch_size,
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
                )
                new_count = 0
                batch_candidates = len(batch.test_cases or [])
                total_candidates += batch_candidates
                for tc in batch.test_cases:
                    title_key = (tc.title or "").strip()
                    # 若标题为空，则用 ID 作为兜底去重键，避免丢失导致死循环
                    key = title_key if title_key else f"ID:{tc.id}".strip()

                    if key in seen_titles:
                        discarded_duplicates += 1
                        continue

                    seen_titles.add(key)
                    all_cases.append(tc)
                    if title_key:
                        existing_titles.append(title_key)
                    new_count += 1
                    if len(all_cases) >= args.max_cases:
                        break
                console.print(
                    f"  → 本批新增 [green]{new_count}[/green] 条，累计 [bold]{len(all_cases)}[/bold] 条"
                )
                logger.info(
                    "批次已完成：本批新增=%d，累计数量=%d，已累计候选=%d",
                    new_count,
                    len(all_cases),
                    total_candidates,
                )
                if new_count == 0:
                    consecutive_zero_new_batches += 1
                    if consecutive_zero_new_batches >= max_consecutive_zero_new:
                        logger.warning(
                            "触发兜底：连续 %d 批新增为 0（已选 %d/%d 条），停止继续生成。候选总数=%d，去重丢弃=%d",
                            max_consecutive_zero_new,
                            len(all_cases),
                            args.max_cases,
                            total_candidates,
                            discarded_duplicates,
                        )
                        break
                else:
                    consecutive_zero_new_batches = 0
            if len(all_cases) < args.max_cases:
                console.print(
                    f"[yellow]未达到目标条数：目标 {args.max_cases}，实际 {len(all_cases)}[/yellow]"
                )
                logger.warning(
                    "未达到目标条数：目标=%d，实际=%d，总批次=%d，候选总数=%d，去重丢弃=%d",
                    args.max_cases,
                    len(all_cases),
                    total_batches,
                    total_candidates,
                    discarded_duplicates,
                )
            else:
                console.print(f"[green]已达到目标条数：{len(all_cases)} 条[/green]")
            logger.info(
                "用例收集完成：已选择=%d/%d，候选总数=%d，去重丢弃=%d，总批次=%d",
                len(all_cases),
                args.max_cases,
                total_candidates,
                discarded_duplicates,
                total_batches,
            )

            # 组装成统一的 GenerationResult 对象，便于 writers 统一输出
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

            logger.info("正在把结果写入：%s -> %s", path.name, output_dir)
            out_paths = write_outputs(result, output_dir)
            file_elapsed_min = (time.time() - file_start_ts) / 60.0
            console.print(f"[bold green]文档处理完成，用时 {file_elapsed_min:.2f} 分钟[/bold green]")
            logger.info(
                "文档处理完成：source=%s，用时=%.2f 分钟",
                path.name,
                file_elapsed_min,
            )
            summary.add_row(path.name, "\n".join([p.name for p in out_paths]))
            success_count += 1

        except Exception as e:
            fail_count += 1
            logger.exception("处理失败：%s", path)
            if isinstance(e, LLMLengthLimitError):
                console.print(
                    f"[red]跳过 {path.name}：模型输出长度超限。建议减小 --batch-size 或提高模型输出上限。[/red]"
                )
            elif isinstance(e, LLMConnectionError):
                console.print(
                    f"[red]跳过 {path.name}：网络连接异常。请检查网络/代理并重试。[/red]"
                )
            elif isinstance(e, LLMJSONParseError):
                console.print(
                    f"[red]跳过 {path.name}：模型返回格式异常。已写入 debug 文件，详见日志。[/red]"
                )
            else:
                console.print(f"[red]跳过 {path.name}：处理失败（详细信息见日志）[/red]")

    console.print(summary)
    if total_files:
        console.print(f"[bold]结果: 成功 {success_count}、失败 {fail_count}[/bold]")
    console.print(f"[bold green]完成。[/bold green] 输出已写入：{output_dir}")
    total_elapsed_min = (time.time() - start_ts) / 60.0
    console.print(f"[bold green]任务总耗时 {total_elapsed_min:.2f} 分钟[/bold green]")
    logger.info(
        "处理完成：总文件=%d，成功=%d，失败=%d，日志文件=%s",
        total_files,
        success_count,
        fail_count,
        log_file,
    )
    logger.info("任务总耗时：%.2f 分钟", total_elapsed_min)
    return 0


def _parse_args() -> argparse.Namespace:
    """
    解析命令行参数，支持：输入输出目录、语言、编码、用例数量、批大小、最大文档长度等。
    """
    class ChineseArgumentParser(argparse.ArgumentParser):
        def format_help(self) -> str:
            text = super().format_help()
            # argparse 默认输出包含少量英文标签；这里做统一替换，避免“英文提示”。
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
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

