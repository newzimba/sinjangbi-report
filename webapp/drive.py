"""구글 드라이브 저장 (데스크톱용 Google Drive 동기화 폴더 사용).

데스크톱용 Google Drive를 설치·로그인하면 PC에 '내 드라이브'(보통 G:\\내 드라이브)가 생긴다.
웹앱은 그 안의 「신장비 리포트」 폴더에 1p 워드 파일을 복사하고, 업로드는 Google Drive 프로그램이 한다.

파일명: 작성일_장비키워드.docx  (예: 20261118_노면동결센서.docx)
"""
import re
import shutil
import string
from datetime import datetime
from pathlib import Path

SUBFOLDER = "신장비 리포트"
ROOT_NAMES = ("내 드라이브", "My Drive")
KEYWORD_MAX = 12


def detect_drive_root():
    """데스크톱용 Google Drive의 '내 드라이브' 위치를 찾는다. 없으면 None."""
    candidates = [Path(f"{d}:\\") for d in string.ascii_uppercase]
    candidates.append(Path.home() / "Google Drive")
    for base in candidates:
        for name in ROOT_NAMES:
            p = base / name
            try:
                if p.is_dir():
                    return p
            except OSError:
                continue
    return None


def resolve_folder(settings):
    """저장 폴더: 설정에 직접 지정한 폴더 > 자동으로 찾은 '내 드라이브\\신장비 리포트'. 없으면 None."""
    custom = (settings.get("drive_folder") or "").strip()
    if custom:
        return Path(custom)
    root = detect_drive_root()
    return root / SUBFOLDER if root else None


def keyword(d):
    """파일명에 쓸 장비 키워드: AI가 만든 short_name, 없으면 장비명에서 괄호·공백을 뺀 앞부분."""
    raw = (d.get("short_name") or "").strip()
    if not raw:
        raw = re.sub(r"[(（\[].*?[)）\]]", "", d.get("name") or "")
    kw = re.sub(r'[\\/:*?"<>|\s·,.\-_]+', "", raw)
    return kw[:KEYWORD_MAX] or "장비"


def file_name(created_at, d):
    """작성일(YYYYMMDD)_장비키워드.docx"""
    try:
        day = datetime.fromisoformat(created_at).strftime("%Y%m%d")
    except (TypeError, ValueError):
        day = datetime.now().strftime("%Y%m%d")
    return f"{day}_{keyword(d)}.docx"


def save(src, folder, name, previous=None):
    """워드 파일을 드라이브 폴더에 복사한다.

    같은 1p를 다시 저장하면(previous와 같은 이름) 덮어쓰고, 다른 1p와 이름이 겹치면 _2, _3을 붙인다.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    if target.exists() and str(target) != str(previous or ""):
        stem, n = target.stem, 2
        while (folder / f"{stem}_{n}.docx").exists() and str(folder / f"{stem}_{n}.docx") != str(previous or ""):
            n += 1
        target = folder / f"{stem}_{n}.docx"
    shutil.copy2(src, target)
    return target
