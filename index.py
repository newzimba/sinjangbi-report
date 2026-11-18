"""Vercel 진입점: FastAPI 앱(webapp/app.py)의 `app`을 그대로 내보낸다.

PC에서는 run.bat(uvicorn)으로 실행하고, Vercel에서는 이 파일을 함수 진입점으로 쓴다.
"""
from webapp.app import app  # noqa: F401
