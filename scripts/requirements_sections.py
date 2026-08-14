"""从 requirements.txt 按节提取依赖清单。

用法（在项目根目录执行）：
    python scripts/requirements_sections.py [section ...]

输出：每行一个依赖项，供 `pip install` 直接使用。指定节以外的依赖不输出。

节标记规则（见 requirements.txt 头部注释）：
    pkg==1.0  #@ base,desktop   -> 属于 base 与 desktop 节
    无标记的行默认归入 base。

示例：
    # 只装桌面运行时（含 base 共享依赖）
    pip install $(python scripts/requirements_sections.py desktop)
    # 服务端 Web + RAG 重模型
    pip install $(python scripts/requirements_sections.py server ml)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"

SECTION_MARK = re.compile(r"#@\s+([\w,]+)")
LINE_CLEAN = re.compile(r"\s*#@.*$")


def main() -> int:
    requested = {section.strip() for section in sys.argv[1:] if section.strip()}
    # 仅请求 build（增量补打包工具）时不带 base；否则 base 是共享运行时，始终包含。
    only_build = bool(requested) and requested <= {"build"}
    if not REQUIREMENTS.exists():
        print(f"requirements.txt 不存在：{REQUIREMENTS}", file=sys.stderr)
        return 2

    packages: list[str] = []
    with REQUIREMENTS.open("r", encoding="utf-8") as reader:
        for raw_line in reader:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            mark = SECTION_MARK.search(raw_line)
            sections = (
                {item.strip() for item in mark.group(1).split(",")}
                if mark
                else set()
            )
            if sections:
                if "base" in sections:
                    # base 是共享运行时：仅 build 增量时不带，其余情况总包含
                    if only_build:
                        continue
                elif not (sections & requested):
                    continue
            elif only_build:
                # 无标记（按 base 处理）：仅 build 时不输出
                continue
            package = LINE_CLEAN.sub("", raw_line).strip()
            if package:
                packages.append(package)

    if not packages:
        print(f"未找到匹配的依赖（节：{sorted(requested) or ['base']}）", file=sys.stderr)
        return 1
    print("\n".join(packages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
