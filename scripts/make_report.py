"""주간 신장비 조사 결과(JSON)를 1p 보고서 초안과 후보 목록 문서(.docx)로 만들고 보고 이력에 추가한다.

사용법:
    python make_report.py <조사결과.json>

.docx 파일은 한글(2020 이상)에서 바로 열리며, [다른 이름으로 저장 → .hwp]로 변환할 수 있다.
"""
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "data" / "history.csv"
HISTORY_HEADER = ["조사주차", "장비명", "제조사", "국가", "분류", "출처URL", "상태"]
FONT = "맑은 고딕"
MAX_SOURCES = 3  # 1p에 넣는 참고 자료 수 (나머지는 후보목록·JSON에 남음)


# ---------- 서식 도우미 ----------
def set_font(run, size=10, bold=False, color=None):
    run.font.name = FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    run.font.size = Pt(size)
    run.font.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)


def para(doc_or_cell, text="", size=10, bold=False, align=None, space_after=2, indent_mm=0, color=None):
    p = doc_or_cell.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(space_after)
    pf.line_spacing = 1.08
    if indent_mm:
        pf.left_indent = Mm(indent_mm)
    if align:
        p.alignment = align
    if text:
        set_font(p.add_run(text), size, bold, color)
    return p


def shade(cell, hex_color):
    tcPr = cell._element.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)


def cell_text(cell, text, size=9.5, bold=False, fill=None):
    cell.text = ""
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(0)
    set_font(p.add_run(text or "-"), size, bold)
    if fill:
        shade(cell, fill)


def add_link(p, text, url, size=8, color="1F4E79"):
    """문단에 클릭 가능한 하이퍼링크를 추가한다."""
    if not url:
        set_font(p.add_run(text or "-"), size)
        return
    r_id = p.part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
                            is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    for tag, val in (("w:rFonts", None), ("w:color", color), ("w:u", "single"), ("w:sz", str(int(size * 2)))):
        el = OxmlElement(tag)
        if tag == "w:rFonts":
            el.set(qn("w:ascii"), FONT)
            el.set(qn("w:hAnsi"), FONT)
            el.set(qn("w:eastAsia"), FONT)
        else:
            el.set(qn("w:val"), val)
        rpr.append(el)
    run.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    run.append(t)
    link.append(run)
    p._p.append(link)


def domain(url):
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1) if m else (url or "-")


def fixed_widths(table, widths_mm):
    """표 열 너비를 고정한다 (Word·한글 모두 적용)."""
    table.autofit = False
    tblPr = table._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblPr.append(layout)
    for j, w in enumerate(widths_mm):
        table.columns[j].width = Mm(w)
        for cell in table.columns[j].cells:
            cell.width = Mm(w)


def new_doc():
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    sec.left_margin = sec.right_margin = Mm(18)
    sec.top_margin = sec.bottom_margin = Mm(15)
    style = doc.styles["Normal"]
    style.font.name = FONT
    style.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    style.font.size = Pt(10)
    # 기본 빈 문단 제거
    for p in list(doc.paragraphs):
        p._element.getparent().remove(p._element)
    return doc


def heading(doc, text):
    para(doc, "□ " + text, size=11, bold=True, space_after=1)


def bullets(doc, items, mark="○"):
    for it in items or []:
        para(doc, f"{mark} {it}", size=10, indent_mm=4, space_after=1)


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip()[:40]


# ---------- 1p 보고서 ----------
def build_onepager(r, week, out_dir):
    doc = new_doc()
    para(doc, "해외 도로 유지관리 신장비 동향", size=15, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=0)
    para(doc, r["장비명"], size=12, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2, color="1F4E79")
    para(doc, f"{week}  |  첨단기계화센터  |  작성일 {date.today():%Y.%m.%d}  |  AI 초안(검토 필요)",
         size=8.5, align=WD_ALIGN_PARAGRAPH.RIGHT, space_after=4, color="666666")

    # 1. 개요 표
    heading(doc, "개요")
    t = doc.add_table(rows=2, cols=4)
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    rows = [("장비명", r.get("장비명"), "제조사", r.get("제조사")),
            ("국가", r.get("국가"), "공개 시기", r.get("공개시기"))]
    for i, (a, b, c, d) in enumerate(rows):
        cell_text(t.cell(i, 0), a, bold=True, fill="DCE6F1")
        cell_text(t.cell(i, 1), b)
        cell_text(t.cell(i, 2), c, bold=True, fill="DCE6F1")
        cell_text(t.cell(i, 3), d)
    fixed_widths(t, (22, 62, 22, 68))
    para(doc, f"  분류: {r.get('분류', '-')}", size=9, space_after=3, color="444444")

    # 2. 장비 사진 자리
    heading(doc, "장비 사진")
    pt = doc.add_table(rows=1, cols=1)
    pt.style = "Table Grid"
    fixed_widths(pt, (174,))
    c = pt.cell(0, 0)
    cell_text(c, "[사진 삽입 영역]  아래 이미지 출처에서 사진을 받아 붙여넣으세요 (출처 표기).", size=8.5, fill="F2F2F2")
    if r.get("이미지출처"):
        p = c.add_paragraph()
        set_font(p.add_run("이미지 출처: "), 8)
        add_link(p, domain(r["이미지출처"]) + " (클릭)", r["이미지출처"], size=8)
    c.paragraphs[0].paragraph_format.space_before = Pt(18)
    c.paragraphs[-1].paragraph_format.space_after = Pt(18)
    para(doc, "", space_after=2)

    heading(doc, "주요 기능·제원")
    bullets(doc, r.get("주요기능"))
    if r.get("제원"):
        bullets(doc, r["제원"], mark="-")

    heading(doc, "기존 방식 대비 개선점")
    bullets(doc, r.get("개선점"))

    heading(doc, "국내 적용성")
    bullets(doc, r.get("국내적용성"))

    heading(doc, "시사점 및 향후 조치")
    bullets(doc, r.get("시사점"))
    if r.get("향후조치"):
        para(doc, f"☞ 권고: {r['향후조치']}", size=10, bold=True, indent_mm=4, space_after=3, color="C00000")

    para(doc, "참고 자료", size=8, bold=True, space_after=0, color="666666")
    for s in r.get("출처", [])[:MAX_SOURCES]:
        p = para(doc, "· ", size=8, space_after=0, color="666666")
        add_link(p, f"{s.get('title', '')} ({domain(s.get('url'))})", s.get("url"), size=8, color="555555")
        p.paragraph_format.line_spacing = 1.0

    path = out_dir / f"{week}_1p_{safe_name(r['장비명'])}.docx"
    doc.save(path)
    return path


# ---------- 후보 목록 ----------
def build_candidate_list(data, out_dir):
    week = data["week"]
    doc = new_doc()
    doc.sections[0].orientation = 1
    doc.sections[0].page_width, doc.sections[0].page_height = Mm(297), Mm(210)
    para(doc, f"해외 유지관리 신장비 후보 목록 ({week})", size=14, bold=True, space_after=2)
    para(doc, f"조사 기간: {data.get('period', '-')}  |  후보 {len(data['candidates'])}건  |  "
              f"1p 초안 {len(data.get('reports', []))}건  |  AI 자동 조사 결과(검토 필요)",
         size=9, space_after=6, color="666666")
    if data.get("summary"):
        para(doc, "이번 주 요약", size=11, bold=True, space_after=1)
        bullets(doc, data["summary"])
        para(doc, "", space_after=2)

    cols = ["No", "장비명", "제조사(국가)", "분류", "한 줄 요약", "신규성 근거", "출처"]
    widths = [9, 46, 38, 18, 72, 52, 26]
    t = doc.add_table(rows=1, cols=len(cols))
    t.style = "Table Grid"
    for j, h in enumerate(cols):
        cell_text(t.rows[0].cells[j], h, size=9, bold=True, fill="DCE6F1")
    for i, cnd in enumerate(data["candidates"], 1):
        cells = t.add_row().cells
        star = " ★" if cnd.get("1p") else ""
        vals = [str(i), cnd["장비명"] + star, f"{cnd.get('제조사', '-')} ({cnd.get('국가', '-')})",
                cnd.get("분류"), cnd.get("요약"), cnd.get("신규성")]
        for j, v in enumerate(vals):
            cell_text(cells[j], v, size=8.5)
        cells[6].text = ""
        add_link(cells[6].paragraphs[0], domain(cnd.get("url")), cnd.get("url"), size=8)
    fixed_widths(t, widths)
    para(doc, "★ = 1p 보고서 초안 작성 대상", size=8.5, space_after=0, color="666666")

    path = out_dir / f"{week}_신장비_후보목록.docx"
    doc.save(path)
    return path


# ---------- 이력 ----------
def append_history(data):
    exists = HISTORY.exists() and HISTORY.stat().st_size > 0
    with open(HISTORY, "a", newline="", encoding="utf-8-sig" if not exists else "utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(HISTORY_HEADER)
        for c in data["candidates"]:
            w.writerow([data["week"], c["장비명"], c.get("제조사", ""), c.get("국가", ""),
                        c.get("분류", ""), c.get("url", ""), "1p초안" if c.get("1p") else "후보"])


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    src = Path(sys.argv[1])
    data = json.loads(src.read_text(encoding="utf-8"))
    out_dir = src.parent
    made = [build_candidate_list(data, out_dir)]
    for r in data.get("reports", []):
        made.append(build_onepager(r, data["week"], out_dir))
    append_history(data)
    for p in made:
        print("생성:", p)
    print("이력 추가:", HISTORY, f"({len(data['candidates'])}건)")


if __name__ == "__main__":
    main()
