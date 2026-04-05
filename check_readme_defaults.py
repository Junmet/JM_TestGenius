"""
检查 README 中记录的 CLI 默认值是否与代码实现一致。

当前主要校验：
- --max-cases
- --batch-size
- --max-chars

用法：
    python check_readme_defaults.py
"""
from __future__ import annotations

from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def extract_readme_defaults() -> dict[str, str]:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    # 在参数表中查找三行关键参数的默认值
    defaults: dict[str, str] = {}
    pattern = re.compile(
        r"`(--max-cases|--batch-size|--max-chars)`\s*\|\s*`?([0-9]+)`?",
        re.IGNORECASE,
    )
    for m in pattern.finditer(readme):
        key, val = m.group(1), m.group(2)
        defaults[key] = val
    return defaults


def extract_argparse_defaults() -> dict[str, str]:
    from src.main import _parse_args  # type: ignore[attr-defined]

    # _parse_args() 内部直接使用 argparse.parse_args()，读取 sys.argv，
    # 因为本脚本自身不会传入额外参数，所以默认值会保持一致。
    args = _parse_args()
    return {
        "--max-cases": str(args.max_cases),
        "--batch-size": str(args.batch_size),
        "--max-chars": str(args.max_chars),
    }


def main() -> int:
    readme_vals = extract_readme_defaults()
    code_vals = extract_argparse_defaults()

    missing = [k for k in code_vals if k not in readme_vals]
    mismatches = [
        (k, readme_vals.get(k), code_vals[k])
        for k in code_vals
        if readme_vals.get(k) is not None and readme_vals[k] != code_vals[k]
    ]

    ok = True
    if missing:
        print("README 缺少以下参数默认值条目:", ", ".join(missing))
        ok = False
    if mismatches:
        for k, rv, cv in mismatches:
            print(f"默认值不一致：{k} README={rv} 代码={cv}")
        ok = False

    if ok:
        print("README 中的关键默认参数与代码一致。")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

