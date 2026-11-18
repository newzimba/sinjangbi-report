# 신장비 리포트

한국도로공사 첨단기계화센터 — 해외 도로 유지관리 신장비 주간 동향 1p 작성 웹앱 (내부 업무용).

- 최근 3개월 소식 · 최근 3년 이내 개발 장비를 다국어로 찾아, 도로 유지관리 장비 근접도 상위 5개를 고르고 1p 5건을 자동 작성
- 1p: A4 1장, 개조식, 사진 2장(없으면 "사진 없음"), 출처는 앱 안에만 보관(1p 미표시)
- 교차 검증, AI 사진 확인, 워드 다운로드·ZIP, 구글 드라이브 저장

문서: `BRD.md` · `PRD.md` · `검증기준.md` · `주간_1p_프롬프트.md`

## PC에서 실행 (권장)
1. Python 설치 후 `pip install -r requirements.txt`
2. 사용자 환경 변수 `ANTHROPIC_API_KEY` 등록
3. `run.bat` 실행 → http://127.0.0.1:8000

테스트: `pip install -r requirements-dev.txt` 후 `python -m pytest -q`

## Vercel 배포 시 제한 (중요)
Vercel은 짧게 실행되는 서버리스 함수라서 이 앱의 일부 기능이 동작하지 않습니다.
- 장비 찾기·1p 작성(수 분 걸리는 백그라운드 작업)이 중간에 끊길 수 있음
- 데이터는 `/tmp`에만 저장되어 함수가 다시 시작되면 사라짐
- MS Word 분량 검사, 구글 드라이브 폴더 저장은 Windows PC에서만 동작
- **로그인 기능이 없음** — `ANTHROPIC_API_KEY`를 넣으면 주소를 아는 누구나 API 비용을 쓸 수 있으므로 Vercel의 Deployment Protection(비밀번호·Vercel 인증)을 켜 두세요.

API 키는 저장소에 넣지 말고 Vercel 프로젝트 설정의 Environment Variables에만 입력하세요.
