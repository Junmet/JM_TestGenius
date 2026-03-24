from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

_FMT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
_TAG = "_jm_testgenius_mark"


def setup_generation_logging(*, stream: bool = False) -> Path:
    """
    为一次「生成任务」配置根日志：
    - 始终写入项目根目录下 log/ 中带时间戳的 .log 文件（与 CLI 行为一致）；
    - stream=True 时额外输出到 stderr，便于 `streamlit run` 的终端看到日志。

    在同一进程内重复调用（例如 Streamlit 再次点击「开始生成」）会先移除此前挂接的
    同项目 FileHandler / StreamHandler，避免重复写入或刷屏。
    """
    log_dir = Path("log").resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _TAG, None) in ("file", "stream"):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    fmt = logging.Formatter(_FMT)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    setattr(fh, _TAG, "file")
    root.addHandler(fh)

    if stream:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)
        setattr(sh, _TAG, "stream")
        root.addHandler(sh)

    root.setLevel(logging.INFO)
    # 第三方 HTTP 客户端默认 INFO 会刷屏；细节如需可改为 DEBUG
    for name in ("httpx", "httpcore", "openai", "httpcore.connection"):
        logging.getLogger(name).setLevel(logging.WARNING)

    return log_file
