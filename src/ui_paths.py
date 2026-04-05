"""在桌面环境中尝试用系统文件管理器打开目录（Streamlit / 本地工具复用）。"""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path


def reveal_path_in_os(path: Path) -> tuple[bool, str]:
    """
    尝试在访达 / 资源管理器 / xdg-open 中打开 path。
    返回 (是否已发起命令, 说明文案)。
    """
    p = path.resolve()
    if not p.exists():
        return False, f"路径不存在：{p}"
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, "已在访达中打开"
        if system == "Windows":
            subprocess.Popen(["explorer", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, "已尝试在资源管理器中打开"
        subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, "已尝试用 xdg-open 打开"
    except Exception as e:  # noqa: BLE001
        return False, str(e)
