"""순수 함수 단위 테스트: F2 출처 필터, F3 출처 번호 정리, F4 이미지 판별, F8 워드 생성."""
import httpx
import pytest
from docx import Document

from conftest import fake_report_data, make_png
from webapp import docgen, images, report_gen, research

SEEN = {"maker.com", "roadsbridges.com", "dot.gov"}
BLOCKED = ["youtube.com", "blog.naver.com"]


def cand(name, *urls):
    return {"name": name, "relevance": 8, "in_period": True,
            "sources": [{"title": u, "url": u, "type": "전문 언론 기사"} for u in urls]}


# ---------- F2 출처 필터 ----------
def test_f2_정상_조사중_본_도메인은_유지():
    out, dropped = research.verify_candidates([cand("A", "https://www.roadsbridges.com/x")], SEEN, BLOCKED, [], 3)
    assert [c["name"] for c in out] == ["A"] and out[0]["id"] == "c1" and not dropped


def test_f2_예외_중복_URL은_하나로():
    out, _ = research.verify_candidates([cand("A", "https://maker.com/a", "https://maker.com/a/")], SEEN, BLOCKED, [], 3)
    assert len(out[0]["sources"]) == 1


def test_f2_오류_유튜브와_지어낸_도메인은_제거되고_후보도_제외():
    raw = [cand("A", "https://www.youtube.com/watch?v=1"), cand("B", "https://fake-site.example/x"),
           cand("C", "https://maker.com/c", "https://youtube.com/x")]
    out, dropped = research.verify_candidates(raw, SEEN, BLOCKED, [], 3)
    assert [c["name"] for c in out] == ["C"]
    assert len(out[0]["sources"]) == 1
    assert len(dropped) == 2 and all("출처가 없음" in d for d in dropped)


def test_f2_오류_보고완료_장비는_제외_이름표기차이_무시():
    out, dropped = research.verify_candidates([cand("ATMA (Kratos)", "https://maker.com/a")], SEEN, BLOCKED,
                                              ["atma(kratos)"], 3)
    assert out == [] and "이미 보고" in dropped[0]


def test_f1_후보개수_제한():
    raw = [cand(f"A{i}", "https://maker.com/a") for i in range(6)]
    out, _ = research.verify_candidates(raw, SEEN, BLOCKED, [], 3)
    assert [c["id"] for c in out] == ["c1", "c2", "c3"]


# ---------- F3 / F7 출처 번호 ----------
def test_f7_출처가_빠지면_번호를_다시_매기고_확인필요에_추가():
    d = fake_report_data()
    d["sources"].insert(0, {"title": "유튜브", "url": "https://youtube.com/watch?v=1"})  # [1]이 차단 대상
    d["functions"] = ["유튜브에서 본 내용 [1]", "보도자료 내용 [2]", "보고서 내용 [3]"]
    out = report_gen.verify_report(d, SEEN | {"maker1.com"}, BLOCKED)
    assert [s["url"] for s in out["sources"]] == ["https://maker1.com/news", "https://dot.gov/report"]
    assert out["functions"] == ["유튜브에서 본 내용", "보도자료 내용 [1]", "보고서 내용 [2]"]
    assert any(c["reason"] == "근거 출처가 검증에서 제외됨" and c["text"] == "유튜브에서 본 내용" for c in out["checks"])
    assert all(c["checked"] is False for c in out["checks"])


def test_f3_오류_판단값이_잘못되면_보류로():
    d = fake_report_data()
    d["judgement"] = "최고"
    assert report_gen.verify_report(d, SEEN | {"maker1.com"}, BLOCKED)["judgement"] == "보류"


def test_f5_오류_없는_항목은_다시생성_거부():
    with pytest.raises(ValueError):
        report_gen.regenerate_section("hack", fake_report_data(), "")


# ---------- F4 이미지 ----------
def test_f4_형식_판별():
    assert images.image_kind(make_png()) == "png"
    assert images.image_kind(b"\xff\xd8\xff\xe0abc") == "jpg"
    assert images.image_kind(b"GIF89a....") == "gif"
    assert images.image_kind(b"RIFF\x00\x00\x00\x00WEBPVP8 ") is None  # WebP는 워드 미지원
    assert images.image_kind(b"hello world") is None


@pytest.mark.parametrize("data,msg", [
    (b"", "비어"), (b"x" * (images.MAX_BYTES + 1), "5MB"), (b"not an image but named .png", "이미지 파일이 아닙니다"),
])
def test_f4_오류_업로드_검증(data, msg):
    kind, err = images.validate_upload(data)
    assert kind is None and msg in err


def test_f4_정상_업로드_검증():
    assert images.validate_upload(make_png()) == ("png", None)


def test_f4_og_image_찾기_상대경로_포함():
    html = '<head><meta property="og:image" content="/img/a.png"></head>'
    assert images.find_og_image(html, "https://maker.com/news/1") == "https://maker.com/img/a.png"
    assert images.find_og_image("<html></html>", "https://maker.com") is None


def test_f4_본문이미지_수집_로고아이콘_제외():
    html = ('<meta property="og:image" content="/og.jpg"><img src="/img/logo.png"><img src="icons/x.png">'
            '<img data-src="/photos/drone.jpg"><img src="data:image/png;base64,AAAA"><img src="/a.svg">'
            '<img src="/photos/drone.jpg">')
    assert images.image_urls_in_page(html, "https://maker.com/news/1") == [
        "https://maker.com/og.jpg", "https://maker.com/photos/drone.jpg"]


# 사진 후보 3장: 사람 사진(대표 이미지), 장비 사진, WebP(워드 미지원) — 다른 페이지엔 작은 아이콘
PEOPLE = make_png(120, 80, noise=True)
DRONE_A, DRONE_B, DRONE_A_CROP = (make_png(130, 90, noise=True), make_png(140, 90, noise=True),
                                  make_png(110, 90, noise=True))
WHAT = {PEOPLE: "연구원 단체 사진", DRONE_A: "교량 내부를 나는 드론", DRONE_B: "책상 위 드론 두 대",
        DRONE_A_CROP: "교량 내부를 나는 드론(잘라낸 사진)"}
SCORE = {PEOPLE: 1, DRONE_A: 9, DRONE_B: 7, DRONE_A_CROP: 8}


def photo_handler(req):
    u = str(req.url)
    pages = {
        "https://a.com/p": '<meta property="og:image" content="https://a.com/team.png"><img src="/drone-a.png">'
                           '<img src="/x.webp"><img src="/drone-a-crop.png">',
        "https://b.com/p": '<img src="/tiny.png"><img src="/drone-b.png">',
    }
    if u in pages:
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=pages[u])
    files = {"https://a.com/team.png": PEOPLE, "https://a.com/drone-a.png": DRONE_A,
             "https://a.com/drone-a-crop.png": DRONE_A_CROP, "https://b.com/drone-b.png": DRONE_B,
             "https://a.com/x.webp": b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"0" * 20000,
             "https://b.com/tiny.png": make_png(4, 4)}
    return httpx.Response(200, content=files[u]) if u in files else httpx.Response(404)


def fake_judge(cands, eq, desc):
    """사람 사진은 거르고, 잘라낸 사진은 원본(DRONE_A)과 같은 장면으로 표시."""
    idx = {c["data"]: i for i, c in enumerate(cands)}
    return [{"index": i, "shows_equipment": c["data"] != PEOPLE, "score": SCORE[c["data"]], "what": WHAT[c["data"]],
             "duplicate_of": idx.get(DRONE_A, -1) if c["data"] == DRONE_A_CROP else -1} for i, c in enumerate(cands)]


def client_():
    return httpx.Client(transport=httpx.MockTransport(photo_handler))


def test_f4_사람사진_같은장면은_빼고_서로다른_장비사진_2장(tmp_path):
    out = images.find_equipment_images(["https://a.com/p", "https://b.com/p"], "드론", "교량 점검",
                                       tmp_path / "r1", judge=fake_judge, client=client_())
    assert [open(p["file"], "rb").read() for p in out["photos"]] == [DRONE_A, DRONE_B]  # 점수순, 잘라낸 사진 제외
    assert [p["check"]["what"] for p in out["photos"]] == ["교량 내부를 나는 드론", "책상 위 드론 두 대"]
    assert out["photos"][1]["credit"] == "출처: b.com, https://b.com/p" and out["note"] == ""
    whats = [r["what"] for r in out["rejected"]]
    assert "연구원 단체 사진" in whats and any("같은 장면" in w for w in whats)
    assert out["photos"][0]["file"] != out["photos"][1]["file"]


def test_f4_예외_장비사진이_1장뿐이면_안내(tmp_path):
    out = images.find_equipment_images(["https://b.com/p"], "드론", "", tmp_path / "r5", judge=fake_judge, client=client_())
    assert len(out["photos"]) == 1 and "1장만 찾음" in out["note"]


def test_f4_예외_모두_장비사진이_아니면_사진없음(tmp_path):
    judge = lambda cands, eq, d: [{"index": i, "shows_equipment": False, "score": 0, "what": "인물", "duplicate_of": -1}
                                  for i in range(len(cands))]
    out = images.find_equipment_images(["https://a.com/p"], "드론", "", tmp_path / "r2", judge=judge, client=client_())
    assert out["photos"] == [] and "모두 장비 사진이 아님" in out["note"] and len(out["rejected"]) == 3


def test_f4_예외_사진후보가_없으면_사진없음(tmp_path):
    out = images.find_equipment_images(["https://none.com/x"], "드론", "", tmp_path / "r3",
                                       judge=fake_judge, client=client_())
    assert out["photos"] == [] and "찾지 못함" in out["note"]


def test_f4_오류_AI판별_실패시_사진을_임의로_넣지_않음(tmp_path):
    def broken(*a):
        raise RuntimeError("api down")
    out = images.find_equipment_images(["https://a.com/p"], "드론", "", tmp_path / "r4", judge=broken, client=client_())
    assert out["photos"] == [] and "넣지 않음" in out["note"]
    assert not list(tmp_path.glob("r4*"))  # 파일도 저장하지 않음


def two_photos(tmp_path, w=400, h=300):
    out = []
    for i in range(2):
        img = tmp_path / f"p{i}.png"
        img.write_bytes(make_png(w + i, h))
        out.append({"file": str(img), "credit": f"출처: maker{i}.com, https://maker{i}.com/news"})
    return out


# ---------- F8 워드 ----------
def test_f8_워드_파일_내용(tmp_path):
    d = fake_report_data("자율 방호차/ATMA: 신형?")
    d["images"] = two_photos(tmp_path)
    name = docgen.file_name("2026-W47", d["name"])
    assert name == "2026-W47_1p_자율_방호차_ATMA_신형.docx"  # 파일명에 못 쓰는 문자 정리
    path = docgen.build_docx(d, "2026-W47", tmp_path / name)
    doc = Document(path)
    text = "\n".join(p.text for p in doc.paragraphs) + "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
    for expected in ["자율 방호차/ATMA: 신형?", "□ 주요 기능·제원", "○ 선도 차량을 무인으로 추종함",
                     "■ 권고", "□ 보류", "안전: 운전원 추돌 위험 제거", "- 최고 속도: 40km/h (업체 주장)"]:
        assert expected in text, expected
    # 1p에는 출처를 넣지 않음: 문장 끝 [n], 참고 자료 목록, 사진 아래 출처, 링크 모두 없음
    for absent in ["[1]", "[2]", "참고 자료", "출처:", "maker0.com", "dot.gov"]:
        assert absent not in text, absent
    assert not [r for r in doc.part.rels.values() if "hyperlink" in r.reltype]
    assert len(doc.inline_shapes) == 2 and len(doc.tables[1].rows) == 1  # 사진 2장 나란히, 출처 줄 없음


def test_plain_출처번호_제거():
    assert docgen.plain("후방 추돌 충격을 흡수함 [1][3]") == "후방 추돌 충격을 흡수함"
    assert docgen.plain("최고 속도: 40km/h [2] (업체 주장)") == "최고 속도: 40km/h (업체 주장)"
    assert docgen.plain(None) == "" and docgen.plain("[1]") == ""


@pytest.mark.parametrize("credit,expected", [
    ("출처: news.rub.de, https://news.rub.de/a/b", ("출처: news.rub.de", "https://news.rub.de/a/b")),
    ("출처: 국토교통성 https://mlit.go.jp/x", ("출처: 국토교통성", "https://mlit.go.jp/x")),
    ("https://maker.com/p", ("출처: maker.com", "https://maker.com/p")),
    ("출처: (직접 입력하세요)", ("출처: (직접 입력하세요)", None)),
    ("", ("출처: 확인 필요", None)),
])
def test_f4_사진출처_짧게_표시(credit, expected):
    assert docgen.split_credit(credit) == expected


def test_f8_사진이_없으면_사진없음_표시(tmp_path):
    d = fake_report_data()
    d["images"] = []
    doc = Document(docgen.build_docx(d, "2026-W47", tmp_path / "a.docx"))
    assert len(doc.inline_shapes) == 0
    photo_tbl = doc.tables[1]
    assert [c.text for c in photo_tbl.rows[0].cells] == ["사진 없음"]
    assert "확인 필요" not in photo_tbl.rows[0].cells[0].text


def test_f8_사진이_1장이면_나머지칸_사진없음(tmp_path):
    d = fake_report_data()
    d["images"] = two_photos(tmp_path)[:1]
    doc = Document(docgen.build_docx(d, "2026-W47", tmp_path / "b.docx"))
    assert len(doc.inline_shapes) == 1
    assert doc.tables[1].rows[0].cells[1].text == "사진 없음"


def test_f8_사진파일이_사라졌으면_사진없음(tmp_path):
    d = fake_report_data()
    d["images"] = [{"file": str(tmp_path / "gone.png"), "credit": ""}]
    doc = Document(docgen.build_docx(d, "2026-W47", tmp_path / "c.docx"))
    assert len(doc.inline_shapes) == 0 and doc.tables[1].rows[0].cells[0].text == "사진 없음"


# ---------- F6 실제 MS Word 페이지 수 (Word 없는 PC는 건너뜀) ----------
@pytest.mark.word
def test_f6_실제_페이지수(tmp_path):
    d = fake_report_data()
    d["images"] = two_photos(tmp_path, 800, 600)  # 사진 2장이어도 1장
    short = docgen.build_docx(d, "2026-W47", tmp_path / "short.docx")
    n = docgen.count_pages(short)
    if n is None:
        pytest.skip("MS Word 없음")
    assert n == 1
    d["functions"] = [f"아주 긴 문장으로 분량을 늘림 {i} " * 4 for i in range(25)]
    assert docgen.count_pages(docgen.build_docx(d, "2026-W47", tmp_path / "long.docx")) > 1
