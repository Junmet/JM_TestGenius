from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
CONFIG = ROOT / "src" / "config.py"


def _extract_config_defaults(config_text: str) -> dict[str, str]:
    pairs = {
        "DEEPSEEK_TIMEOUT": r'DEEPSEEK_TIMEOUT",\s*"(\d+)"',
        "DEEPSEEK_MAX_TOKENS": r'DEEPSEEK_MAX_TOKENS",\s*"(\d+)"',
    }
    defaults: dict[str, str] = {}
    for key, pattern in pairs.items():
        m = re.search(pattern, config_text)
        if not m:
            raise RuntimeError(f"未在 config.py 中找到 {key} 默认值。")
        defaults[key] = m.group(1)
    return defaults


def _extract_readme_defaults(readme_text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in readme_text.splitlines():
        line = line.strip()
        # 仅解析配置表中的行：| `DEEPSEEK_TIMEOUT` | ... | `120` |
        m = re.match(r"\|\s*`(DEEPSEEK_[A-Z_]+)`\s*\|.*\|\s*`([^`]+)`\s*\|", line)
        if m:
            result[m.group(1)] = m.group(2).strip()
    return result


def main() -> int:
    readme_text = README.read_text(encoding="utf-8")
    config_text = CONFIG.read_text(encoding="utf-8")

    config_defaults = _extract_config_defaults(config_text)
    readme_defaults = _extract_readme_defaults(readme_text)

    mismatches: list[str] = []
    for key, value in config_defaults.items():
        readme_value = readme_defaults.get(key)
        if readme_value is None:
            mismatches.append(f"README 缺少配置项：{key}")
            continue
        if readme_value != value:
            mismatches.append(
                f"{key} 默认值不一致：README={readme_value}，代码={value}"
            )

    if mismatches:
        print("README 默认值检查失败：")
        for item in mismatches:
            print(f"- {item}")
        return 1

    print("README 默认值检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
