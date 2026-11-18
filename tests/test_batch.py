"""주간 1p 5건: 조사 기간 3개월 기본값, 도로 유지관리 근접도 선정, 자동 작성, 전체 작성, ZIP, 탭 이동."""
import io
import time
import zipfile
from datetime import date, timedelta
from urllib.parse import unquote

import pytest
from docx import Document

from conftest import wait_status
from test_app import START, END, start_run
from webapp import db, research


@pytest.fixture(autouse=True)
def no_prior_reports(monkeypatch):
    """앞선 테스트에서 '보고 완료'한 장비가 제외되지 않도록, 이 파일에서는 보고 이력을 비운 것으로 본다."""
    monkeypatch.setattr("webapp.app.db.reported_names", lambda: [])


def wait_batch(run_id, n, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        reps = db.reports_for_run(run_id)
        if len(reps) == n and all(r["status"] != "generating" for r in reps):
            return reps
        time.sleep(0.05)
    raise AssertionError("1p 묶음 작성이 끝나지 않음")


def cand(name, relevance, in_period=True):
    return {"name": name, "relevance": relevance, "in_period": in_period,
            "sources": [{"title": "t", "url": "https://maker.com/x", "type": "전문 언론 기사"}]}


# ---------- 기본값 ----------
def test_기본값_3개월_5건_자동작성():
    assert db.DEFAULT_SETTINGS["default_period_days"] == 90
    assert db.DEFAULT_SETTINGS["candidate_count"] == 5
    assert db.DEFAULT_SETTINGS["auto_generate"] is True


# ---------- 도로 유지관리 근접도 ----------
def test_근접도_높은순_선정_낮은것_제외():
    raw = [cand("범용 드론", 4), cand("차선 도색 로봇", 9, in_period=False), cand("포트홀 보수차", 10),
           cand("철도 점검차", 6), cand("신규 도로 포장기", 2), cand("방호차", 9)]
    out, dropped = research.verify_candidates(raw, {"maker.com"}, [], [], 5)
    assert [c["name"] for c in out] == ["포트홀 보수차", "방호차", "차선 도색 로봇", "철도 점검차"]  # 같은 점수면 기간 안 우선
    assert sum("거리가 멂" in d for d in dropped) == 2


def test_근접도_정보는_프롬프트와_스키마에_포함():
    item = research.CANDIDATE_SCHEMA["properties"]["candidates"]["items"]
    assert {"relevance", "relevance_reason"} <= set(item["required"])
    p = research.build_research_prompt({"categories": [], "countries": [], "preferred_domains": []},
                                       "2026-08-20", "2026-11-18", [], [], 5)
    assert "도로 유지관리 장비 근접도" in p and "9개 정도" in p  # 5건 + 여유 4


# ---------- 개발 3년 이내 ----------
def test_개발시기_해석():
    assert research.parse_dev("2024-05") == date(2024, 5, 28)
    assert research.parse_dev("2023") == date(2023, 12, 28)          # 연도만 → 그해 말로 너그럽게
    assert research.parse_dev("2025.3 시제품 공개") == date(2025, 3, 28)
    assert research.parse_dev("확인 필요") is None and research.parse_dev(None) is None
    assert research.dev_cutoff(date(2026, 11, 18)) == date(2023, 11, 18)
    assert research.dev_cutoff(date(2028, 2, 29)) == date(2025, 2, 28)


def test_개발3년_넘은장비는_제외_모르는것은_뒤로():
    cut = date(2023, 11, 18)
    raw = [{**cand("구형 방호차", 10), "developed": "2019-04"},
           {**cand("신형 노면조사차", 9), "developed": "2025-02"},
           {**cand("교통량 센서", 9), "developed": "확인 필요"},
           {**cand("포트홀 로봇", 9), "developed": "2023"},          # 2023년 말로 보아 기준일 이후 → 포함
           {**cand("경계선 장비", 10), "developed": "2023-10"}]      # 기준일 이전 → 제외
    out, dropped = research.verify_candidates(raw, {"maker.com"}, [], [], 5, cutoff=cut)
    assert [c["name"] for c in out] == ["신형 노면조사차", "포트홀 로봇", "교통량 센서"]
    assert [c["dev_unknown"] for c in out] == [False, False, True]
    assert sum("3년 경과" in d for d in dropped) == 2


def test_범위확대_점검검사_교통데이터_포함():
    p = research.build_research_prompt({"categories": [], "countries": [], "preferred_domains": []},
                                       "2026-08-20", "2026-11-18", [], [], 5)
    for word in ["도로 점검·검사", "교통량", "교통 데이터 수집", "모니터링", "최근 3년 이내 개발", research.dev_cutoff().isoformat()]:
        assert word in p, word
    item = research.CANDIDATE_SCHEMA["properties"]["candidates"]["items"]
    assert {"developed", "developed_basis"} <= set(item["required"])
    assert "3년이 지난" in research.build_structuring_prompt("노트", 5, ["포장"])


def test_분야를_검색묶음_3개로_나눔():
    s = {"categories": [{"name": n, "keywords": {}} for n in [
        "포장 유지관리 (차선 도색, 포트홀 보수 등)", "터널·교량 점검", "이동식 작업 안전 (라바콘 자동 설치 등)",
        "기타 유지관리 (제설, 청소, 녹지, 시설물)", "동향 일반 (신제품 발표·전시회)",
        "도로 점검·검사 (노면 조사·비파괴 검사)", "교통 데이터 수집 (교통량·축중·노면 기상)", "새 분야"]]}
    g = {label: [c["name"][:6] for c in cats] for label, _, cats in research.group_categories(s, [])}
    assert set(g) == {"작업 장비", "점검·검사", "교통 데이터·모니터링"}
    assert any(n.startswith("터널") for n in g["점검·검사"]) and any(n.startswith("도로 점검") for n in g["점검·검사"])
    assert any(n.startswith("교통") for n in g["교통 데이터·모니터링"]) and any(n.startswith("동향") for n in g["교통 데이터·모니터링"])
    assert "새 분야" in g["작업 장비"]  # 어디에도 안 맞는 분야는 작업 장비 묶음에
    only = research.group_categories(s, ["터널·교량 점검"])  # 분야를 골랐으면 해당 묶음만
    assert [label for label, _, _ in only] == ["점검·검사"]


def test_묶음별_검색프롬프트는_자기분야만():
    p = research.build_research_prompt({"categories": [], "countries": [], "preferred_domains": []},
                                       "2026-08-20", "2026-11-18", [], [], 4, focus=("교통 데이터·모니터링", "교통량 검지"))
    assert "「교통 데이터·모니터링」 분야만 맡는다" in p


def test_개발시기_모르면_따로_확인해서_채움():
    cands = [{**cand("A", 10), "developed": "확인 필요"}, {**cand("B", 9), "developed": "확인 필요"},
             {**cand("C", 9), "developed": "2025-01"}, {**cand("D", 3), "developed": "확인 필요"}]
    answers = {"A": {"developed": "2019-05", "developed_basis": "2019 첫 출시"},
               "B": {"developed": "2025-09", "developed_basis": "2025-09 시제품 공개"}}
    asked = []

    def checker(c):
        asked.append(c["name"])
        return answers.get(c["name"])
    n = research.fill_dev_dates(cands, checker, limit=5)
    assert n == 2 and sorted(asked) == ["A", "B"]  # 이미 아는 C, 근접도 낮은 D는 확인 안 함
    out, dropped = research.verify_candidates(cands, {"maker.com"}, [], [], 5, cutoff=date(2023, 11, 18))
    assert [c["name"] for c in out] == ["B", "C"] and any("A: 개발 후 3년 경과" in d for d in dropped)
    assert out[0]["developed_basis"].startswith("[개발 시기 확인 검색]")


def test_후보카드_개발시기_표시(client):
    run_id = start_run(client)
    html = client.get(f"/runs/{run_id}").text
    assert "2025-06 개발" in html and "2025-06 제조사 보도자료로 첫 공개" in html


# ---------- 자동 작성 ----------
def test_장비를_찾으면_1p를_모두_자동작성(client):
    db.save_settings({"auto_generate": True})
    run_id = start_run(client)
    reps = wait_batch(run_id, 3)
    assert [r["status"] for r in reps] == ["ready"] * 3
    assert [r["data"]["name"] for r in reps] == ["장비1", "장비2", "장비3"]
    html = client.get(f"/runs/{run_id}").text
    assert "이번 주 1p 3건" in html and "1p 검토·수정하기" in html and "워드 3건 한번에 받기" in html
    assert "1p 검토 중 (보고 0/3)" in client.get("/").text
    assert f'href="/runs/{run_id}">이번 주 1p 3건 보기' in client.get("/").text


def test_자동작성_끄면_목록만_그리고_전체작성_버튼(client):
    run_id = start_run(client)
    assert db.reports_for_run(run_id) == []
    html = client.get(f"/runs/{run_id}").text
    assert "3건 1p 작성 시작" in html
    r = client.post(f"/runs/{run_id}/generate_all", follow_redirects=False)
    assert "1p 3건 작성을 시작했습니다" in unquote(r.headers["location"])
    wait_batch(run_id, 3)
    r = client.post(f"/runs/{run_id}/generate_all", follow_redirects=False)  # 다시 눌러도 중복 생성 없음
    assert "이미 있습니다" in unquote(r.headers["location"]) and len(db.reports_for_run(run_id)) == 3


def test_ZIP으로_워드_한번에(client):
    run_id = start_run(client)
    client.post(f"/runs/{run_id}/generate_all")
    wait_batch(run_id, 3)
    r = client.get(f"/runs/{run_id}/download.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = sorted(z.namelist())
    assert len(names) == 3 and names[0].startswith("1_") and all(n.endswith(".docx") for n in names)
    assert any("장비2" in p.text for p in Document(io.BytesIO(z.read(names[1]))).paragraphs)


def test_ZIP_1p가_없으면_안내(client):
    run_id = start_run(client)
    r = client.get(f"/runs/{run_id}/download.zip", follow_redirects=False)
    assert "아직 없습니다" in unquote(r.headers["location"])


def test_편집화면_5건_탭이동(client):
    run_id = start_run(client)
    client.post(f"/runs/{run_id}/generate_all")
    reps = wait_batch(run_id, 3)
    html = client.get(f"/reports/{reps[1]['id']}").text
    for r in reps:
        assert f'href="/reports/{r["id"]}"' in html
    assert 'class="on"' in html.split(f'href="/reports/{reps[1]["id"]}"')[1][:20]


def test_주간상태_묶음기준(client):
    run_id = start_run(client)
    client.post(f"/runs/{run_id}/generate_all")
    reps = wait_batch(run_id, 3)
    week = reps[0]["week"]
    for r in reps[:2]:
        db.mark_reported({**r, "file_path": None})
    assert db.week_status(week) == "1p 검토 중 (보고 2/3)"
    db.mark_reported({**reps[2], "file_path": None})
    assert db.week_status(week) == "보고 완료"
    with db.connect() as con:  # 다음 테스트에 영향이 없도록 이 테스트의 보고 이력은 지운다
        con.execute(f"DELETE FROM history WHERE report_id IN ({','.join(str(r['id']) for r in reps)})")
        con.execute(f"UPDATE reports SET status='ready' WHERE run_id={run_id}")


def test_설정_자동작성_켜고끄기(client):
    s = db.get_settings()
    form = {"candidate_count": "5", "default_period_days": "90"}
    for i, c in enumerate(s["categories"]):
        form[f"cat_name_{i}"] = c["name"]
    client.post("/settings", data={**form, "auto_generate": "on"})
    assert db.get_settings()["auto_generate"] is True
    client.post("/settings", data=form)
    assert db.get_settings()["auto_generate"] is False
    client.post("/settings", data={**form, "candidate_count": "3"})
