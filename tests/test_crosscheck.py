"""교차 검증: 다른 출처로 사실 확인 → 틀린 내용 수정 (적용 규칙 + 화면 흐름)."""
from urllib.parse import unquote

from conftest import fake_report_data, wait_status
from test_app import make_report
from webapp import crosscheck as xc
from webapp import db

ORIG = {"maker1.com", "dot.gov"}          # 원래 출처 도메인
SEEN = {"maker1.com", "dot.gov", "news.example.com", "research.example.org"}  # 교차 검증 중 실제로 본 도메인


def res(id, verdict, url="", correction="", evidence="근거"):
    return {"id": id, "verdict": verdict, "evidence": evidence, "correction": correction,
            "source_title": "독립 기사", "source_url": url}


def test_검증대상_문장목록_출처번호_제거_국내적용성_제외():
    ids = {c["id"]: c["text"] for c in xc.claims_of(fake_report_data())}
    assert "functions.0" in ids and ids["functions.0"] == "선도 차량을 무인으로 추종함"
    assert "maker" in ids and "improvements.safety" in ids and "reasons.1" in ids
    assert not any(k.startswith("domestic") for k in ids)


def test_정상_독립출처로_불일치면_문장수정_출처추가_확인필요추가():
    d = fake_report_data()
    fixed, recs = xc.apply_results(d, [res("specs.0", "불일치", "https://news.example.com/a",
                                           "최고 속도: 32km/h (업체 주장)")], SEEN, ORIG, [])
    assert fixed["specs"][0] == "최고 속도: 32km/h (업체 주장) [3]"
    assert fixed["sources"][2] == {"title": "독립 기사", "url": "https://news.example.com/a"}
    assert recs[0]["after"] and recs[0]["before"] == "최고 속도: 40km/h (업체 주장)"
    assert any("교차 검증으로 수정됨" in c["reason"] for c in fixed["checks"])
    assert d["specs"][0].startswith("최고 속도: 40km/h")  # 원본은 그대로 (복사본 수정)


def test_정상_개요칸은_출처번호_없이_수정():
    fixed, _ = xc.apply_results(fake_report_data(), [res("released", "불일치", "https://news.example.com/b", "2026-10 발표")],
                                SEEN, ORIG, [])
    assert fixed["released"] == "2026-10 발표"


def test_예외_같은기관_사이트는_독립출처가_아님_수정안함():
    # dot.gov 의 하위 사이트(www2.dot.gov)도 같은 기관 → 자동 수정하지 않고 '불일치 의심'으로 확인 필요에 추가
    fixed, recs = xc.apply_results(fake_report_data(), [res("functions.1", "불일치", "https://www2.dot.gov/x", "다른 문장")],
                                   SEEN | {"www2.dot.gov"}, ORIG, [])
    assert fixed["functions"][1] == "후방 추돌 충격을 흡수함 [1]" and recs[0]["verdict"] == "불일치 의심"
    assert any("자동 수정 안 함" in c["reason"] for c in fixed["checks"])


def test_오류_조사중_보지않은_URL은_지어낸것으로_보고_수정안함():
    fixed, recs = xc.apply_results(fake_report_data(), [res("functions.0", "불일치", "https://made-up.example/x", "바뀐 문장")],
                                   SEEN, ORIG, [])
    assert fixed["functions"][0] == "선도 차량을 무인으로 추종함 [1]" and recs[0]["after"] is None


def test_예외_독립출처없는_일치는_확인불가로_낮추고_수치문장은_확인필요():
    fixed, recs = xc.apply_results(fake_report_data(), [res("specs.0", "일치", "")], SEEN, ORIG, [])
    assert recs[0]["verdict"] == "확인 불가"
    assert any("교차 확인 못 함" in c["reason"] for c in fixed["checks"])


def test_정상_독립출처로_일치():
    fixed, recs = xc.apply_results(fake_report_data(), [res("functions.0", "일치", "https://research.example.org/p")],
                                   SEEN, ORIG, [])
    assert recs[0]["verdict"] == "일치" and recs[0]["source"]["url"] == "https://research.example.org/p"
    assert len(fixed["sources"]) == 2  # 일치는 1p 출처를 늘리지 않음 (분량 유지)


def test_오류_없는_문장id는_무시():
    _, recs = xc.apply_results(fake_report_data(), [res("functions.99", "불일치", "https://news.example.com/a", "x"),
                                                    res("hack", "일치")], SEEN, ORIG, [])
    assert recs == []


def test_같은기관_판단():
    assert xc.base_domain("news.rub.de") == "rub.de"
    assert xc.base_domain("www.ex.co.kr") == "ex.co.kr"


def test_화면_교차검증_실행과_결과표(client, monkeypatch):
    _, rid = make_report(client)

    def fake_cross(d, settings, progress):
        progress("가짜 교차 검증")
        fixed, recs = xc.apply_results(d, [res("specs.0", "불일치", "https://news.example.com/a", "최고 속도: 32km/h"),
                                           res("functions.0", "일치", "https://research.example.org/p")], SEEN, ORIG, [])
        fixed["crosscheck"] = {"at": "2026-11-18 10:00", "records": recs, "searches": 3, "fetches": 2,
                               "notes": "", "usage": {}}
        return fixed

    monkeypatch.setattr("webapp.app.crosscheck.cross_check", fake_cross)
    r = client.post(f"/reports/{rid}/crosscheck", follow_redirects=False)
    assert unquote(r.headers["location"]).endswith("#xcheck")
    assert wait_status(client, f"/reports/{rid}/crosscheck/status", "running")["status"] == "done"
    d = db.get_report(rid)["data"]
    assert d["specs"][0] == "최고 속도: 32km/h [3]" and d["crosscheck"]["records"]
    html = client.get(f"/reports/{rid}").text
    assert "수정됨" in html and "최고 속도: 32km/h" in html and "일치 1" in html and "수정 1" in html


def test_화면_교차검증_오류는_안내(client, monkeypatch):
    _, rid = make_report(client)
    monkeypatch.setattr("webapp.app.crosscheck.cross_check",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("검색 실패")))
    client.post(f"/reports/{rid}/crosscheck")
    s = wait_status(client, f"/reports/{rid}/crosscheck/status", "running")
    assert s["status"] == "error" and "검색 실패" in s["error"]
