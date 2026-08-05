from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path

import fitz
from openpyxl import Workbook


ROOT_DIR = Path(__file__).resolve().parents[1]
FIXTURES_DIR = ROOT_DIR / "evals" / "fixtures"


CJK_FONT = "china-s"  # PyMuPDF 内置简体中文字体，insert_text 必须显式指定


def _insert_cjk(page, point, text, fontsize=12):
    page.insert_text(point, text, fontsize=fontsize, fontname=CJK_FONT)


def gen_cross_page_meeting_room_pdf(path: Path) -> None:
    """两页会议室预订审批流程，步骤 3 在一页末尾、步骤 4 在下一页开头。

    主题刻意避开 demo 语料（请假、合同、报销），保证扩展夹具可独立召回。
    """
    document = fitz.open()
    page = document.new_page(width=420, height=560)
    _insert_cjk(page, (40, 40), "会议室预订审批流程（一）", fontsize=16)
    steps = [
        "步骤 1：员工在 OA 系统提交会议室预订申请。",
        "步骤 2：行政部确认会议室资源与时间。",
        "步骤 3：行政部审批通过并锁定时段。",
        "步骤 4：财务部按部门分摊会议室费用。",
        "步骤 5：门禁系统同步授权参会人员。",
        "步骤 6：系统自动发送预订确认通知。",
    ]
    for index, step in enumerate(steps):
        y = 90 + index * 70
        _insert_cjk(page, (40, y), step)
        if y > 500:
            page = document.new_page(width=420, height=560)
            _insert_cjk(page, (40, 40), "会议室预订审批流程（二）", fontsize=16)
            _insert_cjk(page, (40, 90), step)
            break
    document.save(path)
    document.close()


def gen_device_config_table_pdf(path: Path) -> None:
    """含办公设备配置标准表格的 PDF，研发岗和行政岗两档配置。"""
    document = fitz.open()
    page = document.new_page(width=520, height=340)
    _insert_cjk(page, (40, 40), "办公设备配置标准", fontsize=16)
    _insert_cjk(page, (40, 70), "不同岗位的办公设备配置如下表所示：")
    for y in (100, 150, 200, 250, 300):
        page.draw_line((35, y), (485, y))
    for x, value in zip((45, 155, 255, 355, 445), ("岗位", "笔记本电脑", "内存", "显示器", "价格上限")):
        _insert_cjk(page, (x, 130), value)
    for x, value in zip((45, 155, 255, 355, 445), ("研发岗", "高性能笔记本", "32GB", "27 英寸", "15000 元")):
        _insert_cjk(page, (x, 180), value)
    for x, value in zip((45, 155, 255, 355, 445), ("行政岗", "标准笔记本", "16GB", "24 英寸", "8000 元")):
        _insert_cjk(page, (x, 230), value)
    document.save(path)
    document.close()


def gen_department_budget_xlsx(path: Path) -> None:
    """预算 Sheet，研发部第二季度预算为 120 万元。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "预算"
    sheet.append(["部门", "第一季度", "第二季度", "第三季度", "第四季度"])
    sheet.append(["研发部", 100, 120, 130, 140])
    sheet.append(["销售部", 80, 85, 90, 95])
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    path.write_bytes(buffer.getvalue())


def main() -> int:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    gen_cross_page_meeting_room_pdf(
        FIXTURES_DIR / "p0_1_cross_page_meeting_room.pdf"
    )
    gen_device_config_table_pdf(
        FIXTURES_DIR / "p0_1_device_config_table.pdf"
    )
    gen_department_budget_xlsx(
        FIXTURES_DIR / "p0_1_department_budget.xlsx"
    )
    print(f"P0-1 fixtures generated in {FIXTURES_DIR}")
    for path in sorted(FIXTURES_DIR.iterdir()):
        print(f"  {path.name} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
