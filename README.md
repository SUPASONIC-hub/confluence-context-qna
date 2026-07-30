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
SEARCH_TIME_BUDGET_SECONDS=4.8
SLOW_SEARCH_MS=4800
ASK_CACHE_TTL_SECONDS=600
SEARCH_MAX_CANDIDATES=96
DB_STATEMENT_TIMEOUT_MS=5500
SEARCH_SENTENCE_SCAN_LIMIT=8
SEARCH_TEXT_SCAN_CHARS=3600
EVAL_SEARCH_TIME_BUDGET_SECONDS=1.6
RANKING_EVAL_LIMIT=12
INGEST_FETCH_LIMIT=20
INGEST_BATCH_MAX_SIZE=40
INGEST_BATCH_TIME_BUDGET_SECONDS=12
INGEST_MEMORY_SOFT_LIMIT_MB=360
INGEST_MAX_PAGE_TEXT_CHARS=450000
```

`CONFLUENCE_OFFICIAL_SPACES`에는 공식 정책/운영 문서가 들어있는 스페이스 키를 쉼표로 입력합니다. 해당 스페이스의 검색 결과는 점수가 더 높게 계산됩니다.
`CONFLUENCE_SPACE_WEIGHTS`와 `CONFLUENCE_DOCUMENT_TYPE_WEIGHTS`는 검색 랭킹 보정값입니다. `키:점수`를 쉼표로 연결합니다.
검색 품질이 낮게 표시되면 먼저 `CONFLUENCE_OFFICIAL_SPACES`를 지정하고, 자주 쓰는 공식 스페이스에는 `CONFLUENCE_SPACE_WEIGHTS`를 2-5점 범위로 부여하세요. 앱의 검색 품질 패널은 핵심어 매칭률, 공식 근거 수, 오래된 후보 수, 추천 검색어를 함께 보여줍니다.
운영 통계의 `인덱스`, `chunk/page`, `공식공간`, `랭킹` 카드가 경고 상태면 검색 품질이 낮아질 수 있습니다. 문서 수집 완료, 공식 스페이스 설정, 랭킹 가중치 설정을 먼저 확인하세요.
`ADMIN_TOKEN`을 설정하면 수집/백업 API 호출 시 `X-Admin-Token` 헤더가 필요합니다.
`SEARCH_TIME_BUDGET_SECONDS`는 검색 요청 하나가 후보 재랭킹에 쓰는 시간 예산입니다. Render에서는 4-5초 범위를 권장하며, 정밀 검색이 실패해도 빠른 fallback 후보를 반환합니다.
`SEARCH_MAX_CANDIDATES`는 쿼리별 재랭킹 후보 상한입니다. 문서가 많은 환경에서 검색 응답이 느리면 72-96 범위로 낮춰 보세요.
`DB_STATEMENT_TIMEOUT_MS`는 Postgres 단일 SQL의 제한 시간입니다. 기본값은 4500ms이며, 느린 검색/진단 쿼리가 gunicorn worker를 오래 붙잡지 않도록 합니다.
`DB_SCHEMA_TIMEOUT_MS`는 배포 직후 최초 스키마 확인 작업의 제한 시간입니다. 기본값은 15000ms이며, 이후 요청에서는 스키마 확인을 반복하지 않습니다.
`ASK_CACHE_TTL_SECONDS`는 같은 질문/검색 모드 재실행 시 서버 메모리 캐시를 유지하는 시간입니다. 반복 질문이나 502 후 재시도 비용을 줄입니다.
`EVAL_SEARCH_TIME_BUDGET_SECONDS`와 `RANKING_EVAL_LIMIT`는 랭킹 평가 1건당 검색 시간 예산과 운영 패널 평가 케이스 수를 제한합니다. 운영 중에는 1-2초, 12건 이하를 권장합니다.
`INGEST_FETCH_LIMIT`는 Confluence API에서 한 번에 가져오는 페이지 수입니다. Render 메모리가 빠듯하면 10-20을 권장합니다.
`INGEST_BATCH_MAX_SIZE`는 브라우저나 외부 호출이 한 번에 요청할 수 있는 최대 수집 페이지 수입니다. 긴 요청으로 `/healthz`가 밀리지 않도록 40 이하를 권장합니다.
`INGEST_BATCH_TIME_BUDGET_SECONDS`와 `INGEST_MEMORY_SOFT_LIMIT_MB`에 닿으면 수집 API는 `paused`로 반환하고, 이미 저장한 페이지의 다음 위치부터 다음 배치에서 자동 재개합니다.
`INGEST_MAX_PAGE_TEXT_CHARS`는 비정상적으로 큰 페이지 본문이 수집 프로세스와 DB를 압박하지 않도록 페이지별 검색 본문을 제한합니다.
`요청 실패: 502 Render gateway error`가 표시되면 배포/재시작 중이거나 검색 후보가 많아 요청 시간이 길어진 상태일 수 있습니다. 앱은 `/api/ask`를 한 번 재시도하고, 실패 시 히스토리/통계를 다시 확인합니다.
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

랭킹 회귀 평가:

```powershell
python .\confluence_qna.py eval-ranking --limit 24 --mode balanced
```

평가셋은 피드백이 있는 질문 히스토리와 실제 Confluence 문서 제목/유형에서 생성한 자동 gold case를 함께 사용합니다. `hit@3`, `MRR`, `bad_top_rate`, 평균 검색 시간을 확인해 검색 변경 전후를 비교하세요. `--json`을 붙이면 CI나 외부 리포트에서 쓰기 쉬운 JSON으로 출력합니다.
정확한 평가를 위해 실제 검색 결과에는 `유용`, `부분적`, `부정확` 피드백을 꾸준히 남기세요. 피드백 라벨이 적으면 자동 gold case 비중이 커져 제목 재현성은 검증할 수 있지만 실제 업무 판단 품질 평가는 제한됩니다.
사용자 테스트를 할 때는 앱 URL만 공유하고 관리자 토큰은 공유하지 마세요. 일반 사용자는 질문과 피드백 저장만 수행하고, 수집/복원/진단/랭킹 평가는 운영자가 관리자 토큰으로 실행하는 흐름을 권장합니다. `부분적` 또는 `부정확` 피드백에는 기대 문서, 누락 키워드, 틀린 이유를 메모로 남기면 다음 평가셋 보정에 바로 사용할 수 있습니다.

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
Start Command: gunicorn app:app --worker-class gthread --workers 1 --threads 4 --timeout 60 --graceful-timeout 20 --keep-alive 5
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

운영 패널이나 점검 결과가 `DB sqlite`, `임시 DB`, `문서 0`으로 보이면 배포 서비스가 Postgres를 사용하지 못하고 있는 상태입니다. Render 웹 서비스의 Environment에 `DATABASE_URL`이 Postgres 연결 문자열로 들어가 있는지 확인하고, 변경 후 서비스를 다시 배포/재시작해야 합니다. 이 상태에서 수집하면 재배포나 재시작 때 서버 문서가 다시 0개가 될 수 있습니다.

권장 수집 방식은 재시작에 안전한 배치 수집입니다.

```powershell
Invoke-RestMethod -Method Post `
  -Uri "https://YOUR-SERVICE.onrender.com/api/ingest/batch" `
  -Headers @{ "X-Admin-Token" = "ADMIN_TOKEN_VALUE" } `
  -ContentType "application/json" `
  -Body '{"batch_size":40}'
```

수집 상태 확인:

```text
https://YOUR-SERVICE.onrender.com/api/ingest/status
```

관리자 운영 점검:

```text
https://YOUR-SERVICE.onrender.com/api/admin/diagnostics
```

브라우저 운영 패널에서는 관리자 토큰 저장 후 `배치 수집`으로 이어서 수집하고, `처음부터 수집`으로 저장된 수집 진행 위치를 0부터 다시 계산합니다. 서버는 페이지 단위로 저장 지점을 커밋하고, 메모리/시간 예산에 닿으면 잠시 반환한 뒤 다음 배치에서 자동 재개합니다. 기존 문서는 삭제하지 않고 upsert로 최신 내용으로 갱신합니다.
`관리자 토큰 필요`가 표시되면 Render의 `ADMIN_TOKEN`과 같은 값을 운영 패널에 저장한 뒤 배치 수집을 실행합니다.
운영 패널의 `랭킹 평가`는 `/api/admin/ranking-eval`을 호출해 현재 corpus/히스토리 기준 검색 품질을 짧게 점검합니다. 평가 결과가 낮으면 실패 케이스 질문을 기준으로 동의어, 공식 스페이스 가중치, 문서 유형 가중치를 보정하세요.

CSV 백업:

```text
https://YOUR-SERVICE.onrender.com/api/export/pages.csv
```

문서 전체 백업/복원:

```text
GET  https://YOUR-SERVICE.onrender.com/api/export/pages.json
POST https://YOUR-SERVICE.onrender.com/api/import/pages.json
```

운영 패널의 `문서 백업`은 검색에 필요한 본문과 chunk 재생성 정보를 JSON으로 내려받고 같은 브라우저 IndexedDB에도 보관합니다. 배포 후 문서 수가 0으로 보이면 브라우저에 보관된 백업으로 자동 복원을 시도하고, 실패하면 `백업 복원`으로 JSON을 업로드해 다시 수집하지 않고 검색 DB를 복구할 수 있습니다. 서버 데이터의 확실한 영속성은 `DATABASE_URL`이 Postgres로 연결되어 있어야 보장됩니다.

질문 히스토리는 서버 DB와 별도로 같은 브라우저에도 최근 결과를 보관합니다. 커밋/재배포로 서버 히스토리가 0개가 되어도 브라우저가 같은 프로필이면 왼쪽 히스토리에 `브라우저` 표시로 다시 불러올 수 있습니다.

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
- 핵심어 근접 쌍 가중치와 근거별 매칭 이유/커버리지 진단
- 후보 chunk 수 제한과 경량 문장 비교로 Render 검색 타임아웃 방지
- 정책/매뉴얼/회의록/결정사항/기획서/이슈 문서 유형 분류
- 질문 의도별 문서 유형 가중치
- 공식 스페이스 가중치
- 질문 의도 키워드 확장 및 최신성 기반 재정렬
- 한국어/영문 동의어와 약어 확장 기반 후보 검색
- 질문의 대상/의도/조건/기간/부정·예외를 분리하는 경량 문맥 프로파일
- 문맥 프로파일 기반 후보 확장, 재랭킹, 문맥 커버리지 진단
- 핵심 대상어 `AND` 후보를 먼저 수집하고 부족할 때만 넓은 `OR` 검색으로 확장
- 질문 핵심 구문과 띄어쓰기 제거 구문이 제목/본문에 직접 포함된 후보를 넓은 후보보다 먼저 수집
- 본문 문장 안에서 대상어, 조건, 최신성, 예외/부정 표현이 함께 나오는 문서를 강하게 가점
- 제목만 맞고 본문 근거가 약한 후보는 감점해 문서 내용 기반 결과를 우선
- 최근 히스토리에서 과도하게 반복 노출된 문서는 문맥 커버리지가 낮을 때 감점
- 질문 원문/띄어쓰기 제거 문구 exact match 보정
- 검색 품질에 따른 추천 검색 모드 산정
- coverage/official/freshness/diversity/strength 기반 검색 scorecard
- 검색 실패 원인 코드와 조치 목록 생성
- 근거별 랭킹 신호와 검색 결과 피드백 저장
- 최근 유용/부정확 피드백 기반 랭킹 보정
- 검색 시간 예산, 느린 검색 표시, Render 502 복구 안내
- 같은 질문/모드 서버 캐시와 수집/복원 시 캐시 무효화
- 수집 페이지 단위 커밋, 진행 지점 저장, 메모리/시간 소프트 리밋 기반 안전 일시정지
- Gunicorn gthread worker와 초경량 `/healthz`로 긴 수집 중에도 Render health check 응답 유지
- 핵심어 근접도와 제목 매칭 기반 검색 점수 보정
- 다중 쿼리 검색 후보 생성
- 균형/정밀/넓게/최신 검색 모드
- 문서 다양성 기반 근거 chunk 재정렬
- 1단 검색 입력, 답변-근거 세로 흐름, 문서별 근거 chunk 그룹 UI
- 답변 결과 안에서 상위 근거 문서를 함께 확인하는 인라인 근거 UI
- 인라인 근거에서 상세 근거 문서 카드로 바로 이동하는 앵커 액션
- 핵심어 매칭률, 공식 근거 수, 오래된 후보 수, 랭킹 방식, 근거별 매칭 이유, 검색 품질 노트 표시
- 검색 품질 기반 추천 검색어 생성 및 원클릭 재검색
- 누락 핵심어, 결과 품질 분포, 품질순 근거 정렬 표시
- 질문 문맥 프로파일과 근거별 문맥 매칭 신호 표시
- 근거별 제목/본문/제목+본문 매칭 범위와 검색 결과 매칭 범위 분포 표시
- 본문/문장/제목 핵심어 커버리지와 문맥 누락 항목 표시
- 질문 입력 중 검색 품질 힌트와 운영 인덱스 건강도 표시
- 수집 fetch 크기, RSS 메모리, 안전 일시정지 사유를 운영 패널에 표시
- 근거 문서 목록 점진 렌더링과 필터 입력 디바운스로 대량 결과 화면 응답성 개선
- 검색 품질 이슈별 맞춤 재검색 액션과 운영 점검 로그 표시
- 유용/부정확 피드백 버튼과 운영 피드백 집계 표시
- 검색 품질에 따른 정밀/넓게/최신 재검색과 공식 근거 필터 액션
- 답변 섹션 탐색, 근거 정렬, 근거 목록 내 검색, 문서별 근거 펼침, 매칭 키워드 하이라이트 UI
- 질문 히스토리에서 이전 질문을 다시 실행하는 재질문 흐름
- 히스토리 검색, 답변 복사, 검색 품질 메타데이터 표시
- 수집 문서 JSON 백업과 복원
- 브라우저 IndexedDB 기반 문서 백업 자동 복원과 로컬 질문 히스토리 fallback
- 관리자 토큰 필요 여부와 저장 상태를 보여주는 운영 상태 표시
- 배치 수집 종료 후 통계/히스토리 자동 갱신과 시간 포함 운영 로그 표시
- 결론 후보, 최신성, 히스토리, 리스크 중심 검색 보고서 출력
- 웹 기반 질문/답변 및 히스토리 저장
- 검색 API 예외를 JSON 오류로 반환해 프론트에서 원인 확인 가능
- `DATABASE_URL` 기반 Postgres 저장소와 로컬 SQLite fallback
- 백그라운드 전체 수집 작업 및 수집 상태 API
