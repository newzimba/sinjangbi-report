"""테스트 공통 설정: 임시 데이터 폴더 사용, Claude 호출은 가짜 응답으로 대체."""
import os
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

import pytest

TMP = Path(tempfile.mkdtemp(prefix="sinjangbi_test_"))
os.environ["SINJANGBI_DATA"] = str(TMP)  # 실제 data/app.db는 건드리지 않음
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from webapp import app as app_module  # noqa: E402


def make_png(w=40, h=30, noise=False):
    """의존성 없이 만드는 진짜 PNG. noise=True면 압축이 안 돼 실제 사진처럼 수십 KB가 된다."""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    row = (lambda: os.urandom(3 * w)) if noise else (lambda: b"\x80\x40\x20" * w)
    raw = b"".join(b"\x00" + row() for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def fake_candidates(prefix="장비"):
    return [{
        "id": f"c{i}", "name": f"{prefix}{i}", "maker": f"제조사{i}", "country": "미국",
        "category": "이동식 작업 안전 (라바콘 자동 설치 등)", "summary": f"{prefix}{i} 요약",
        "novelty": "2026-11-12 발표", "novelty_date": "2026-11-12", "in_period": i != 3,
        "benchmark_value": "작업자 안전", "relevance": 10 - i, "relevance_reason": "고속도로 이동식 작업 전용 장비",
        "developed": "2025-06", "developed_basis": "2025-06 제조사 보도자료로 첫 공개", "dev_unknown": False,
        "sources": [{"title": f"보도자료{i}", "url": f"https://maker{i}.com/news",
                                                   "type": "제조사 홈페이지·보도자료"}],
    } for i in (1, 2, 3)]


def fake_report_data(name="장비1"):
    return {
        "name": name, "maker": "제조사1", "country": "미국", "released": "2026-11", "category": "이동식 작업 안전",
        "functions": ["선도 차량을 무인으로 추종함 [1]", "후방 추돌 충격을 흡수함 [1]", "원격 정지 기능을 갖춤 [2]"],
        "specs": ["최고 속도: 40km/h (업체 주장) [1]"],
        "improvements": {"safety": "운전원 추돌 위험 제거 [1]", "efficiency": "인력 1명 절감", "cost": "확인 필요"},
        "domestic": ["이동식 작업 방호차량에 적용 가능함 [2]", "법령 검토 필요"],
        "judgement": "권고", "reasons": ["작업자 안전 효과 큼 [1]", "국내 실증 필요"],
        "sources": [{"title": "제조사 보도자료", "url": "https://maker1.com/news"},
                    {"title": "DOT 보고서", "url": "https://dot.gov/report"}],
        "checks": [{"text": "최고 속도 40km/h", "reason": "업체 주장", "checked": False},
                   {"text": "2026-11 공개", "reason": "날짜 불확실", "checked": False}],
        "usage": {"input": 1, "output": 1},
    }


def wait_status(client, url, busy, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        s = client.get(url).json()
        if s["status"] != busy:
            return s
        time.sleep(0.05)
    raise AssertionError(f"{url} did not finish")


@pytest.fixture
def client(monkeypatch):
    calls = {"research": []}

    def fake_research(settings, start, end, categories, exclude, progress, cancelled):
        calls["research"].append({"start": start, "end": end, "categories": categories, "exclude": exclude})
        progress("가짜 검색 중…")
        names = [c for c in fake_candidates() if c["name"] not in exclude]
        return {"candidates": names, "search_note": "테스트", "dropped": [], "queries": ["q1"],
                "stats": {"searches": 1, "fetches": 0, "input": 10, "output": 5, "web_search": 1}, "notes": "노트"}

    def fake_generate(candidate, settings, progress, cancelled):
        progress("가짜 작성 중…")
        return fake_report_data(candidate["name"]), "사실 노트"

    def fake_images(urls, equipment, desc, stem, judge, client=None, count=2):
        calls.setdefault("photo", []).append(stem.name)
        photos = []
        for i, what in enumerate(["도로 위의 방호차량", "작업 중인 방호차량 후면"]):
            p = stem.with_name(f"{stem.name}_{i}.png")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(make_png(60 + i, 40))
            photos.append({"file": str(p), "page_url": urls[0], "image_url": f"https://maker1.com/{i}.png",
                           "credit": "출처: maker1.com, https://maker1.com/news",
                           "check": {"status": "ok", "what": what}})
        return {"photos": photos, "rejected": [{"url": "https://maker1.com/team.jpg", "what": "연구원 단체 사진"}],
                "note": ""}

    monkeypatch.setattr(app_module.research, "run_research", fake_research)
    monkeypatch.setattr(app_module.report_gen, "generate_report", fake_generate)
    monkeypatch.setattr(app_module.images, "find_equipment_images", fake_images)
    # 업로드 사진 판별도 가짜로 (기본: 장비 사진)
    monkeypatch.setattr(app_module.photo_check, "judge_images",
                        lambda cands, eq, desc: [{"index": 0, "shows_equipment": True, "score": 8, "what": "장비"}])
    monkeypatch.setattr(app_module.docgen, "count_pages", lambda path, timeout=90: 1)
    # 대부분의 테스트는 후보 3개·자동 작성 끔으로 흐름을 하나씩 확인 (자동 작성은 별도 테스트)
    app_module.db.save_settings({"candidate_count": 3, "auto_generate": False})
    c = TestClient(app_module.app)
    c.calls = calls
    return c
