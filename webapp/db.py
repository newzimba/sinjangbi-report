"""SQLite 저장소: 설정(settings)과 보고 이력(history).

DB 파일: 신장비리포트/data/app.db (처음 실행할 때 자동 생성)
- 설정 초기값은 config/keywords.md에서 읽는다.
- 이력은 data/history.csv(기존 스크립트 결과)를 한 번 옮겨 온다.
"""
import csv
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 테스트에서는 SINJANGBI_DATA 환경 변수로 임시 폴더를 쓴다.
# Vercel에서는 /tmp만 쓸 수 있다 (함수가 다시 시작되면 지워지므로 데이터가 유지되지 않음).
if os.environ.get("SINJANGBI_DATA"):
    DATA_DIR = Path(os.environ["SINJANGBI_DATA"])
elif os.environ.get("VERCEL"):
    DATA_DIR = Path("/tmp/sinjangbi")
else:
    DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "app.db"
IMAGE_DIR = DATA_DIR / "images"
REPORT_DIR = DATA_DIR / "reports"
KEYWORDS_MD = ROOT / "config" / "keywords.md"
HISTORY_CSV = DATA_DIR / "history.csv"

# 공식 자료로 인정하는 출처 유형 (승인된 프롬프트 기준, 화면에서는 읽기 전용)
SOURCE_TYPES = ["제조사 홈페이지·보도자료", "정부·도로기관 발표", "학술·연구 보고서", "전문 언론 기사"]

DEFAULT_SETTINGS = {
    "candidate_count": 5,          # 매주 1p 5건 (2026-11-18 변경: 3개 중 1개 선택 → 5개 모두 작성)
    "default_period_days": 90,     # 최근 3개월 (2026-11-18 변경)
    "auto_generate": True,         # 후보를 찾으면 1p를 자동으로 모두 작성
    # 우선 참고 도메인 (설정 화면에서 수정 가능)
    "preferred_domains": [
        "worldhighways.com", "forconstructionpros.com", "roadsbridges.com", "workzonesafety.org",
        "transportation.gov", "highways.dot.gov", "nationalhighways.co.uk", "mlit.go.jp", "xtech.nikkei.com",
    ],
    # 근거로 쓰지 않는 도메인 (유튜브·블로그·커뮤니티)
    "blocked_domains": [
        "youtube.com", "blog.naver.com", "tistory.com", "medium.com", "reddit.com", "facebook.com",
    ],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    week       TEXT NOT NULL,
    name       TEXT NOT NULL,
    maker      TEXT,
    country    TEXT,
    category   TEXT,
    url        TEXT,
    status     TEXT NOT NULL,          -- 후보 / 1p초안 / 보고완료
    judgement  TEXT,                   -- 권고 / 보류 / 불필요
    file_path  TEXT,
    source     TEXT,                   -- webapp / csv이관
    created_at TEXT NOT NULL,
    UNIQUE (week, name)
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    week        TEXT NOT NULL,
    start       TEXT NOT NULL,
    end         TEXT NOT NULL,
    categories  TEXT NOT NULL,          -- JSON 목록 (빈 목록 = 전체)
    exclude     TEXT NOT NULL,          -- JSON 목록: 제외할 장비명
    status      TEXT NOT NULL,          -- running / done / error / cancelled
    progress    TEXT,
    result      TEXT,                   -- JSON: candidates, search_note, dropped, queries, stats, notes
    error       TEXT,
    selected_id TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL,
    candidate_id TEXT NOT NULL,
    week         TEXT NOT NULL,
    status       TEXT NOT NULL,         -- generating / ready / error / reported
    progress     TEXT,
    error        TEXT,
    data         TEXT,                  -- JSON: 1p 항목 (name, functions, judgement, sources, checks, image …)
    notes        TEXT,                  -- AI 조사 노트 (항목별 다시 생성에 사용)
    pages        INTEGER,               -- 마지막 분량 검사 결과 (NULL = 미검사, 0 = 검사 불가)
    file_path    TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT,
    UNIQUE (run_id, candidate_id)
);
"""


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


# ---------- 초기화 ----------
def parse_keywords_md(text):
    """keywords.md → (분야 목록, 우선 국가 목록)."""
    categories, countries, current, section = [], [], None, None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            title = line[3:].strip()
            if title.startswith("우선 국가"):
                section, current = "countries", None
            else:
                section = "category"
                current = {"name": re.sub(r"^\d+\.\s*", "", title), "keywords": {}}
                categories.append(current)
        elif section == "countries" and line and not line.startswith(">"):
            countries += [c.strip() for c in line.split(",") if c.strip()]
        elif section == "category" and current is not None and line.startswith("- "):
            m = re.match(r"-\s*([A-Za-z]+)\s*:\s*(.+)", line)
            if m:
                current["keywords"][m.group(1).upper()] = [k.strip() for k in m.group(2).split(",") if k.strip()]
    return categories, countries


def init_db():
    with connect() as con:
        con.executescript(SCHEMA)
        if con.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0:
            seed = dict(DEFAULT_SETTINGS)
            cats, countries = ([], [])
            if KEYWORDS_MD.exists():
                cats, countries = parse_keywords_md(KEYWORDS_MD.read_text(encoding="utf-8"))
            seed["categories"] = cats
            seed["countries"] = countries
            for k, v in seed.items():
                con.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (k, json.dumps(v, ensure_ascii=False)))
        if con.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0 and HISTORY_CSV.exists():
            import_history_csv(con)
        # 기존 DB에 보고서 연결 칸 추가 (없을 때만)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(history)")}
        if "report_id" not in cols:
            con.execute("ALTER TABLE history ADD COLUMN report_id INTEGER")
        # 서버가 꺼지면서 멈춘 작업은 오류로 정리
        con.execute("UPDATE runs SET status='error', error='서버가 다시 시작되어 조사가 중단되었습니다. 다시 찾아 주세요.' "
                    "WHERE status='running'")
        con.execute("UPDATE reports SET status='error', error='서버가 다시 시작되어 작성이 중단되었습니다. 다시 생성해 주세요.' "
                    "WHERE status='generating'")


def import_history_csv(con):
    now = datetime.now().isoformat(timespec="seconds")
    with open(HISTORY_CSV, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            con.execute(
                """INSERT OR IGNORE INTO history
                   (week, name, maker, country, category, url, status, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'csv이관', ?)""",
                (row.get("조사주차"), row.get("장비명"), row.get("제조사"), row.get("국가"),
                 row.get("분류"), row.get("출처URL"), row.get("상태") or "후보", now),
            )


# ---------- 설정 ----------
def get_settings():
    with connect() as con:
        rows = con.execute("SELECT key, value FROM settings").fetchall()
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: json.loads(r["value"]) for r in rows})
    return s


def save_settings(values: dict):
    with connect() as con:
        for k, v in values.items():
            con.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, json.dumps(v, ensure_ascii=False)),
            )


# ---------- 이력 ----------
def filtered_history(category="", judgement="", status=""):
    sql, args = "SELECT * FROM history WHERE 1=1", []
    if category:
        sql += " AND category LIKE ?"
        args.append(category.split(" (")[0] + "%")
    if judgement:
        sql += " AND judgement = ?"
        args.append(judgement)
    if status:
        sql += " AND status = ?"
        args.append(status)
    with connect() as con:
        return [dict(r) for r in con.execute(sql + " ORDER BY week DESC, id DESC", args)]


def mark_reported(report):
    """보고 완료: 이력에 저장(같은 주·같은 장비면 갱신)."""
    d = report["data"]
    with connect() as con:
        con.execute(
            """INSERT INTO history (week, name, maker, country, category, url, status, judgement,
                                    file_path, source, created_at, report_id)
               VALUES (?, ?, ?, ?, ?, ?, '보고완료', ?, ?, 'webapp', ?, ?)
               ON CONFLICT(week, name) DO UPDATE SET status='보고완료', judgement=excluded.judgement,
                   file_path=excluded.file_path, report_id=excluded.report_id, maker=excluded.maker,
                   country=excluded.country, category=excluded.category, url=excluded.url""",
            (report["week"], d["name"], d.get("maker"), d.get("country"), d.get("category"),
             (d.get("sources") or [{}])[0].get("url"), d.get("judgement"), report.get("file_path"),
             _now(), report["id"]))
        con.execute("UPDATE reports SET status='reported', updated_at=? WHERE id=?", (_now(), report["id"]))


def recent_history(limit=5):
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM history ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))]


def all_history():
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM history ORDER BY week DESC, id DESC")]


def stats():
    """홈 화면 현황 수치."""
    with connect() as con:
        q = lambda sql: con.execute(sql).fetchone()[0]
        return {
            "reported": q("SELECT COUNT(*) FROM history WHERE status='보고완료'"),
            "recommend": q("SELECT COUNT(*) FROM history WHERE status='보고완료' AND judgement='권고'"),
            "tracked": q("SELECT COUNT(*) FROM history"),
            "runs": q("SELECT COUNT(*) FROM runs WHERE status='done'"),
        }


def reported_names():
    """중복 제외 기준: 보고 완료된 장비명."""
    with connect() as con:
        return [r["name"] for r in con.execute("SELECT name FROM history WHERE status = '보고완료'")]


def batch_summary(week):
    """이번 주 1p 묶음(1p가 있는 가장 최근 조사)의 현황. 없으면 None."""
    with connect() as con:
        row = con.execute("SELECT run_id FROM reports WHERE week=? ORDER BY run_id DESC LIMIT 1", (week,)).fetchone()
    if not row:
        return None
    reps = reports_for_run(row["run_id"])
    count = lambda s: sum(1 for r in reps if r["status"] == s)
    return {"run_id": row["run_id"], "n": len(reps), "generating": count("generating"), "ready": count("ready"),
            "reported": count("reported"), "error": count("error"), "first_id": reps[0]["id"]}


def week_status(week):
    """이번 주 상태: 1p 묶음 현황 > 후보 찾는 중 > 작성 전."""
    run = latest_run(week)
    if run and run["status"] == "running":
        return "후보 찾는 중"
    b = batch_summary(week)
    if b:
        if b["reported"] == b["n"]:
            return "보고 완료"
        if b["generating"]:
            return f"1p 작성 중 ({b['n'] - b['generating']}/{b['n']})"
        return f"1p 검토 중 (보고 {b['reported']}/{b['n']})"
    with connect() as con:
        rows = [r["status"] for r in con.execute("SELECT status FROM history WHERE week = ?", (week,))]
    if "보고완료" in rows:
        return "보고 완료"
    if run and run["status"] == "done":
        return "후보 확인 중"
    return "작성 전"


# ---------- 조사 실행(runs) ----------
def _now():
    return datetime.now().isoformat(timespec="seconds")


def _run_row(r):
    if not r:
        return None
    d = dict(r)
    d["categories"] = json.loads(d["categories"])
    d["exclude"] = json.loads(d["exclude"])
    d["result"] = json.loads(d["result"]) if d["result"] else None
    return d


def create_run(week, start, end, categories, exclude):
    with connect() as con:
        cur = con.execute(
            "INSERT INTO runs (week, start, end, categories, exclude, status, progress, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'running', '준비 중…', ?)",
            (week, start, end, json.dumps(categories, ensure_ascii=False),
             json.dumps(exclude, ensure_ascii=False), _now()))
        return cur.lastrowid


def get_run(run_id):
    with connect() as con:
        return _run_row(con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


def latest_run(week):
    with connect() as con:
        return _run_row(con.execute(
            "SELECT * FROM runs WHERE week = ? ORDER BY id DESC LIMIT 1", (week,)).fetchone())


# ---------- 1p 초안(reports) ----------
def _report_row(r):
    if not r:
        return None
    d = dict(r)
    d["data"] = json.loads(d["data"]) if d["data"] else None
    data = d["data"]
    if data is not None and "images" not in data:  # 예전 형식(사진 1장 "image") → 사진 목록 "images"
        old = data.pop("image", None) or {}
        data["images"] = [{k: v for k, v in old.items() if k not in ("rejected",)}] if old.get("file") else []
        data["image_rejected"] = old.get("rejected", [])
        data["image_note"] = "" if old.get("file") else (old.get("check") or {}).get("what", "")
    return d


def create_report(run_id, candidate_id, week):
    """같은 후보의 초안이 있으면 그 id를 돌려준다 (중복 생성 방지)."""
    with connect() as con:
        row = con.execute("SELECT id FROM reports WHERE run_id=? AND candidate_id=?",
                          (run_id, candidate_id)).fetchone()
        if row:
            return row["id"], False
        cur = con.execute(
            "INSERT INTO reports (run_id, candidate_id, week, status, progress, created_at) "
            "VALUES (?, ?, ?, 'generating', '준비 중…', ?)", (run_id, candidate_id, week, _now()))
        return cur.lastrowid, True


def reports_for_run(run_id):
    """한 조사에서 나온 1p들 (후보 순서대로)."""
    with connect() as con:
        rows = [_report_row(r) for r in con.execute("SELECT * FROM reports WHERE run_id=?", (run_id,))]
    return sorted(rows, key=lambda r: int(r["candidate_id"].lstrip("c") or 0))


def get_report(report_id):
    with connect() as con:
        return _report_row(con.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone())


def latest_report(week):
    with connect() as con:
        return _report_row(con.execute(
            "SELECT * FROM reports WHERE week=? ORDER BY updated_at DESC, id DESC LIMIT 1", (week,)).fetchone())


def update_report(report_id, **fields):
    if "data" in fields and fields["data"] is not None:
        fields["data"] = json.dumps(fields["data"], ensure_ascii=False)
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE reports SET {cols} WHERE id = ?", (*fields.values(), report_id))


def update_run(run_id, **fields):
    if "result" in fields and fields["result"] is not None:
        fields["result"] = json.dumps(fields["result"], ensure_ascii=False)
    if fields.get("status") in ("done", "error", "cancelled"):
        fields["finished_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))
