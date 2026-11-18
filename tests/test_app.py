"""웹앱 흐름 테스트 (브라우저 대신 TestClient): 홈 → 후보 찾기 → 선택 → 1p 편집 → 다운로드 → 보고 완료 → 이력."""
from datetime import date, timedelta
from io import BytesIO
from urllib.parse import unquote

import anthropic
import httpx
import pytest
from docx import Document

from conftest import make_png, wait_status
from webapp import db
from webapp.claude_client import friendly_error

TODAY = date.today()
START, END = (TODAY - timedelta(days=7)).isoformat(), TODAY.isoformat()


def loc(r):
    return unquote(r.headers["location"])


def start_run(client, **form):
    r = client.post("/search", data={"start": START, "end": END, **form}, follow_redirects=False)
    assert r.status_code == 303 and loc(r).startswith("/runs/"), loc(r)
    run_id = int(loc(r).split("/")[-1])
    assert wait_status(client, f"/runs/{run_id}/status", "running")["status"] == "done"
    return run_id


def make_report(client):
    run_id = start_run(client)
    cid = db.get_run(run_id)["result"]["candidates"][0]["id"]  # 이미 보고한 장비는 빠져 있으므로 남은 첫 후보
    r = client.post(f"/runs/{run_id}/select", data={"candidate_id": cid}, follow_redirects=False)
    rid = int(loc(r).split("/")[-1])
    assert wait_status(client, f"/reports/{rid}/status", "generating")["status"] == "ready"
    return run_id, rid


def editor_form(client, rid, **over):
    d = db.get_report(rid)["data"]
    form = {"name": d["name"], "maker": d["maker"], "country": d["country"], "released": d["released"],
            "category": d["category"], "functions": "\n".join(d["functions"]), "specs": "\n".join(d["specs"]),
            "imp_safety": d["improvements"]["safety"], "imp_efficiency": d["improvements"]["efficiency"],
            "imp_cost": d["improvements"]["cost"], "domestic": "\n".join(d["domestic"]),
            "judgement": d["judgement"], "reasons": "\n".join(d["reasons"]),
            "sources": "\n".join(f"{s['title']} | {s['url']}" for s in d["sources"]), "after": ""}
    for i, p in enumerate(d.get("images", [])):
        form[f"image_credit_{i}"] = p["credit"]
    form.update(over)
    return form


# ---------- S1 홈 · F10 설정 ----------
def test_홈_화면이_열림(client):
    r = client.get("/")
    assert r.status_code == 200 and "A4 한 장" in r.text and "장비 3개 찾고 1p 작성" in r.text
    assert "누적 보고" in r.text and "벤치마킹 권고" in r.text  # 현황 수치


@pytest.mark.parametrize("count,days", [("99", "7"), ("0", "7"), ("3", "0"), ("abc", "7")])
def test_f10_오류_설정값_범위밖은_저장거부(client, count, days):
    r = client.post("/settings", data={"candidate_count": count, "default_period_days": days}, follow_redirects=False)
    assert "error=" in r.headers["location"]


def test_f10_정상_설정저장후_홈반영(client):
    s = db.get_settings()
    form = {"candidate_count": "4", "default_period_days": "7", "countries": "미국, 일본",
            "preferred_domains": "a.com", "blocked_domains": "youtube.com"}
    for i, c in enumerate(s["categories"]):
        form[f"cat_name_{i}"] = c["name"]
        for lang, kws in c["keywords"].items():
            form[f"cat_{i}_{lang}"] = ", ".join(kws)
    assert "saved=1" in client.post("/settings", data=form, follow_redirects=False).headers["location"]
    assert "장비 4개 찾고 1p 작성" in client.get("/").text
    form["candidate_count"] = "3"
    client.post("/settings", data=form)
    assert db.get_settings()["candidate_count"] == 3


# ---------- F1 후보 찾기 ----------
@pytest.mark.parametrize("start,end", [(END, START), (START, (TODAY + timedelta(days=30)).isoformat()), ("abc", END)])
def test_f1_오류_잘못된_기간은_조사안함(client, start, end):
    r = client.post("/search", data={"start": start, "end": end}, follow_redirects=False)
    assert "조사 기간을 확인하세요" in loc(r)
    assert client.calls["research"] == []


def test_f1_정상_후보3개_카드표시_분야전달(client):
    run_id = start_run(client, categories="터널·교량 점검")
    assert client.calls["research"][-1]["categories"] == ["터널·교량 점검"]
    html = client.get(f"/runs/{run_id}").text
    assert html.count("이 장비 1p 작성") == 3 and "장비1" in html and "기간 외" in html
    assert "도로 유지관리 근접도" in html and "9/10" in html


def test_f1_예외_다른후보찾기는_직전후보를_제외(client):
    run_id = start_run(client)
    r = client.post(f"/runs/{run_id}/retry", data={"start": START, "end": END}, follow_redirects=False)
    new_id = int(loc(r).split("/")[-1])
    wait_status(client, f"/runs/{new_id}/status", "running")
    assert {"장비1", "장비2", "장비3"} <= set(client.calls["research"][-1]["exclude"])
    assert "근접도 기준을 넘는 장비를 0개만" in client.get(f"/runs/{new_id}").text


def test_f1_오류_API오류는_한국어로_안내(client, monkeypatch):
    def boom(*a, **k):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    monkeypatch.setattr("webapp.app.research.run_research", boom)
    r = client.post("/search", data={"start": START, "end": END}, follow_redirects=False)
    run_id = int(loc(r).split("/")[-1])
    s = wait_status(client, f"/runs/{run_id}/status", "running")
    assert s["status"] == "error" and "네트워크" in s["error"]
    assert "같은 조건으로 다시 찾기" in client.get(f"/runs/{run_id}").text


def test_친절한_오류문구():
    assert "네트워크" in friendly_error(anthropic.APIConnectionError(request=httpx.Request("GET", "https://x")))
    assert friendly_error(ValueError("x")).startswith("오류:")


# ---------- F3 1p 생성 ----------
def test_f3_오류_없는후보_선택(client):
    run_id = start_run(client)
    r = client.post(f"/runs/{run_id}/select", data={"candidate_id": "c9"}, follow_redirects=False)
    assert "후보를 다시 선택하세요" in loc(r)


def test_f3_정상_선택하면_7개항목_편집화면(client):
    _, rid = make_report(client)
    html = client.get(f"/reports/{rid}").text
    for s in ["□ 개요", "□ 장비 사진", "□ 주요 기능·제원", "□ 기존 방식 대비 개선점", "□ 국내 적용성",
              "□ 벤치마킹 판단", "원문 대조용 출처", "확인 필요"]:
        assert s in html, s
    assert "■ 권고" in html
    # 출처는 1p 미리보기에는 없고, 원문 대조용 패널에만 문장별 [n] 링크로 있음
    preview = html.split('id="preview"')[1].split("원문 대조용 출처")[0]
    assert "[1]" not in preview and "참고 자료" not in preview and "maker1.com" not in preview
    assert '<sup><a href="https://maker1.com/news"' in html and "1p에는 표시 안 됨" in html


def test_f3_예외_같은후보_다시선택하면_기존초안(client):
    run_id, rid = make_report(client)
    r = client.post(f"/runs/{run_id}/select", data={"candidate_id": "c1"}, follow_redirects=False)
    assert loc(r) == f"/reports/{rid}"


def test_f3_오류_생성실패시_다시생성_버튼(client, monkeypatch):
    run_id = start_run(client)
    monkeypatch.setattr("webapp.app.report_gen.generate_report",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("실패")))
    rid = int(loc(client.post(f"/runs/{run_id}/select", data={"candidate_id": "c2"}, follow_redirects=False)).split("/")[-1])
    assert wait_status(client, f"/reports/{rid}/status", "generating")["status"] == "error"
    assert "다시 생성" in client.get(f"/reports/{rid}").text


# ---------- F5 편집·미리보기 ----------
def test_f5_정상_수정저장_유지(client):
    _, rid = make_report(client)
    form = editor_form(client, rid, functions="새 기능 문장 [1]\n둘째 기능", judgement="보류")
    assert "저장했습니다" in loc(client.post(f"/reports/{rid}/save", data=form, follow_redirects=False))
    d = db.get_report(rid)["data"]
    assert d["functions"] == ["새 기능 문장 [1]", "둘째 기능"] and d["judgement"] == "보류"
    assert "■ 보류" in client.get(f"/reports/{rid}").text


def test_f5_예외_빈항목은_저장하되_경고(client):
    _, rid = make_report(client)
    r = client.post(f"/reports/{rid}/save", data=editor_form(client, rid, functions="", domestic=""), follow_redirects=False)
    assert "빈 항목: 주요 기능, 국내 적용성" in loc(r)


@pytest.mark.parametrize("over,msg", [({"judgement": "최고"}, "판단은"), ({"sources": "제목만 있고 주소 없음"}, "형식")])
def test_f5_오류_잘못된입력은_저장거부(client, over, msg):
    _, rid = make_report(client)
    before = db.get_report(rid)["data"]
    r = client.post(f"/reports/{rid}/save", data=editor_form(client, rid, **over), follow_redirects=False)
    assert "error=" in r.headers["location"] and msg in loc(r)
    assert db.get_report(rid)["data"] == before


def test_f5_실시간미리보기는_저장하지않음(client):
    _, rid = make_report(client)
    html = client.post(f"/reports/{rid}/preview", data=editor_form(client, rid, domestic="미리보기 문장 [2]")).text
    assert "○ 미리보기 문장" in html and "[2]" not in html and "dot.gov" not in html  # 1p 미리보기엔 출처 없음
    assert "미리보기 문장 [2]" not in db.get_report(rid)["data"]["domestic"]


def test_f5_미리보기_이스케이프(client):
    _, rid = make_report(client)
    html = client.post(f"/reports/{rid}/preview", data=editor_form(client, rid, domestic="<script>x</script> [9]")).text
    assert "<script>x" not in html and "&lt;script&gt;" in html and "[9]" not in html


def test_f7_원문대조_패널_없는출처번호_표시(client):
    _, rid = make_report(client)
    client.post(f"/reports/{rid}/save", data=editor_form(client, rid, domestic="근거 없는 번호 [9]"))
    html = client.get(f"/reports/{rid}").text
    assert '<sup class="bad" title="없는 출처 번호">[9]</sup>' in html


def test_f5_항목별_다시생성(client, monkeypatch):
    _, rid = make_report(client)
    monkeypatch.setattr("webapp.app.report_gen.regenerate_section", lambda s, d, n: {"domestic": ["다시 쓴 문장 [1]"]})
    r = client.post(f"/reports/{rid}/section/domestic", follow_redirects=False)
    assert "다시 생성했습니다" in loc(r) and db.get_report(rid)["data"]["domestic"] == ["다시 쓴 문장 [1]"]
    assert "error=" in client.post(f"/reports/{rid}/section/hack", follow_redirects=False).headers["location"]


# ---------- F4 사진 ----------
def test_f4_사진2장_출처_AI확인결과_표시(client):
    _, rid = make_report(client)
    photos = db.get_report(rid)["data"]["images"]
    assert len(photos) == 2 and all(p["credit"].startswith("출처: maker1.com") for p in photos)
    assert client.get(f"/reports/{rid}/image/0").content[:4] == b"\x89PNG"
    assert client.get(f"/reports/{rid}/image/1").content[:4] == b"\x89PNG"
    html = client.get(f"/reports/{rid}").text
    assert "AI 확인: 장비 사진 — 도로 위의 방호차량" in html and "AI 확인: 장비 사진 — 작업 중인 방호차량 후면" in html
    assert "장비 사진이 아니어서 뺀 사진 1장" in html
    assert 'src="/reports/%d/image/1' % rid in client.post(f"/reports/{rid}/preview", data=editor_form(client, rid)).text


def test_f4_사진다시찾기_버튼(client, monkeypatch):
    _, rid = make_report(client)
    r = client.post(f"/reports/{rid}/photos/refind", follow_redirects=False)
    assert "장비 사진 2장으로 교체했습니다" in loc(r)
    monkeypatch.setattr("webapp.app.images.find_equipment_images", lambda *a, **k: {
        "photos": [], "rejected": [], "note": "사진 후보 3장 모두 장비 사진이 아님"})
    r = client.post(f"/reports/{rid}/photos/refind", follow_redirects=False)
    assert "장비 사진을 찾지 못했습니다" in loc(r) and "직접 올려" in loc(r)
    html = client.get(f"/reports/{rid}").text
    assert "사진 후보 3장 모두 장비 사진이 아님" in html and "사진(0/2장)" in html


def test_f4_예외_한장만_찾으면_안내(client, monkeypatch):
    _, rid = make_report(client)
    one = db.get_report(rid)["data"]["images"][:1]
    monkeypatch.setattr("webapp.app.images.find_equipment_images", lambda *a, **k: {
        "photos": one, "rejected": [], "note": "장비 사진을 1장만 찾음 — 나머지는 직접 올려 주세요"})
    assert "1장만 찾음" in loc(client.post(f"/reports/{rid}/photos/refind", follow_redirects=False))
    assert "사진(1/2장)" in client.get(f"/reports/{rid}").text


def test_f4_예외_장비가_아닌_사진을_올리면_경고(client, monkeypatch):
    _, rid = make_report(client)
    monkeypatch.setattr("webapp.app.photo_check.judge_images",
                        lambda c, e, d: [{"index": 0, "shows_equipment": False, "score": 0, "what": "고양이 사진"}])
    r = client.post(f"/reports/{rid}/image/0", files={"file": ("a.png", make_png(), "image/png")}, follow_redirects=False)
    assert "장비 사진이 아닌 것 같습니다: 고양이 사진" in loc(r)
    assert db.get_report(rid)["data"]["images"][0]["check"]["status"] == "warn"


def test_예전형식_사진1장_데이터도_열림(client):
    _, rid = make_report(client)
    d = db.get_report(rid)["data"]
    old = {k: v for k, v in d.items() if k not in ("images", "image_rejected", "image_note")}
    old["image"] = {**d["images"][0], "rejected": [{"url": "u", "what": "인물"}]}
    db.update_report(rid, data=old)
    d2 = db.get_report(rid)["data"]
    assert len(d2["images"]) == 1 and d2["image_rejected"] == [{"url": "u", "what": "인물"}]
    assert client.get(f"/reports/{rid}").status_code == 200


@pytest.mark.parametrize("data,msg", [(b"text pretending to be png", "이미지 파일이 아닙니다"),
                                      (b"\x89PNG" + b"0" * (6 * 1024 * 1024), "5MB")])
def test_f4_오류_업로드거부(client, data, msg):
    _, rid = make_report(client)
    r = client.post(f"/reports/{rid}/image/0", files={"file": ("a.png", data, "image/png")}, follow_redirects=False)
    assert msg in loc(r)


def test_f4_오류_없는_사진자리(client):
    _, rid = make_report(client)
    r = client.post(f"/reports/{rid}/image/5", files={"file": ("a.png", make_png(), "image/png")}, follow_redirects=False)
    assert "사진 자리를 다시 선택하세요" in loc(r)
    assert client.get(f"/reports/{rid}/image/abc").status_code == 404


def test_f4_정상_자리별_업로드와_삭제(client):
    _, rid = make_report(client)
    r = client.post(f"/reports/{rid}/image/1", files={"file": ("a.png", make_png(), "image/png")}, follow_redirects=False)
    photos = db.get_report(rid)["data"]["images"]
    assert "사진 2을(를) 교체했습니다" in loc(r) and "_upload_1_" in photos[1]["file"] and "_upload_" not in photos[0]["file"]
    assert "직접 입력하세요" in photos[1]["credit"]
    client.post(f"/reports/{rid}/image/0/delete")
    assert len(db.get_report(rid)["data"]["images"]) == 1
    client.post(f"/reports/{rid}/image/0/delete")
    assert db.get_report(rid)["data"]["images"] == []
    assert client.get(f"/reports/{rid}/image/0").status_code == 404
    assert "사진 없음" in client.get(f"/reports/{rid}").text
    preview = client.post(f"/reports/{rid}/preview", data=editor_form(client, rid)).text
    assert '<div class="a4-nophoto">사진 없음</div>' in preview and "<img" not in preview
    assert "지울 사진이 없습니다" in loc(client.post(f"/reports/{rid}/image/0/delete", follow_redirects=False))
    # 빈 자리에 올리면 앞에서부터 채움
    client.post(f"/reports/{rid}/image/1", files={"file": ("a.png", make_png(), "image/png")})
    assert len(db.get_report(rid)["data"]["images"]) == 1


def test_f4_사진별_출처_수정(client):
    _, rid = make_report(client)
    client.post(f"/reports/{rid}/save", data=editor_form(client, rid, image_credit_1="출처: 국토교통성, https://mlit.go.jp/x"))
    assert db.get_report(rid)["data"]["images"][1]["credit"] == "출처: 국토교통성, https://mlit.go.jp/x"


# ---------- F6 분량 검사 ----------
@pytest.mark.parametrize("pages,msg,stored", [(1, "A4 1장입니다", 1), (2, "2페이지입니다", 2), (None, "확인하지 못했습니다", 0)])
def test_f6_분량검사_결과별_안내(client, monkeypatch, pages, msg, stored):
    _, rid = make_report(client)
    monkeypatch.setattr("webapp.app.docgen.count_pages", lambda p, timeout=90: pages)
    r = client.post(f"/reports/{rid}/save", data=editor_form(client, rid, after="pages"), follow_redirects=False)
    assert msg in loc(r) and db.get_report(rid)["pages"] == stored


def test_f6_생성중_1장넘으면_자동으로_줄임(client, monkeypatch):
    pages = iter([2, 1])
    monkeypatch.setattr("webapp.app.docgen.count_pages", lambda p, timeout=90: next(pages))
    monkeypatch.setattr("webapp.app.report_gen.shorten_report", lambda d, n: {"functions": ["짧게 줄인 기능 [1]"]})
    _, rid = make_report(client)
    rep = db.get_report(rid)
    assert rep["pages"] == 1 and rep["data"]["functions"] == ["짧게 줄인 기능 [1]"]


@pytest.mark.parametrize("seq,msg", [([2, 1], "1장에 맞췄습니다"), ([3, 2], "아직 2페이지"), ([None], "확인하지 못했습니다")])
def test_f6_줄이기_버튼_결과별_안내(client, monkeypatch, seq, msg):
    _, rid = make_report(client)
    it = iter(seq)
    monkeypatch.setattr("webapp.app.docgen.count_pages", lambda p, timeout=90: next(it))
    monkeypatch.setattr("webapp.app.report_gen.shorten_report", lambda d, n: {"domestic": ["줄인 문장"]})
    assert msg in loc(client.post(f"/reports/{rid}/shorten", follow_redirects=False))


def test_f6_줄이기_결과의_없는_출처번호는_삭제():
    from webapp.report_gen import _drop_bad_markers
    out = _drop_bad_markers({"functions": ["a [1]", "b [7]"], "improvements": {"safety": "c [0]"}}, 2)
    assert out == {"functions": ["a [1]", "b"], "improvements": {"safety": "c"}}


# ---------- F8 다운로드 ----------
def test_f8_워드다운로드(client):
    _, rid = make_report(client)
    r = client.get(f"/reports/{rid}/download")
    assert r.status_code == 200 and "wordprocessingml" in r.headers["content-type"]
    assert "2026" in unquote(r.headers["content-disposition"]) or "_1p_" in unquote(r.headers["content-disposition"])
    doc = Document(BytesIO(r.content))
    assert any("장비1" in p.text for p in doc.paragraphs) and len(doc.inline_shapes) == 2


# ---------- F7 + F9 보고 완료 · 이력 ----------
def test_f7_f9_보고완료_이력저장_다음조사제외(client):
    _, rid = make_report(client)
    form = editor_form(client, rid, check_0="on", after="complete")
    r = client.post(f"/reports/{rid}/save", data=form, follow_redirects=False)
    assert "보고 완료로 저장했습니다" in loc(r)
    d = db.get_report(rid)["data"]
    assert [c["checked"] for c in d["checks"]] == [True, False]
    rows = [h for h in db.all_history() if h["name"] == "장비1" and h["status"] == "보고완료"]
    assert len(rows) == 1 and rows[0]["judgement"] == "권고" and rows[0]["report_id"] == rid
    assert "장비1" in db.reported_names()
    # 한 번 더 완료해도 이력은 1건
    client.post(f"/reports/{rid}/save", data=form)
    assert len([h for h in db.all_history() if h["name"] == "장비1" and h["status"] == "보고완료"]) == 1
    # 다음 조사에서 제외 목록에 포함
    start_run(client)
    assert "장비1" in client.calls["research"][-1]["exclude"]
    assert "보고 완료" in client.get("/").text


def test_f9_이력필터와_CSV(client):
    _, rid = make_report(client)
    name = db.get_report(rid)["data"]["name"]
    client.post(f"/reports/{rid}/save", data=editor_form(client, rid, after="complete"))
    html = client.get("/history", params={"judgement": "권고", "status": "보고완료"}).text
    assert name in html and "워드" in html
    assert "조건에 맞는 이력이 없습니다" in client.get("/history", params={"judgement": "불필요"}).text
    r = client.get("/history.csv", params={"status": "보고완료"})
    assert r.content.startswith("﻿".encode("utf-8")) and name in r.text and "주차,장비명" in r.text
