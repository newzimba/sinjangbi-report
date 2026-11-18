"""구글 드라이브 저장: 파일명(작성일_장비키워드), 덮어쓰기·중복 이름, 폴더 없음, [드라이브 저장] 버튼."""
from pathlib import Path
from urllib.parse import unquote

import pytest
from docx import Document

from test_app import editor_form, make_report, start_run
from test_batch import wait_batch
from webapp import db, drive


@pytest.mark.parametrize("d,expected", [
    ({"short_name": "노면동결센서", "name": "x"}, "노면동결센서"),
    ({"short_name": " 포트홀 보수/로봇 ", "name": "x"}, "포트홀보수로봇"),           # 공백·금지 문자 제거
    ({"short_name": "", "name": "원격조종 트럭탑재 방호차(RCTMA)"}, "원격조종트럭탑재방호차"),  # 없으면 장비명에서
    ({"name": "아주아주긴장비이름입니다정말로깁니다"}, "아주아주긴장비이름입니다"),            # 12자까지
    ({"name": "(괄호만)"}, "장비"),
])
def test_장비키워드(d, expected):
    assert drive.keyword(d) == expected


def test_파일명은_작성일_장비키워드():
    assert drive.file_name("2026-11-18T10:21:33", {"short_name": "노면동결센서"}) == "20261118_노면동결센서.docx"


def test_같은1p는_덮어쓰고_다른1p는_번호(tmp_path):
    src1, src2 = tmp_path / "a.docx", tmp_path / "b.docx"
    src1.write_bytes(b"v1")
    src2.write_bytes(b"other")
    folder = tmp_path / "내 드라이브" / "신장비 리포트"
    t1 = drive.save(src1, folder, "20261118_센서.docx")
    assert t1 == folder / "20261118_센서.docx" and t1.read_bytes() == b"v1"
    src1.write_bytes(b"v2")
    assert drive.save(src1, folder, "20261118_센서.docx", previous=str(t1)) == t1 and t1.read_bytes() == b"v2"
    t2 = drive.save(src2, folder, "20261118_센서.docx")       # 다른 1p, 같은 이름
    assert t2.name == "20261118_센서_2.docx" and t1.read_bytes() == b"v2"


def test_내드라이브_자동찾기_설정우선(tmp_path, monkeypatch):
    monkeypatch.setattr(drive, "detect_drive_root", lambda: tmp_path / "내 드라이브")
    assert drive.resolve_folder({}) == tmp_path / "내 드라이브" / "신장비 리포트"
    assert drive.resolve_folder({"drive_folder": str(tmp_path / "x")}) == tmp_path / "x"
    monkeypatch.setattr(drive, "detect_drive_root", lambda: None)
    assert drive.resolve_folder({}) is None


@pytest.fixture
def drive_dir(tmp_path):
    root = tmp_path / "내 드라이브"
    root.mkdir()
    db.save_settings({"drive_folder": str(root / "신장비 리포트")})
    yield root / "신장비 리포트"
    db.save_settings({"drive_folder": ""})


def test_드라이브저장_버튼(client, drive_dir):
    _, rid = make_report(client)
    rep = db.get_report(rid)
    expected = drive.file_name(rep["created_at"], rep["data"])
    assert f"드라이브 파일명: <b>{expected}</b>" in client.get(f"/reports/{rid}").text
    r = client.post(f"/reports/{rid}/save", data=editor_form(client, rid, short_name="방호차", after="drive"),
                    follow_redirects=False)
    name = expected.split("_")[0] + "_방호차.docx"
    assert f"구글 드라이브에 저장했습니다: {name}" in unquote(r.headers["location"])
    saved = drive_dir / name
    assert saved.exists() and any(p.text for p in Document(saved).paragraphs)
    d = db.get_report(rid)["data"]
    assert d["short_name"] == "방호차" and d["drive"]["path"] == str(saved)
    assert "드라이브에 저장됨: " + name in client.get(f"/reports/{rid}").text
    client.post(f"/reports/{rid}/drive")                      # 다시 저장 → 덮어쓰기 (파일 1개 유지)
    assert sorted(p.name for p in drive_dir.iterdir()) == [name]


def test_드라이브폴더_없으면_안내(client, monkeypatch):
    db.save_settings({"drive_folder": ""})
    monkeypatch.setattr(drive, "detect_drive_root", lambda: None)
    _, rid = make_report(client)
    r = client.post(f"/reports/{rid}/drive", follow_redirects=False)
    assert "구글 드라이브 폴더를 찾지 못했습니다" in unquote(r.headers["location"])
    assert "드라이브 폴더 없음" in client.get(f"/reports/{rid}").text


def test_이번주_1p_전부_드라이브저장(client, drive_dir, monkeypatch):
    monkeypatch.setattr("webapp.app.db.reported_names", lambda: [])
    run_id = start_run(client)
    client.post(f"/runs/{run_id}/generate_all")
    wait_batch(run_id, 3)
    assert "드라이브에 3건 저장" in client.get(f"/runs/{run_id}").text
    r = client.post(f"/runs/{run_id}/drive", follow_redirects=False)
    assert "구글 드라이브에 3건 저장했습니다" in unquote(r.headers["location"])
    assert len(list(drive_dir.glob("*.docx"))) == 3
