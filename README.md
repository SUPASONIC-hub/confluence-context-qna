# Confluence Context QNA

Confluence 문서를 수집해 SQLite FTS 검색 인덱스를 만들고, 최신성 가중치와 다중 쿼리 검색으로 질의에 대한 정책 답변, 정상 여부, 의사결정 히스토리, 잠재 리스크를 찾는 프로토타입입니다.

## 1. 환경 설정

```powershell
Copy-Item .env.example .env
```

`.env`에 본인 값만 채우세요. `.env`는 `.gitignore`에 포함되어 커밋 대상에서 제외됩니다.

필수 값:

```dotenv
CONFLUENCE_BASE_URL=https://your-domain.atlassian.net/wiki
CONFLUENCE_EMAIL=you@example.com
CONFLUENCE_API_TOKEN=your_api_token
DATABASE_URL=
```

선택 값:

```dotenv
CONFLUENCE_SPACE_KEY=
CONFLUENCE_OFFICIAL_SPACES=POLICY,OPS
CONFLUENCE_SPACE_WEIGHTS=POLICY:5,OPS:3
CONFLUENCE_DOCUMENT_TYPE_WEIGHTS=정책:4,매뉴얼:3,결정사항:3,이슈:2
ADMIN_TOKEN=change_me
```

`CONFLUENCE_OFFICIAL_SPACES`에는 공식 정책/운영 문서가 들어있는 스페이스 키를 쉼표로 입력합니다. 해당 스페이스의 검색 결과는 점수가 더 높게 계산됩니다.
`CONFLUENCE_SPACE_WEIGHTS`와 `CONFLUENCE_DOCUMENT_TYPE_WEIGHTS`는 검색 랭킹 보정값입니다. `키:점수`를 쉼표로 연결합니다.
`ADMIN_TOKEN`을 설정하면 수집/백업 API 호출 시 `X-Admin-Token` 헤더가 필요합니다.
`DATABASE_URL`이 있으면 Postgres를 사용하고, 없으면 로컬 SQLite(`data/confluence_qna.sqlite3`)를 사용합니다.

## 2. 설치

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 3. Confluence 문서 수집

접속과 권한을 먼저 점검:

```powershell
python .\confluence_qna.py diagnose
```

접근 가능한 모든 스페이스를 수집:

```powershell
python .\confluence_qna.py ingest --all-spaces
```

기본값은 접근 가능한 전체 페이지 수집입니다. 빠른 테스트가 필요할 때만 `--limit`을 씁니다. `--limit`은 전체 제한이 아니라 스페이스별 최대 페이지 수입니다.

특정 스페이스만 수집:

```powershell
python .\confluence_qna.py ingest --space KEY --limit 100
```

`.env`의 `CONFLUENCE_SPACE_KEY`를 사용할 수도 있습니다. `--space`와 `CONFLUENCE_SPACE_KEY`가 둘 다 없으면 기본적으로 접근 가능한 모든 스페이스를 수집합니다.

```powershell
python .\confluence_qna.py ingest
```

## 4. 질문

```powershell
python .\confluence_qna.py ask "현재 환불 정책 프로세스가 정상인가요?"
```

검색된 근거, 최신 문서 후보, 히스토리 후보, 리스크 후보를 구조화해서 보여줍니다.

## 5. Knowledge Management 인터페이스

```powershell
python .\app.py
```

브라우저에서 `http://127.0.0.1:5050`을 열면 질문 입력, 답변 결과, 근거 문서, 질문 히스토리를 한 화면에서 볼 수 있습니다. 질문 히스토리는 `data/confluence_qna.sqlite3`에 저장됩니다.

## 6. Git 및 Render 운영

이 프로젝트는 Render Blueprint용 `render.yaml`을 포함합니다. GitHub 저장소에 push한 뒤 Render에서 Blueprint 또는 Web Service로 연결할 수 있습니다.

Render 설정:

```text
Build Command: pip install -r requirements.txt
Start Command: gunicorn app:app
Health Check Path: /healthz
```

Render 환경 변수에는 `.env` 값을 직접 넣되, `.env` 파일 자체는 커밋하지 않습니다.

필수 Render 환경 변수:

```dotenv
CONFLUENCE_BASE_URL=https://your-domain.atlassian.net/wiki
CONFLUENCE_EMAIL=you@example.com
CONFLUENCE_API_TOKEN=your_api_token
CONFLUENCE_PAGE_LIMIT=0
CONFLUENCE_SPACE_KEY=
CONFLUENCE_OFFICIAL_SPACES=
CONFLUENCE_SPACE_WEIGHTS=
CONFLUENCE_DOCUMENT_TYPE_WEIGHTS=정책:4,매뉴얼:3,결정사항:3,이슈:2
ADMIN_TOKEN=strong-random-token
DATABASE_URL=Render Postgres 연결 문자열
```

`render.yaml`에는 무료 Render Postgres가 포함되어 있습니다. Render Blueprint로 생성하면 `DATABASE_URL`이 웹 서비스에 자동 연결됩니다.

권장 수집 방식은 재시작에 안전한 배치 수집입니다.

```powershell
Invoke-RestMethod -Method Post `
  -Uri "https://YOUR-SERVICE.onrender.com/api/ingest/batch" `
  -Headers @{ "X-Admin-Token" = "ADMIN_TOKEN_VALUE" } `
  -ContentType "application/json" `
  -Body '{"batch_size":80}'
```

수집 상태 확인:

```text
https://YOUR-SERVICE.onrender.com/api/ingest/status
```

관리자 운영 점검:

```text
https://YOUR-SERVICE.onrender.com/api/admin/diagnostics
```

브라우저 운영 패널에서는 관리자 토큰 저장 후 `배치 수집`으로 이어서 수집하고, `처음부터 수집`으로 저장된 수집 진행 위치를 0부터 다시 계산합니다. 기존 문서는 삭제하지 않고 upsert로 최신 내용으로 갱신합니다.

CSV 백업:

```text
https://YOUR-SERVICE.onrender.com/api/export/pages.csv
```

문서 전체 백업/복원:

```text
GET  https://YOUR-SERVICE.onrender.com/api/export/pages.json
POST https://YOUR-SERVICE.onrender.com/api/import/pages.json
```

운영 패널의 `문서 백업`은 검색에 필요한 본문과 chunk 재생성 정보를 JSON으로 내려받습니다. 배포 후 문서 수가 0으로 보이면 `백업 복원`으로 이 JSON을 업로드해 다시 수집하지 않고 검색 DB를 복구할 수 있습니다. Render에서는 `DATABASE_URL`이 Postgres로 연결되어 있어야 배포/재시작 후에도 데이터가 유지됩니다.

`ADMIN_TOKEN`이 설정되어 있으면 브라우저 운영 패널에 토큰을 저장한 뒤 CSV 백업을 누르거나, `X-Admin-Token` 헤더로 호출합니다.

GitHub Actions 예약 수집:

`.github/workflows/ingest-batch.yml`이 포함되어 있습니다. GitHub repository secrets에 아래 값을 설정하면 6시간마다 배치 수집을 호출합니다.

```text
SERVICE_URL=https://YOUR-SERVICE.onrender.com
ADMIN_TOKEN=Render에 설정한 ADMIN_TOKEN
```

수동 실행 시 `reset=true`를 선택하면 저장된 수집 진행 위치를 초기화하고 처음부터 다시 수집합니다. 워크플로는 중복 실행을 막도록 concurrency가 설정되어 있습니다.

주의: Render 무료 Postgres는 1GB 제한과 30일 만료 제한이 있습니다. 무료 조건에서 로컬 SQLite보다 안정적이지만 장기 운영용 영구 DB는 아닙니다.
날짜 접두 로컬 메모(`20*_*.txt`)는 저장소에 올리지 않습니다.

## 구현 범위

- Confluence REST API 수집
- 접근 가능한 전체 스페이스 순회 수집
- 본문 HTML 텍스트 정제
- `last_updated`, 작성자, 스페이스, URL 메타데이터 저장
- 등록일, 수정일, 작성자, 스페이스, URL 메타데이터 저장
- SQLite FTS5와 한국어 키워드 포함 검색 병행
- 문서 본문 chunk 분할 검색
- 한국어 조사/어미 제거 기반 질문 키워드 정제
- 질문에 직접 포함된 정책/기준/리스크/예외 같은 도메인 의도어를 핵심 맥락으로 유지
- 질문-문서 chunk 간 의미 토큰/문장 겹침 기반 문맥 재랭킹
- 후보 chunk 수 제한과 경량 문장 비교로 Render 검색 타임아웃 방지
- 정책/매뉴얼/회의록/결정사항/기획서/이슈 문서 유형 분류
- 질문 의도별 문서 유형 가중치
- 공식 스페이스 가중치
- 질문 의도 키워드 확장 및 최신성 기반 재정렬
- 핵심어 근접도와 제목 매칭 기반 검색 점수 보정
- 다중 쿼리 검색 후보 생성
- 균형/정밀/넓게/최신 검색 모드
- 문서 다양성 기반 근거 chunk 재정렬
- 1단 검색 입력, 답변-근거 세로 흐름, 문서별 근거 chunk 그룹 UI
- 답변 결과 안에서 상위 근거 문서를 함께 확인하는 인라인 근거 UI
- 인라인 근거에서 상세 근거 문서 카드로 바로 이동하는 앵커 액션
- 핵심어 매칭률, 공식 근거 수, 오래된 후보 수, 랭킹 방식, 검색 품질 노트 표시
- 검색 품질에 따른 정밀/넓게/최신 재검색과 공식 근거 필터 액션
- 답변 섹션 탐색, 근거 정렬, 근거 목록 내 검색, 문서별 근거 펼침, 매칭 키워드 하이라이트 UI
- 질문 히스토리에서 이전 질문을 다시 실행하는 재질문 흐름
- 히스토리 검색, 답변 복사, 검색 품질 메타데이터 표시
- 수집 문서 JSON 백업과 복원
- 관리자 토큰 필요 여부와 저장 상태를 보여주는 운영 상태 표시
- 배치 수집 종료 후 통계/히스토리 자동 갱신과 시간 포함 운영 로그 표시
- 결론 후보, 최신성, 히스토리, 리스크 중심 검색 보고서 출력
- 웹 기반 질문/답변 및 히스토리 저장
- 검색 API 예외를 JSON 오류로 반환해 프론트에서 원인 확인 가능
- `DATABASE_URL` 기반 Postgres 저장소와 로컬 SQLite fallback
- 백그라운드 전체 수집 작업 및 수집 상태 API
