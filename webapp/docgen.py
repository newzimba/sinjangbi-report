"""F8 워드(.docx) 1p 생성 · F6 페이지 수 검사 (MS Word 자동화).

scripts/make_report.py의 서식 도우미를 재사용한다.
"""
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.image.image import Image as DocxImage
from docx.shared import Mm, Pt, RGBColor

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from make_report import cell_text, domain, fixed_widths, new_doc, para, set_font  # noqa: E402

JUDGEMENTS = ["권고", "보류", "불필요"]
IMG_MAX_W, IMG_MAX_H = 80, 45  # mm — 1장 안에 들어가도록 사진 크기 제한


def safe_filename(s):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s or "").strip("_")[:40] or "report"


def file_name(week, name):
    return f"{week}_1p_{safe_filename(name)}.docx"


def heading(doc, text):
    para(doc, "□ " + text, size=11, bold=True, space_after=1)


MARK_RE = re.compile(r"\s*\[\d+\]")


def plain(text):
    """1p에는 출처를 표시하지 않는다 (2026-11-18 사용자 요청): 문장 끝 [n] 출처 번호를 지운다."""
    return MARK_RE.sub("", text or "").strip()


def bullets(doc, items, mark="○"):
    for it in items or []:
        if plain(it):
            para(doc, f"{mark} {plain(it)}", size=10, indent_mm=4, space_after=1)


def add_picture(paragraph, path, max_w=IMG_MAX_W, max_h=IMG_MAX_H):
    """비율을 유지하며 최대 가로·세로 안에 맞춰 넣는다."""
    img = DocxImage.from_file(path)
    ratio = img.px_height / max(img.px_width, 1)
    run = paragraph.add_run()
    if max_w * ratio > max_h:
        run.add_picture(path, height=Mm(max_h))
    else:
        run.add_picture(path, width=Mm(max_w))


CREDIT_RE = re.compile(r"^(.*?),?\s*(https?://\S+)\s*$")


def split_credit(credit):
    """'출처: 기관, https://…' → ('출처: 기관', 'https://…'). URL이 없으면 (원문, None)."""
    m = CREDIT_RE.match((credit or "").strip())
    if not m:
        return (credit or "출처: 확인 필요"), None
    label = m.group(1).strip().rstrip(",") or f"출처: {domain(m.group(2))}"
    return label, m.group(2)


NO_PHOTO = "사진 없음"


def no_photo_cell(cell, height_mm):
    """사진 자리에 '사진 없음'을 표시한다 (회색 칸). 임의의 사진을 넣지 않는다."""
    cell_text(cell, NO_PHOTO, size=9.5, fill="F2F2F2")
    p = cell.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(height_mm)
    p.paragraph_format.space_after = Pt(height_mm)
    for r in p.runs:
        r.font.color.rgb = RGBColor(0x88, 0x88, 0x88)


def add_photos(doc, photos, slots=2):
    """사진 2칸을 나란히 둔다. 장비 사진이 없는 칸은 '사진 없음'으로 표시한다.
    사진이 하나도 없으면 한 줄짜리 '사진 없음' 칸만 둔다. 1p에는 사진 출처를 표시하지 않는다(앱에만 보관)."""
    if not photos:
        t = doc.add_table(rows=1, cols=1)
        t.style = "Table Grid"
        fixed_widths(t, [174])
        no_photo_cell(t.cell(0, 0), 6)
        return
    t = doc.add_table(rows=1, cols=slots)
    fixed_widths(t, [174 / slots] * slots)
    for i in range(slots):
        cell = t.cell(0, i)
        if i < len(photos):
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_after = Pt(0)
            add_picture(p, photos[i]["file"], max_w=82, max_h=45)
        else:
            no_photo_cell(cell, 40)


def build_docx(d, week, out_path):
    doc = new_doc()
    para(doc, "해외 도로 유지관리 신장비 동향", size=15, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=0)
    para(doc, d.get("name", ""), size=12, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2, color="1F4E79")
    para(doc, f"{week}  |  첨단기계화센터  |  {date.today():%Y.%m.%d}",
         size=8.5, align=WD_ALIGN_PARAGRAPH.RIGHT, space_after=3, color="666666")

    heading(doc, "개요")
    t = doc.add_table(rows=2, cols=4)
    t.style = "Table Grid"
    for i, (a, b, c, e) in enumerate([("장비명", plain(d.get("name")), "제조사", plain(d.get("maker"))),
                                      ("국가", plain(d.get("country")), "공개 시기", plain(d.get("released")))]):
        cell_text(t.cell(i, 0), a, bold=True, fill="DCE6F1")
        cell_text(t.cell(i, 1), b)
        cell_text(t.cell(i, 2), c, bold=True, fill="DCE6F1")
        cell_text(t.cell(i, 3), e)
    fixed_widths(t, (22, 62, 22, 68))
    para(doc, "", space_after=2)

    heading(doc, "장비 사진")
    photos = [p for p in d.get("images", []) if p.get("file") and Path(p["file"]).exists()]
    add_photos(doc, photos)
    para(doc, "", space_after=2)

    heading(doc, "주요 기능·제원")
    bullets(doc, d.get("functions"))
    bullets(doc, d.get("specs"), mark="-")

    heading(doc, "기존 방식 대비 개선점")
    imp = d.get("improvements") or {}
    bullets(doc, [f"{label}: {imp.get(k)}" for k, label in (("safety", "안전"), ("efficiency", "효율"), ("cost", "비용"))
                  if imp.get(k)])

    heading(doc, "국내 적용성")
    bullets(doc, d.get("domestic"))

    j = d.get("judgement")
    marks = "   ".join(("■ " if j == x else "□ ") + x for x in JUDGEMENTS)
    p = para(doc, "□ 벤치마킹 판단:   ", size=11, bold=True, space_after=1)
    set_font(p.add_run(marks), 11, bold=True, color="C00000")
    bullets(doc, d.get("reasons"))
    # 참고 자료(출처 목록)는 1p에 넣지 않는다 — 출처는 앱 편집 화면에만 보관 (원문 대조·교차 검증용)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    return out_path


PS_COUNT = r"""
$ErrorActionPreference = 'Stop'
$w = New-Object -ComObject Word.Application
try {
  $w.Visible = $false; $w.DisplayAlerts = 0
  $d = $w.Documents.Open('{path}', $false, $true)
  $n = $d.ComputeStatistics(2); $d.Close($false); Write-Output $n
} finally { $w.Quit() }
"""


def count_pages(path, timeout=90):
    """MS Word로 페이지 수를 센다. Word가 없거나 실패하면 None."""
    if sys.platform != "win32":
        return None
    script = PS_COUNT.replace("{path}", str(Path(path).resolve()).replace("'", "''"))
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=timeout)
        return int(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 and r.stdout.strip() else None
    except (subprocess.TimeoutExpired, ValueError, IndexError, OSError):
        return None
