"""신장비 리포트 웹앱 — MVP F1~F10.

실행: 신장비리포트 폴더의 run.bat 더블클릭
      또는  python -m uvicorn --app-dir 신장비리포트 webapp.app:app --port 8000
"""
import csv
import io
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import crosscheck, db, docgen, drive, images, photo_check, report_gen, research
from .claude_client import friendly_error

BASE = Path(__file__).resolve().parent
LANGS = ["EN", "JP", "DE"]

app = FastAPI(title="신장비 리포트")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

db.init_db()


def cite(text, sources):
    """F7 문장 끝 [n] → 참고 자료 n번 링크 (나머지 글자는 안전하게 이스케이프)."""
    from markupsafe import Markup, escape

    def rep(m):
        i = int(m.group(1))
        if 1 <= i <= len(sources or []):
            url = escape(sources[i - 1].get("url", ""))
            return f'<sup><a href="{url}" target="_blank" rel="noopener" title="{url}">[{i}]</a></sup>'
        return f'<sup class="bad" title="없는 출처 번호">[{i}]</sup>'
    return Markup(report_gen.MARK_RE.sub(rep, str(escape(text or ""))))


templates.env.filters["cite"] = cite
templates.env.filters["plain"] = docgen.plain
templates.env.globals["split_credit"] = docgen.split_credit


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def split_list(text: str, sep=","):
    return [x.strip() for x in text.replace("\r", "").split(sep) if x.strip()]


# ---------- S1 홈 ----------
@app.get("/")
def home(request: Request, msg: str = ""):
    s = db.get_settings()
    today = date.today()
    week = iso_week(today)
    return templates.TemplateResponse(request, "home.html", {
        "active": "home",
        "week": week,
        "status": db.week_status(week),
        "start": (today - timedelta(days=s["default_period_days"])).isoformat(),
        "end": today.isoformat(),
        "categories": [c["name"] for c in s.get("categories", [])],
        "candidate_count": s["candidate_count"],
        "recent": db.recent_history(5),
        "latest": db.latest_run(week),
        "batch": db.batch_summary(week),
        "stats": db.stats(),
        "period_days": s["default_period_days"],
        "msg": msg,
    })


CANCEL = set()  # 취소 요청된 run id


def _worker(run_id):
    run = db.get_run(run_id)
    try:
        result = research.run_research(
            db.get_settings(), run["start"], run["end"], run["categories"], run["exclude"],
            progress=lambda text: db.update_run(run_id, progress=text),
            cancelled=lambda: run_id in CANCEL,
        )
        db.update_run(run_id, status="done", result=result, progress="완료")
        if db.get_settings().get("auto_generate", True):
            _generate_all(run_id)  # 후보 5개 모두 1p 작성 (2개씩 동시에)
    except InterruptedError as e:
        db.update_run(run_id, status="cancelled", error=str(e))
    except Exception as e:  # 화면에 알릴 수 있도록 모든 오류를 기록
        db.update_run(run_id, status="error", error=friendly_error(e))
    finally:
        CANCEL.discard(run_id)


def _start_run(start, end, categories, exclude):
    run_id = db.create_run(iso_week(date.today()), start, end, categories, exclude)
    threading.Thread(target=_worker, args=(run_id,), daemon=True).start()
    return run_id


def _valid_period(start, end):
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError):
        return False
    return s <= e <= date.today() + timedelta(days=1)


@app.post("/search")
async def search(request: Request):
    form = await request.form()
    start, end = form.get("start"), form.get("end")
    if not _valid_period(start, end):
        return RedirectResponse("/?msg=조사 기간을 확인하세요 (시작일 ≤ 종료일, 종료일은 오늘 이전).", status_code=303)
    run_id = _start_run(start, end, form.getlist("categories"), db.reported_names())
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


# ---------- S2 진행 / S3 후보 선택 ----------
@app.get("/runs/{run_id}")
def run_page(request: Request, run_id: int, msg: str = ""):
    run = db.get_run(run_id)
    if not run:
        return RedirectResponse("/?msg=조사 기록을 찾을 수 없습니다.", status_code=303)
    s = db.get_settings()
    reps = {r["candidate_id"]: r for r in db.reports_for_run(run_id)}
    ctx = {"active": "home", "run": run, "msg": msg,
           "categories": [c["name"] for c in s.get("categories", [])],
           "count": s["candidate_count"], "domain": research._norm_domain, "reps": reps,
           "busy": any(r["status"] == "generating" for r in reps.values()),
           "ready_n": sum(1 for r in reps.values() if r["status"] in ("ready", "reported"))}
    page = "candidates.html" if run["status"] == "done" else "progress.html"
    return templates.TemplateResponse(request, page, ctx)


@app.post("/runs/{run_id}/generate_all")
def run_generate_all(run_id: int):
    """후보 전부 1p 작성 (아직 없는 것만)."""
    run = db.get_run(run_id)
    if not run or run["status"] != "done":
        return RedirectResponse(f"/runs/{run_id}", status_code=303)
    n = _generate_all(run_id)
    msg = f"1p {n}건 작성을 시작했습니다 (2건씩)." if n else "모든 후보의 1p가 이미 있습니다."
    return RedirectResponse(f"/runs/{run_id}?msg={msg}", status_code=303)


@app.get("/runs/{run_id}/download.zip")
def run_download_zip(run_id: int):
    """이번 주 1p 워드 파일을 한 번에 (작성이 끝난 것만)."""
    reps = [r for r in db.reports_for_run(run_id) if r["status"] in ("ready", "reported") and r["data"]]
    if not reps:
        return RedirectResponse(f"/runs/{run_id}?msg=내려받을 1p가 아직 없습니다.", status_code=303)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, r in enumerate(reps, 1):
            path = _build_file(r)
            z.write(path, arcname=f"{i}_{path.name}")
    week = reps[0]["week"]
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{week}_1p_{len(reps)}.zip"})


@app.get("/runs/{run_id}/status")
def run_status(run_id: int):
    run = db.get_run(run_id)
    if not run:
        return JSONResponse({"status": "missing"}, status_code=404)
    return {"status": run["status"], "progress": run["progress"], "error": run["error"]}


@app.post("/runs/{run_id}/cancel")
def run_cancel(run_id: int):
    CANCEL.add(run_id)
    db.update_run(run_id, progress="취소하는 중…")
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/retry")
async def run_retry(request: Request, run_id: int):
    """다른 후보 찾기: 이번에 나온 후보를 제외하고, 바꾼 기간·분야로 다시 조사."""
    prev = db.get_run(run_id)
    form = await request.form()
    start, end = form.get("start") or prev["start"], form.get("end") or prev["end"]
    if not _valid_period(start, end):
        return RedirectResponse(f"/runs/{run_id}?msg=조사 기간을 확인하세요.", status_code=303)
    exclude = list(prev["exclude"])
    if prev["result"]:
        exclude += [c["name"] for c in prev["result"]["candidates"]]
    new_id = _start_run(start, end, form.getlist("categories"), sorted(set(exclude)))
    return RedirectResponse(f"/runs/{new_id}", status_code=303)


@app.post("/runs/{run_id}/select")
async def run_select(request: Request, run_id: int):
    """F3: 후보 선택 → 1p 초안 생성 시작 (같은 후보면 기존 초안으로 이동)."""
    form = await request.form()
    cid = form.get("candidate_id")
    run = db.get_run(run_id)
    if not run or not run["result"] or cid not in {c["id"] for c in run["result"]["candidates"]}:
        return RedirectResponse(f"/runs/{run_id}?msg=후보를 다시 선택하세요.", status_code=303)
    db.update_run(run_id, selected_id=cid)
    rid, created = db.create_report(run_id, cid, run["week"])
    if created:
        _start_report(rid)
    return RedirectResponse(f"/reports/{rid}", status_code=303)


# ---------- S4 1p 생성·편집 (F3~F8) ----------
REPORT_CANCEL = set()


def _candidate(report):
    run = db.get_run(report["run_id"])
    return next(c for c in run["result"]["candidates"] if c["id"] == report["candidate_id"])


def _report_worker(rid):
    rep = db.get_report(rid)
    try:
        cand = _candidate(rep)
        data, notes = report_gen.generate_report(
            cand, db.get_settings(),
            progress=lambda t: db.update_report(rid, progress=t),
            cancelled=lambda: rid in REPORT_CANCEL)
        db.update_report(rid, progress="장비 사진 2장을 찾고 AI로 확인하는 중…")
        _apply_photos(data, _find_photo(rid, data, cand))
        db.update_report(rid, data=data, notes=notes, progress="A4 1장인지 확인하는 중…")
        pages = _fit_one_page(rid, auto=True)
        db.update_report(rid, status="ready", pages=pages, progress="완료")
    except InterruptedError as e:
        db.update_report(rid, status="error", error=str(e))
    except Exception as e:
        db.update_report(rid, status="error", error=friendly_error(e))
    finally:
        REPORT_CANCEL.discard(rid)


def _find_photo(rid, data, cand, attempt=""):
    """F4: 출처 페이지 사진 후보를 모아 AI가 장비 사진인지 확인하고, 서로 다른 장비 사진 2장을 고른다."""
    pages = [s["url"] for s in data.get("sources", [])] + [s["url"] for s in cand["sources"]]
    desc = "; ".join(data.get("functions", [])[:2]) or cand.get("summary", "")
    return images.find_equipment_images(list(dict.fromkeys(pages)), data.get("name") or cand["name"], desc,
                                        db.IMAGE_DIR / f"report_{rid}{attempt}", judge=photo_check.judge_images)


def _apply_photos(data, found):
    data["images"] = found["photos"]
    data["image_rejected"] = found["rejected"]
    data["image_note"] = found["note"]
    return data


def _slot(value):
    try:
        s = int(value)
    except (TypeError, ValueError):
        return None
    return s if 0 <= s < images.PHOTO_COUNT else None


GEN_POOL = ThreadPoolExecutor(max_workers=2)  # 1p 작성은 2건씩 동시에 (API 사용량 한도 고려)


def _start_report(rid):
    db.update_report(rid, progress="차례를 기다리는 중… (2건씩 작성)")
    GEN_POOL.submit(_report_worker, rid)


def _generate_all(run_id):
    """조사 결과의 후보 전부에 대해 1p 작성을 시작한다 (이미 있는 1p는 건너뜀). 새로 시작한 수를 돌려준다."""
    run = db.get_run(run_id)
    started = 0
    for c in (run.get("result") or {}).get("candidates", []):
        rid, created = db.create_report(run_id, c["id"], run["week"])
        if created:
            _start_report(rid)
            started += 1
    return started


def _lines(text):
    return [x.strip() for x in (text or "").replace("\r", "").split("\n") if x.strip()]


def form_to_data(form, old):
    """편집 화면 입력 → 1p 데이터. 오류가 있으면 (None, 메시지)."""
    judgement = form.get("judgement", "")
    if judgement not in report_gen.JUDGEMENTS:
        return None, "판단은 권고/보류/불필요 중 하나여야 합니다."
    sources = []
    for line in _lines(form.get("sources")):
        title, _, url = line.rpartition("|")
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            return None, f"참고 자료는 '제목 | https://주소' 형식으로 입력하세요: {line[:40]}"
        sources.append({"title": title.strip() or research._norm_domain(url), "url": url})
    checks = []
    for i, c in enumerate(old.get("checks", [])):
        checks.append({**c, "checked": form.get(f"check_{i}") == "on"})
    photos = [dict(p) for p in old.get("images", [])]
    for i, p in enumerate(photos):
        p["credit"] = (form.get(f"image_credit_{i}") or p.get("credit", "")).strip()
    data = {
        **old,
        "name": form.get("name", "").strip(), "maker": form.get("maker", "").strip(),
        "short_name": form.get("short_name", old.get("short_name", "")).strip(),
        "country": form.get("country", "").strip(), "released": form.get("released", "").strip(),
        "category": form.get("category", "").strip(),
        "functions": _lines(form.get("functions")), "specs": _lines(form.get("specs")),
        "improvements": {k: form.get(f"imp_{k}", "").strip() for k in ("safety", "efficiency", "cost")},
        "domestic": _lines(form.get("domestic")), "judgement": judgement,
        "reasons": _lines(form.get("reasons")), "sources": sources, "checks": checks, "images": photos,
    }
    return data, None


def empty_sections(d):
    names = {"name": "장비명", "functions": "주요 기능", "domestic": "국내 적용성", "reasons": "판단 근거",
             "sources": "참고 자료"}
    out = [label for k, label in names.items() if not d.get(k)]
    if not any((d.get("improvements") or {}).values()):
        out.append("개선점")
    n = len([p for p in d.get("images", []) if p.get("file")])
    if n < images.PHOTO_COUNT:
        out.append(f"사진({n}/{images.PHOTO_COUNT}장)")
    return out


def _report_ctx(rep, msg="", error=""):
    d = rep["data"] or {}
    checks = d.get("checks", [])
    return {"active": "home", "rep": rep, "d": d, "msg": msg, "error": error,
            "judgements": report_gen.JUDGEMENTS, "sections": report_gen.SECTIONS,
            "empty": empty_sections(d) if d else [], "domain": research._norm_domain,
            "unchecked": sum(1 for c in checks if not c.get("checked")),
            "xjob": XCHECK.get(rep["id"], {}),
            "file_name": docgen.file_name(rep["week"], d.get("name", "")),
            "siblings": db.reports_for_run(rep["run_id"]),  # 같은 주 1p 묶음 (탭 이동용)
            "drive_name": drive.file_name(rep["created_at"], d) if d else "",
            "drive_kw": drive.keyword(d) if d else "",
            "drive_folder": drive.resolve_folder(db.get_settings())}


@app.get("/reports/{rid}")
def report_page(request: Request, rid: int, msg: str = "", error: str = ""):
    rep = db.get_report(rid)
    if not rep:
        return RedirectResponse("/?msg=1p 초안을 찾을 수 없습니다.", status_code=303)
    page = "editor.html" if rep["status"] in ("ready", "reported") else "report_progress.html"
    return templates.TemplateResponse(request, page, _report_ctx(rep, msg, error))


@app.get("/reports/{rid}/status")
def report_status(rid: int):
    rep = db.get_report(rid)
    if not rep:
        return JSONResponse({"status": "missing"}, status_code=404)
    return {"status": rep["status"], "progress": rep["progress"], "error": rep["error"]}


@app.post("/reports/{rid}/cancel")
def report_cancel(rid: int):
    REPORT_CANCEL.add(rid)
    db.update_report(rid, progress="취소하는 중…")
    return RedirectResponse(f"/reports/{rid}", status_code=303)


@app.post("/reports/{rid}/regenerate")
def report_regenerate(rid: int):
    rep = db.get_report(rid)
    if rep and rep["status"] in ("error", "ready"):
        db.update_report(rid, status="generating", error=None, progress="다시 생성 준비 중…")
        _start_report(rid)
    return RedirectResponse(f"/reports/{rid}", status_code=303)


@app.post("/reports/{rid}/save")
async def report_save(rid: int, request: Request):
    rep = db.get_report(rid)
    if not rep or not rep["data"]:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    data, err = form_to_data(form, rep["data"])
    if err:
        return RedirectResponse(f"/reports/{rid}?error={err}", status_code=303)
    db.update_report(rid, data=data, pages=None)
    after = form.get("after", "")  # 저장 후 이어서 할 일 (Word 실행은 오래 걸려 별도 스레드에서)
    if after == "pages":
        return await run_in_threadpool(report_pages, rid)
    if after == "download":
        return RedirectResponse(f"/reports/{rid}/download", status_code=303)
    if after == "complete":
        return await run_in_threadpool(report_complete, rid)
    if after == "drive":
        return await run_in_threadpool(report_drive, rid)
    warn = empty_sections(data)
    msg = "저장했습니다." + (f" 빈 항목: {', '.join(warn)}" if warn else "")
    return RedirectResponse(f"/reports/{rid}?msg={msg}", status_code=303)


@app.post("/reports/{rid}/preview", response_class=HTMLResponse)
async def report_preview(rid: int, request: Request):
    """F5 실시간 미리보기: 저장하지 않고 입력값으로 A4 미리보기만 다시 그린다."""
    rep = db.get_report(rid)
    data, err = form_to_data(await request.form(), rep["data"])
    if err:
        return HTMLResponse(f'<div class="notice err">{err}</div>')
    return templates.TemplateResponse(request, "_preview.html", {**_report_ctx(rep), "d": data})


@app.post("/reports/{rid}/section/{section}")
def report_section(rid: int, section: str):
    rep = db.get_report(rid)
    if not rep or not rep["data"] or section not in report_gen.SECTIONS:
        return RedirectResponse(f"/reports/{rid}?error=다시 생성할 수 없는 항목입니다.", status_code=303)
    try:
        out = report_gen.regenerate_section(section, rep["data"], rep["notes"] or "")
    except Exception as e:
        return RedirectResponse(f"/reports/{rid}?error={friendly_error(e)}", status_code=303)
    db.update_report(rid, data={**rep["data"], **out}, pages=None)
    return RedirectResponse(f"/reports/{rid}?msg=「{report_gen.SECTIONS[section]}」을(를) 다시 생성했습니다.",
                            status_code=303)


@app.post("/reports/{rid}/image/{slot}")
async def report_image(rid: int, slot: str, file: UploadFile):
    """사진 직접 올리기: 사진 1 또는 사진 2 자리에 넣는다."""
    rep = db.get_report(rid)
    s = _slot(slot)
    if s is None:
        return RedirectResponse(f"/reports/{rid}?error=사진 자리를 다시 선택하세요.", status_code=303)
    data = await file.read(images.MAX_BYTES + 1)
    kind, err = images.validate_upload(data)
    if err:
        return RedirectResponse(f"/reports/{rid}?error={err}", status_code=303)
    path = db.IMAGE_DIR / f"report_{rid}_upload_{s}_{int(time.time() * 1000)}.{kind}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    d = rep["data"]
    check = {"status": "unverified", "what": "직접 올린 사진 — AI 확인 안 됨"}
    if len(data) <= images.JUDGE_MAX_BYTES:  # 올린 사진도 장비 사진인지 AI가 확인 (막지는 않고 경고만)
        try:
            v = (await run_in_threadpool(photo_check.judge_images, [{"data": data, "kind": kind}],
                                         d.get("name", ""), "; ".join(d.get("functions", [])[:2])))[0]
            check = {"status": "ok" if v["shows_equipment"] else "warn", "what": v["what"]}
        except Exception:
            pass
    photo = {"file": str(path), "page_url": "", "credit": "출처: (직접 입력하세요)", "check": check}
    photos = [p for p in d.get("images", []) if p.get("file")]
    if s < len(photos):
        photos[s] = photo
    else:
        photos.append(photo)
    d["images"] = photos
    db.update_report(rid, data=d, pages=None)
    if check["status"] == "warn":
        return RedirectResponse(f"/reports/{rid}?error=올린 사진이 장비 사진이 아닌 것 같습니다: {check['what']}#photos",
                                status_code=303)
    return RedirectResponse(f"/reports/{rid}?msg=사진 {min(s, len(photos) - 1) + 1}을(를) 교체했습니다. "
                            f"사진 아래 출처를 입력하세요.#photos", status_code=303)


# ---------- 교차 검증 (다른 출처로 사실 확인 → 틀린 내용 수정) ----------
XCHECK = {}  # rid → {"status": running/done/error, "progress", "error"}


def _xcheck_worker(rid):
    try:
        rep = db.get_report(rid)
        fixed = crosscheck.cross_check(rep["data"], db.get_settings(),
                                       progress=lambda t: XCHECK[rid].update(progress=t))
        db.update_report(rid, data=fixed, pages=None)
        XCHECK[rid].update(progress="수정 후 A4 1장인지 확인하는 중…")
        _fit_one_page(rid)
        XCHECK[rid].update(status="done")
    except Exception as e:
        XCHECK[rid].update(status="error", error=friendly_error(e))


@app.post("/reports/{rid}/crosscheck")
def report_crosscheck(rid: int):
    rep = db.get_report(rid)
    if not rep or not rep["data"]:
        return RedirectResponse("/", status_code=303)
    if XCHECK.get(rid, {}).get("status") != "running":
        XCHECK[rid] = {"status": "running", "progress": "준비 중…", "error": None}
        threading.Thread(target=_xcheck_worker, args=(rid,), daemon=True).start()
    return RedirectResponse(f"/reports/{rid}#xcheck", status_code=303)


@app.get("/reports/{rid}/crosscheck/status")
def report_crosscheck_status(rid: int):
    return XCHECK.get(rid, {"status": "none", "progress": "", "error": None})


@app.post("/reports/{rid}/photos/refind")
def report_image_refind(rid: int):
    """사진 다시 찾기: 출처 페이지의 사진 후보를 AI로 다시 확인해 장비 사진으로 교체."""
    rep = db.get_report(rid)
    if not rep or not rep["data"]:
        return RedirectResponse("/", status_code=303)
    try:
        found = _find_photo(rid, rep["data"], _candidate(rep), attempt=f"_{int(time.time())}")
    except Exception as e:
        return RedirectResponse(f"/reports/{rid}?error={friendly_error(e)}", status_code=303)
    d = _apply_photos(rep["data"], found)
    db.update_report(rid, data=d, pages=None)
    n = len(found["photos"])
    if n == images.PHOTO_COUNT:
        return RedirectResponse(f"/reports/{rid}?msg=장비 사진 {n}장으로 교체했습니다.#photos", status_code=303)
    if n:
        return RedirectResponse(f"/reports/{rid}?error={found['note']}#photos", status_code=303)
    return RedirectResponse(f"/reports/{rid}?error=장비 사진을 찾지 못했습니다({found['note']}). 직접 올려 주세요.#photos",
                            status_code=303)


@app.post("/reports/{rid}/image/{slot}/delete")
def report_image_delete(rid: int, slot: str):
    rep = db.get_report(rid)
    s = _slot(slot)
    d = rep["data"]
    photos = d.get("images", [])
    if s is None or s >= len(photos):
        return RedirectResponse(f"/reports/{rid}?error=지울 사진이 없습니다.#photos", status_code=303)
    photos.pop(s)
    db.update_report(rid, data=d, pages=None)
    return RedirectResponse(f"/reports/{rid}?msg=사진 {s + 1}을(를) 삭제했습니다.#photos", status_code=303)


@app.get("/reports/{rid}/image/{slot}")
def report_image_file(rid: int, slot: str):
    rep = db.get_report(rid)
    s = _slot(slot)
    photos = ((rep or {}).get("data") or {}).get("images", [])
    f = photos[s].get("file") if s is not None and s < len(photos) else None
    if not f or not Path(f).exists():
        return Response(status_code=404)
    return FileResponse(f)


def _build_file(rep):
    path = db.REPORT_DIR / docgen.file_name(rep["week"], rep["data"].get("name"))
    docgen.build_docx(rep["data"], rep["week"], path)
    db.update_report(rep["id"], file_path=str(path))
    return path


def _fit_one_page(rid, auto=False):
    """F6: 페이지 수를 세고 1장을 넘으면 AI로 한 번 줄인 뒤 다시 센다. 최종 페이지 수(0=검사 불가)."""
    rep = db.get_report(rid)
    n = docgen.count_pages(_build_file(rep))
    if n and n > 1:
        if auto:
            db.update_report(rid, progress=f"{n}페이지 → 1장에 맞게 줄이는 중…")
        out = report_gen.shorten_report(rep["data"], n)
        db.update_report(rid, data={**rep["data"], **out})
        n = docgen.count_pages(_build_file(db.get_report(rid)))
    db.update_report(rid, pages=n or 0)
    return n or 0


@app.post("/reports/{rid}/shorten")
def report_shorten(rid: int):
    """AI로 1장에 맞게 줄이기 (버튼)."""
    try:
        n = _fit_one_page(rid)
    except Exception as e:
        return RedirectResponse(f"/reports/{rid}?error={friendly_error(e)}", status_code=303)
    if n == 1:
        return RedirectResponse(f"/reports/{rid}?msg=A4 1장에 맞췄습니다 ✓ 줄어든 문장을 확인하세요.", status_code=303)
    if n == 0:
        return RedirectResponse(f"/reports/{rid}?error=페이지 수를 확인하지 못했습니다(MS Word 없음 또는 실행 실패).",
                                status_code=303)
    return RedirectResponse(f"/reports/{rid}?error=줄였지만 아직 {n}페이지입니다. 사진을 지우거나 문장을 직접 줄이세요.",
                            status_code=303)


@app.post("/reports/{rid}/pages")
def report_pages(rid: int):
    """F6 분량 검사: 워드 파일을 만들어 MS Word로 페이지 수를 센다."""
    rep = db.get_report(rid)
    n = docgen.count_pages(_build_file(rep))
    db.update_report(rid, pages=n or 0)
    if not n:
        return RedirectResponse(f"/reports/{rid}?error=페이지 수를 확인하지 못했습니다(MS Word 없음 또는 실행 실패).",
                                status_code=303)
    if n > 1:
        return RedirectResponse(f"/reports/{rid}?error={n}페이지입니다. 문장을 줄이거나 사진을 작게 하세요.",
                                status_code=303)
    return RedirectResponse(f"/reports/{rid}?msg=A4 1장입니다 ✓", status_code=303)


# ---------- 구글 드라이브 저장 ----------
DRIVE_MISSING = ("구글 드라이브 폴더를 찾지 못했습니다. 데스크톱용 Google Drive를 설치·로그인하거나 "
                 "설정에서 저장 폴더를 지정하세요.")


def _drive_save(rep):
    """1p를 드라이브 폴더에 저장. (저장 경로, 오류 메시지)"""
    folder = drive.resolve_folder(db.get_settings())
    if folder is None or not folder.parent.exists():
        return None, DRIVE_MISSING
    src = _build_file(rep)
    prev = (rep["data"].get("drive") or {}).get("path")
    try:
        target = drive.save(src, folder, drive.file_name(rep["created_at"], rep["data"]), previous=prev)
    except OSError as e:
        return None, f"드라이브 폴더에 저장하지 못했습니다: {e}"
    d = db.get_report(rep["id"])["data"]
    d["drive"] = {"path": str(target), "name": target.name, "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    db.update_report(rep["id"], data=d)
    return target, None


@app.post("/reports/{rid}/drive")
def report_drive(rid: int):
    """[드라이브 저장] 버튼: 작성일_장비키워드.docx 로 구글 드라이브 폴더에 저장."""
    rep = db.get_report(rid)
    if not rep or not rep["data"]:
        return RedirectResponse("/", status_code=303)
    target, err = _drive_save(rep)
    if err:
        return RedirectResponse(f"/reports/{rid}?error={err}", status_code=303)
    return RedirectResponse(f"/reports/{rid}?msg=구글 드라이브에 저장했습니다: {target.name}", status_code=303)


@app.post("/runs/{run_id}/drive")
def run_drive(run_id: int):
    """이번 주 1p를 모두 드라이브에 저장 (작성이 끝난 것만)."""
    reps = [r for r in db.reports_for_run(run_id) if r["status"] in ("ready", "reported") and r["data"]]
    if not reps:
        return RedirectResponse(f"/runs/{run_id}?msg=저장할 1p가 아직 없습니다.", status_code=303)
    names = []
    for r in reps:
        target, err = _drive_save(r)
        if err:
            return RedirectResponse(f"/runs/{run_id}?msg={err}", status_code=303)
        names.append(target.name)
    return RedirectResponse(f"/runs/{run_id}?msg=구글 드라이브에 {len(names)}건 저장했습니다: {', '.join(names)}",
                            status_code=303)


@app.get("/reports/{rid}/download")
def report_download(rid: int):
    rep = db.get_report(rid)
    if not rep or not rep["data"]:
        return RedirectResponse("/", status_code=303)
    path = _build_file(rep)
    return FileResponse(path, filename=path.name,
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.post("/reports/{rid}/complete")
def report_complete(rid: int):
    """F9 보고 완료: 최신 내용으로 파일을 만들고 이력에 저장."""
    rep = db.get_report(rid)
    if not rep or not rep["data"]:
        return RedirectResponse("/", status_code=303)
    path = _build_file(rep)
    rep = db.get_report(rid)
    rep["file_path"] = str(path)
    db.mark_reported(rep)
    return RedirectResponse(f"/reports/{rid}?msg=보고 완료로 저장했습니다. 다음 조사부터 이 장비는 제외됩니다.",
                            status_code=303)


# ---------- S5 이력 (1단계: 읽기 전용) ----------
@app.get("/history")
def history(request: Request, category: str = "", judgement: str = "", status: str = ""):
    s = db.get_settings()
    return templates.TemplateResponse(request, "history.html", {
        "active": "history", "rows": db.filtered_history(category, judgement, status),
        "categories": [c["name"].split(" (")[0] for c in s.get("categories", [])],
        "judgements": report_gen.JUDGEMENTS, "f": {"category": category, "judgement": judgement, "status": status},
    })


@app.get("/history.csv")
def history_csv(category: str = "", judgement: str = "", status: str = ""):
    """이력 내보내기: 엑셀에서 한글이 깨지지 않도록 BOM 포함 CSV."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["주차", "장비명", "제조사", "국가", "분류", "상태", "판단", "출처URL"])
    for r in db.filtered_history(category, judgement, status):
        w.writerow([r["week"], r["name"], r["maker"], r["country"], r["category"], r["status"],
                    r["judgement"] or "", r["url"] or ""])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''sinjangbi_history.csv"})


# ---------- S6 설정 ----------
@app.get("/settings")
def settings_page(request: Request, saved: int = 0, error: str = ""):
    return templates.TemplateResponse(request, "settings.html", {
        "active": "settings", "s": db.get_settings(), "langs": LANGS,
        "source_types": db.SOURCE_TYPES, "saved": saved, "error": error,
        "drive_root": drive.detect_drive_root(), "drive_folder": drive.resolve_folder(db.get_settings()),
        "drive_sub": drive.SUBFOLDER,
    })


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    try:
        count = int(form.get("candidate_count", 3))
        days = int(form.get("default_period_days", 7))
        if not (1 <= count <= 10 and 1 <= days <= 365):
            raise ValueError
    except ValueError:
        return RedirectResponse("/settings?error=후보 개수는 1~10, 조사 기간은 1~365일로 입력하세요.", status_code=303)

    categories = []
    idx = 0
    while f"cat_name_{idx}" in form:
        name = form.get(f"cat_name_{idx}", "").strip()
        if name:  # 이름을 비우면 그 분야는 삭제
            kw = {lang: split_list(form.get(f"cat_{idx}_{lang}", "")) for lang in LANGS}
            categories.append({"name": name, "keywords": {k: v for k, v in kw.items() if v}})
        idx += 1

    db.save_settings({
        "candidate_count": count,
        "default_period_days": days,
        "auto_generate": form.get("auto_generate") == "on",
        "drive_folder": (form.get("drive_folder") or "").strip(),
        "countries": split_list(form.get("countries", "")),
        "categories": categories,
        "preferred_domains": split_list(form.get("preferred_domains", ""), sep="\n"),
        "blocked_domains": split_list(form.get("blocked_domains", ""), sep="\n"),
    })
    return RedirectResponse("/settings?saved=1", status_code=303)
