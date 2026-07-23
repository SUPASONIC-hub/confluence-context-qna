from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
from requests import HTTPError
from bs4 import BeautifulSoup
from dotenv import load_dotenv

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None


DB_PATH = Path("data/confluence_qna.sqlite3")


class PostgresConnection:
    def __init__(self, conn):
        self.conn = conn
        self.is_postgres = True

    def execute(self, sql: str, params: Iterable[object] | None = None):
        cur = self.conn.cursor()
        cur.execute(sql.replace("?", "%s"), tuple(params or ()))
        return cur

    def executemany(self, sql: str, params_seq: Iterable[Iterable[object]]):
        cur = self.conn.cursor()
        cur.executemany(sql.replace("?", "%s"), [tuple(params) for params in params_seq])
        return cur

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


@dataclass(frozen=True)
class Config:
    base_url: str
    email: str
    api_token: str
    space_key: str | None
    page_limit: int
    official_spaces: tuple[str, ...]
    space_weights: dict[str, float]
    document_type_weights: dict[str, float]


@dataclass(frozen=True)
class SearchHit:
    page_id: str
    chunk_index: int
    title: str
    text: str
    created_at: str
    last_updated: str
    author: str
    space: str
    url: str
    score: float
    document_type: str
    matched_terms: tuple[str, ...] = ()


def load_config() -> Config:
    load_dotenv()
    space_weights = parse_weight_map(os.getenv("CONFLUENCE_SPACE_WEIGHTS", ""))
    doc_type_weights = parse_weight_map(os.getenv("CONFLUENCE_DOCUMENT_TYPE_WEIGHTS", ""))
    return Config(
        base_url=os.getenv("CONFLUENCE_BASE_URL", "").rstrip("/"),
        email=os.getenv("CONFLUENCE_EMAIL", ""),
        api_token=os.getenv("CONFLUENCE_API_TOKEN", ""),
        space_key=os.getenv("CONFLUENCE_SPACE_KEY") or None,
        page_limit=int(os.getenv("CONFLUENCE_PAGE_LIMIT", "0")),
        official_spaces=tuple(
            space.strip()
            for space in os.getenv("CONFLUENCE_OFFICIAL_SPACES", "").split(",")
            if space.strip()
        ),
        space_weights=space_weights,
        document_type_weights=doc_type_weights,
    )


def parse_weight_map(raw: str) -> dict[str, float]:
    result = {}
    for item in raw.split(","):
        if not item.strip() or ":" not in item:
            continue
        key, value = item.split(":", 1)
        try:
            result[key.strip()] = float(value.strip())
        except ValueError:
            continue
    return result


def require_confluence_config(config: Config) -> None:
    missing = [
        name
        for name, value in (
            ("CONFLUENCE_BASE_URL", config.base_url),
            ("CONFLUENCE_EMAIL", config.email),
            ("CONFLUENCE_API_TOKEN", config.api_token),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f".env에 필수 값이 없습니다: {', '.join(missing)}")


def explain_http_error(error: HTTPError) -> str:
    response = error.response
    if response is None:
        return str(error)

    detail = ""
    try:
        payload = response.json()
        detail = payload.get("message") or payload.get("errorMessage") or ""
    except ValueError:
        detail = response.text[:500]

    base = f"Confluence API 오류: HTTP {response.status_code} {response.reason}"
    if response.status_code == 401:
        hint = "이메일 또는 API 토큰이 잘못되었을 가능성이 큽니다."
    elif response.status_code == 403:
        hint = (
            "인증은 시도됐지만 현재 계정이 이 Confluence 사이트에 접근할 수 없습니다. "
            "CONFLUENCE_EMAIL이 토큰을 발급한 Atlassian 계정과 같은지, "
            "해당 계정에 Confluence product access와 스페이스 권한이 있는지 확인하세요."
        )
    elif response.status_code == 404:
        hint = "CONFLUENCE_BASE_URL 또는 API 경로가 맞는지 확인하세요."
    else:
        hint = "응답 메시지를 기준으로 URL, 권한, 네트워크 상태를 확인하세요."

    return f"{base}\n상세: {detail}\n힌트: {hint}"


def connect_db():
    database_url = os.getenv("DATABASE_URL", "")
    if database_url.startswith(("postgres://", "postgresql://")):
        return connect_postgres(database_url)
    return connect_sqlite()


def connect_postgres(database_url: str) -> PostgresConnection:
    if psycopg is None or dict_row is None:
        raise RuntimeError("Postgres 사용을 위해 `pip install -r requirements.txt`를 실행하세요.")
    raw_conn = psycopg.connect(database_url, row_factory=dict_row)
    conn = PostgresConnection(raw_conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pages (
            page_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT '',
            last_updated TEXT NOT NULL,
            author TEXT NOT NULL,
            space TEXT NOT NULL,
            url TEXT NOT NULL,
            raw_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS page_chunks (
            page_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT '',
            last_updated TEXT NOT NULL,
            author TEXT NOT NULL,
            space TEXT NOT NULL,
            url TEXT NOT NULL,
            PRIMARY KEY (page_id, chunk_index)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_progress (
            space TEXT PRIMARY KEY,
            next_start INTEGER NOT NULL DEFAULT 0,
            completed BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_page_chunks_space ON page_chunks(space)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_page_chunks_updated ON page_chunks(last_updated)")
    conn.commit()
    backfill_page_chunks(conn)
    return conn


def connect_sqlite() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pages (
            page_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT '',
            last_updated TEXT NOT NULL,
            author TEXT NOT NULL,
            space TEXT NOT NULL,
            url TEXT NOT NULL,
            raw_json TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
            title,
            text,
            content='pages',
            content_rowid='rowid'
        );

        CREATE TABLE IF NOT EXISTS page_chunks (
            page_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT '',
            last_updated TEXT NOT NULL,
            author TEXT NOT NULL,
            space TEXT NOT NULL,
            url TEXT NOT NULL,
            PRIMARY KEY (page_id, chunk_index)
        );

        CREATE TABLE IF NOT EXISTS ingest_progress (
            space TEXT PRIMARY KEY,
            next_start INTEGER NOT NULL DEFAULT 0,
            completed BOOLEAN NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT ''
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS page_chunks_fts USING fts5(
            title,
            text,
            content='page_chunks',
            content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
            INSERT INTO pages_fts(rowid, title, text)
            VALUES (new.rowid, new.title, new.text);
        END;

        CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, title, text)
            VALUES ('delete', old.rowid, old.title, old.text);
        END;

        CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, title, text)
            VALUES ('delete', old.rowid, old.title, old.text);
            INSERT INTO pages_fts(rowid, title, text)
            VALUES (new.rowid, new.title, new.text);
        END;

        CREATE TRIGGER IF NOT EXISTS page_chunks_ai AFTER INSERT ON page_chunks BEGIN
            INSERT INTO page_chunks_fts(rowid, title, text)
            VALUES (new.rowid, new.title, new.text);
        END;

        CREATE TRIGGER IF NOT EXISTS page_chunks_ad AFTER DELETE ON page_chunks BEGIN
            INSERT INTO page_chunks_fts(page_chunks_fts, rowid, title, text)
            VALUES ('delete', old.rowid, old.title, old.text);
        END;

        CREATE TRIGGER IF NOT EXISTS page_chunks_au AFTER UPDATE ON page_chunks BEGIN
            INSERT INTO page_chunks_fts(page_chunks_fts, rowid, title, text)
            VALUES ('delete', old.rowid, old.title, old.text);
            INSERT INTO page_chunks_fts(rowid, title, text)
            VALUES (new.rowid, new.title, new.text);
        END;
        """
    )
    ensure_column(conn, "pages", "created_at", "TEXT NOT NULL DEFAULT ''")
    backfill_page_chunks(conn)
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def backfill_page_chunks(conn: sqlite3.Connection) -> None:
    page_count = conn.execute("SELECT COUNT(*) AS count FROM pages").fetchone()["count"]
    chunk_count = conn.execute("SELECT COUNT(*) AS count FROM page_chunks").fetchone()["count"]
    if page_count == 0 or chunk_count > 0:
        return
    rows = conn.execute(
        """
        SELECT page_id, title, text, created_at, last_updated, author, space, url
        FROM pages
        """
    ).fetchall()
    for row in rows:
        conn.executemany(
            """
            INSERT INTO page_chunks(
                page_id, chunk_index, title, text, created_at, last_updated, author, space, url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(page_id, chunk_index) DO NOTHING
            """,
            [
                (
                    row["page_id"],
                    index,
                    row["title"],
                    chunk,
                    row["created_at"],
                    row["last_updated"],
                    row["author"],
                    row["space"],
                    row["url"],
                )
                for index, chunk in enumerate(split_chunks(row["text"]))
            ],
        )
    conn.commit()


def confluence_get(config: Config, path: str, params: dict[str, object]) -> dict:
    url = f"{config.base_url}{path}"
    response = requests.get(
        url,
        params=params,
        auth=(config.email, config.api_token),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    try:
        response.raise_for_status()
    except HTTPError as error:
        raise RuntimeError(explain_http_error(error)) from error
    return response.json()


def clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


def split_chunks(text: str, max_chars: int = 1300, overlap_chars: int = 180) -> list[str]:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?。！？])\s+|(?<=다\.)\s+", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences or [text]:
        if len(sentence) > max_chars:
            for start in range(0, len(sentence), max_chars - overlap_chars):
                piece = sentence[start : start + max_chars].strip()
                if piece:
                    chunks.append(piece)
            current = ""
            continue
        if current and len(current) + len(sentence) + 1 > max_chars:
            chunks.append(current)
            current = current[-overlap_chars:].strip()
        current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks or [text[:max_chars]]


def page_url(config: Config, item: dict) -> str:
    links = item.get("_links", {})
    webui = links.get("webui") or ""
    return f"{config.base_url}{webui}" if webui.startswith("/") else webui


def iter_spaces(config: Config) -> Iterable[str]:
    start = 0
    page_size = 100
    while True:
        data = confluence_get(
            config,
            "/rest/api/space",
            {
                "limit": page_size,
                "start": start,
                "type": "global",
            },
        )
        results = data.get("results", [])
        if not results:
            break
        for item in results:
            key = item.get("key")
            if key:
                yield key
        if len(results) < page_size:
            break
        start += len(results)


def iter_pages(config: Config, space: str, limit: int | None) -> Iterable[dict]:
    start = 0
    page_size = 100
    fetched = 0
    while limit is None or fetched < limit:
        current_limit = page_size if limit is None else min(page_size, limit - fetched)
        data = confluence_get(
            config,
            "/rest/api/content",
            {
                "type": "page",
                "spaceKey": space,
                "limit": current_limit,
                "start": start,
                "expand": "body.storage,version,history,space",
            },
        )
        results = data.get("results", [])
        if not results:
            break
        yield from results
        fetched += len(results)
        if len(results) < page_size:
            break
        start += len(results)


def fetch_page_batch(config: Config, space: str, start: int, limit: int) -> list[dict]:
    data = confluence_get(
        config,
        "/rest/api/content",
        {
            "type": "page",
            "spaceKey": space,
            "limit": limit,
            "start": start,
            "expand": "body.storage,version,history,space",
        },
    )
    return data.get("results", [])


def upsert_page(conn: sqlite3.Connection, config: Config, item: dict) -> None:
    version = item.get("version", {})
    history = item.get("history", {})
    author = (version.get("by") or {}).get("displayName", "unknown")
    space = (item.get("space") or {}).get("key", "")
    body = ((item.get("body") or {}).get("storage") or {}).get("value", "")
    title = item.get("title", "")
    text = clean_html(body)
    created_at = history.get("createdDate", "")
    last_updated = version.get("when", "")
    url = page_url(config, item)
    conn.execute(
        """
        INSERT INTO pages(page_id, title, text, created_at, last_updated, author, space, url, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(page_id) DO UPDATE SET
            title=excluded.title,
            text=excluded.text,
            created_at=excluded.created_at,
            last_updated=excluded.last_updated,
            author=excluded.author,
            space=excluded.space,
            url=excluded.url,
            raw_json=excluded.raw_json
        """,
        (
            item["id"],
            title,
            text,
            created_at,
            last_updated,
            author,
            space,
            url,
            json.dumps(item, ensure_ascii=False),
        ),
    )
    conn.execute("DELETE FROM page_chunks WHERE page_id = ?", (item["id"],))
    conn.executemany(
        """
        INSERT INTO page_chunks(
            page_id, chunk_index, title, text, created_at, last_updated, author, space, url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (item["id"], index, title, chunk, created_at, last_updated, author, space, url)
            for index, chunk in enumerate(split_chunks(text))
        ],
    )


def upsert_stored_page(conn: sqlite3.Connection, item: dict) -> None:
    page_id = str(item.get("page_id") or "").strip()
    if not page_id:
        raise ValueError("backup page is missing page_id")
    title = str(item.get("title") or "")
    text = str(item.get("text") or "")
    created_at = str(item.get("created_at") or "")
    last_updated = str(item.get("last_updated") or "")
    author = str(item.get("author") or "")
    space = str(item.get("space") or "")
    url = str(item.get("url") or "")
    raw_json = item.get("raw_json")
    if not isinstance(raw_json, str):
        raw_json = json.dumps(raw_json or {}, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO pages(page_id, title, text, created_at, last_updated, author, space, url, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(page_id) DO UPDATE SET
            title=excluded.title,
            text=excluded.text,
            created_at=excluded.created_at,
            last_updated=excluded.last_updated,
            author=excluded.author,
            space=excluded.space,
            url=excluded.url,
            raw_json=excluded.raw_json
        """,
        (page_id, title, text, created_at, last_updated, author, space, url, raw_json),
    )
    conn.execute("DELETE FROM page_chunks WHERE page_id = ?", (page_id,))
    conn.executemany(
        """
        INSERT INTO page_chunks(
            page_id, chunk_index, title, text, created_at, last_updated, author, space, url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (page_id, index, title, chunk, created_at, last_updated, author, space, url)
            for index, chunk in enumerate(split_chunks(text))
        ],
    )


def utc_now_text() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def initialize_ingest_progress(conn, config: Config, reset: bool = False) -> None:
    spaces = list(iter_spaces(config))
    if reset:
        conn.execute("DELETE FROM ingest_progress")
    for space in spaces:
        conn.execute(
            """
            INSERT INTO ingest_progress(space, next_start, completed, updated_at)
            VALUES (?, 0, ?, ?)
            ON CONFLICT(space) DO NOTHING
            """,
            (space, False, utc_now_text()),
        )
    conn.commit()


def ingest_progress_status(conn) -> dict[str, object]:
    rows = conn.execute(
        """
        SELECT space, next_start, completed, updated_at
        FROM ingest_progress
        ORDER BY space
        """
    ).fetchall()
    spaces = [
        {
            "space": row["space"],
            "next_start": row["next_start"],
            "completed": bool(row["completed"]),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    return {
        "spaces": spaces,
        "completed": bool(spaces) and all(space["completed"] for space in spaces),
        "remaining": sum(1 for space in spaces if not space["completed"]),
        "completed_spaces": sum(1 for space in spaces if space["completed"]),
        "total_spaces": len(spaces),
        "indexed_offsets": sum(int(space["next_start"] or 0) for space in spaces),
        "active_space": next((space["space"] for space in spaces if not space["completed"]), None),
    }


def ingest_batch(batch_size: int = 100, reset: bool = False) -> dict[str, object]:
    config = load_config()
    require_confluence_config(config)
    conn = connect_db()
    processed = 0
    touched_spaces = []
    try:
        initialize_ingest_progress(conn, config, reset=reset)
        while processed < batch_size:
            row = conn.execute(
                """
                SELECT space, next_start
                FROM ingest_progress
                WHERE completed = ?
                ORDER BY space
                LIMIT 1
                """,
                (False,),
            ).fetchone()
            if row is None:
                break

            current_limit = min(100, batch_size - processed)
            results = fetch_page_batch(config, row["space"], int(row["next_start"]), current_limit)
            for item in results:
                upsert_page(conn, config, item)
            processed += len(results)
            touched_spaces.append(row["space"])

            completed = len(results) < current_limit
            next_start = int(row["next_start"]) + len(results)
            conn.execute(
                """
                UPDATE ingest_progress
                SET next_start = ?, completed = ?, updated_at = ?
                WHERE space = ?
                """,
                (next_start, completed, utc_now_text(), row["space"]),
            )
            conn.commit()
            if not results:
                continue
        status = ingest_progress_status(conn)
        return {
            "status": "completed" if status["completed"] else "running",
            "batch_size": batch_size,
            "processed": processed,
            "touched_spaces": sorted(set(touched_spaces)),
            "progress": status,
        }
    finally:
        conn.close()


def ingest(args: argparse.Namespace) -> None:
    config = load_config()
    require_confluence_config(config)
    if args.all_spaces:
        spaces = list(iter_spaces(config))
    else:
        space = args.space or config.space_key
        spaces = [space] if space else list(iter_spaces(config))

    if not spaces:
        raise RuntimeError("수집 가능한 Confluence 스페이스를 찾지 못했습니다.")

    conn = connect_db()
    total_count = 0
    per_space_limit = args.limit if args.limit is not None else config.page_limit
    if per_space_limit <= 0:
        per_space_limit = None
    for space in spaces:
        count = 0
        limit_label = "all" if per_space_limit is None else str(per_space_limit)
        print(f"수집 시작: space={space}, limit={limit_label}")
        for item in iter_pages(config, space, per_space_limit):
            upsert_page(conn, config, item)
            count += 1
            total_count += 1
        conn.commit()
        print(f"수집 완료: space={space}, pages={count}")
    conn.commit()
    print(f"전체 수집 완료: spaces={len(spaces)}, pages={total_count}, DB={DB_PATH}")


def diagnose(args: argparse.Namespace) -> None:
    config = load_config()
    require_confluence_config(config)
    checks = [
        ("current user", "/rest/api/user/current", {}),
        ("space list", "/rest/api/space", {"limit": 1}),
        ("content list", "/rest/api/content", {"limit": 1, "type": "page", "expand": "space,version"}),
    ]
    print(f"base_url={config.base_url}")
    print(f"email_set={'yes' if config.email else 'no'}")
    print(f"token_set={'yes' if config.api_token else 'no'}")
    for label, path, params in checks:
        url = f"{config.base_url}{path}"
        response = requests.get(
            url,
            params=params,
            auth=(config.email, config.api_token),
            headers={"Accept": "application/json"},
            timeout=20,
        )
        print(f"{label}: HTTP {response.status_code} {response.reason}")
        if response.status_code >= 400:
            try:
                payload = response.json()
                message = payload.get("message") or payload.get("errorMessage") or ""
            except ValueError:
                message = response.text[:300]
            print(f"  message={message}")


def parse_iso(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def recency_boost(last_updated: str) -> float:
    updated = parse_iso(last_updated)
    if not updated:
        return 0.0
    now = dt.datetime.now(dt.timezone.utc)
    age_days = max((now - updated.astimezone(dt.timezone.utc)).days, 0)
    if age_days <= 90:
        return 2.0
    if age_days <= 180:
        return 1.3
    if age_days <= 365:
        return 0.7
    return -0.8


def fts_query(text: str) -> str:
    terms = extract_terms(text)
    return " OR ".join(terms[:12]) or text


INTENT_KEYWORDS = {
    "최종": ("최종", "정의", "최신", "확정", "최종안", "최종본"),
    "정의": ("정의", "기준", "정책", "규칙", "가이드", "가이드라인"),
    "상태값": ("상태값", "상태", "status", "값"),
    "발주": ("발주", "주문", "오더", "order"),
    "정책": ("정책", "가이드", "가이드라인", "기준", "프로세스"),
    "리스크": ("리스크", "위험", "문제", "이슈", "상충", "예외"),
    "정상": ("정상", "검증", "점검", "확인", "이슈", "리스크"),
}

DOCUMENT_TYPE_KEYWORDS = {
    "정책": ("정책", "규정", "가이드", "가이드라인", "기준", "운영 기준", "프로세스", "SOP"),
    "매뉴얼": ("매뉴얼", "manual", "사용법", "처리 방법", "업무 방법", "운영 방법"),
    "회의록": ("회의", "회의록", "논의", "미팅", "싱크", "sync"),
    "결정사항": ("결정", "확정", "최종", "승인", "decision", "히스토리"),
    "기획서": ("기획", "요구사항", "상세 기획", "스펙", "spec", "정의서"),
    "이슈": ("이슈", "문제", "버그", "장애", "리스크", "상충", "예외"),
}

DOMAIN_TERMS = (
    "발주",
    "상태값",
    "상태",
    "최종",
    "정의",
    "정책",
    "가이드",
    "가이드라인",
    "프로세스",
    "매뉴얼",
    "기준",
    "회의록",
    "히스토리",
    "결정",
    "리스크",
    "문제",
    "상충",
    "예외",
)

INTENT_ONLY_TERMS = {
    "최종",
    "정의",
    "최신",
    "확정",
    "최종안",
    "최종본",
    "정책",
    "가이드",
    "가이드라인",
    "기준",
    "프로세스",
    "매뉴얼",
    "회의록",
    "히스토리",
    "결정",
    "리스크",
    "문제",
    "상충",
    "예외",
    "현재",
    "정상",
    "정상인가요",
    "맞나요",
    "인가요",
    "있나요",
    "어떻게",
    "무엇",
    "확인",
    "점검",
}

STOPWORDS = {
    "현재",
    "관련",
    "대한",
    "대해",
    "위한",
    "통해",
    "그리고",
    "또는",
    "혹은",
    "입니다",
    "합니다",
    "되나요",
    "인가요",
    "있나요",
    "없나요",
    "맞나요",
    "정상인가요",
    "어떻게",
    "무엇",
    "어떤",
    "질문",
}

KOREAN_SUFFIXES = (
    "으로써",
    "으로서",
    "에서는",
    "에게는",
    "부터는",
    "까지는",
    "이라는",
    "라는",
    "이며",
    "이고",
    "하고",
    "해서",
    "에서",
    "에게",
    "부터",
    "까지",
    "으로",
    "로서",
    "와의",
    "과의",
    "들은",
    "들을",
    "으로",
    "으로",
    "인가요",
    "나요",
    "가요",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "의",
    "와",
    "과",
    "도",
    "만",
)


def ordered_unique(values: Iterable[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        normalized = value.strip().lower()
        if len(normalized) < 2 or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def normalize_token(token: str) -> str:
    normalized = token.strip().lower()
    if normalized in STOPWORDS:
        return ""
    for suffix in KOREAN_SUFFIXES:
        if len(normalized) - len(suffix) >= 2 and normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return "" if normalized in STOPWORDS else normalized


def question_tokens(text: str) -> list[str]:
    return ordered_unique(
        token
        for token in (normalize_token(raw) for raw in re.findall(r"[0-9A-Za-z가-힣_]+", text))
        if token
    )


def classify_document(title: str, text: str) -> str:
    haystack = f"{title} {text[:2500]}".lower()
    scores: dict[str, int] = {}
    for doc_type, keywords in DOCUMENT_TYPE_KEYWORDS.items():
        score = 0
        for keyword in keywords:
            key = keyword.lower()
            title_count = title.lower().count(key)
            body_count = haystack.count(key)
            score += title_count * 4 + min(body_count, 5)
        if score:
            scores[doc_type] = score
    if not scores:
        return "일반문서"
    return max(scores.items(), key=lambda item: item[1])[0]


def question_intents(question: str) -> set[str]:
    normalized = question.lower()
    intents = set()
    if any(term in normalized for term in ("정상", "맞", "검증", "확인")):
        intents.update(("정책", "매뉴얼", "이슈"))
    if any(term in normalized for term in ("최종", "최신", "정의", "기준", "정책")):
        intents.update(("정책", "결정사항"))
    if any(term in normalized for term in ("왜", "배경", "히스토리", "결정", "회의")):
        intents.update(("회의록", "결정사항"))
    if any(term in normalized for term in ("리스크", "문제", "상충", "예외", "위험")):
        intents.add("이슈")
    return intents


def extract_terms(question: str) -> list[str]:
    tokens = question_tokens(question)
    expanded = []
    for token in tokens:
        expanded.append(token)
        for domain_term in DOMAIN_TERMS:
            if domain_term in token:
                expanded.append(domain_term)
        if re.search(r"[가-힣]", token) and len(token) >= 5:
            expanded.extend(token[i : i + 3] for i in range(0, len(token) - 2))
    for trigger, synonyms in INTENT_KEYWORDS.items():
        if trigger in question:
            expanded.extend(synonyms)
    return ordered_unique(expanded)


def essential_terms(question: str) -> list[str]:
    terms = []
    for term in DOMAIN_TERMS:
        if term not in INTENT_ONLY_TERMS and term in question:
            terms.append(term)
    tokens = question_tokens(question)
    for token in tokens:
        if len(token) >= 2 and not any(intent in token for intent in INTENT_ONLY_TERMS):
            terms.append(token)
    return ordered_unique(terms)


def row_to_hit(row: sqlite3.Row, score: float, matched_terms: Iterable[str]) -> SearchHit:
    document_type = classify_document(row["title"], row["text"])
    return SearchHit(
        page_id=row["page_id"],
        chunk_index=row["chunk_index"],
        title=row["title"],
        text=row["text"],
        created_at=row["created_at"],
        last_updated=row["last_updated"],
        author=row["author"],
        space=row["space"],
        url=row["url"],
        score=score,
        document_type=document_type,
        matched_terms=tuple(ordered_unique(matched_terms)),
    )


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def phrase_candidates(question: str) -> list[str]:
    tokens = [token for token in question_tokens(question) if token not in INTENT_ONLY_TERMS]
    phrases = []
    normalized = compact_text(question)
    if len(normalized) >= 4:
        phrases.append(normalized)
    phrases.extend(" ".join(tokens[index : index + 2]) for index in range(len(tokens) - 1))
    phrases.extend(token for token in tokens if len(token) >= 4)
    return ordered_unique(phrases)


def term_score(
    row: sqlite3.Row,
    query: str,
    terms: list[str],
    essentials: list[str],
    preferred_doc_types: set[str],
    config: Config,
) -> tuple[float, list[str]]:
    title = compact_text(row["title"])
    text = compact_text(row["text"])
    matched = []
    score = recency_boost(row["last_updated"])
    document_type = classify_document(row["title"], row["text"])
    for term in terms:
        title_count = title.count(term)
        text_count = text.count(term)
        if title_count or text_count:
            matched.append(term)
            multiplier = 3 if term in essentials else 1
            score += title_count * 6 * multiplier
            score += min(text_count, 8) * 1.2 * multiplier
    for phrase in phrase_candidates(query)[:8]:
        if phrase in title:
            score += 12.0
        elif phrase in text:
            score += 4.0
    if terms:
        coverage = len(set(matched)) / max(len(set(terms[:12])), 1)
        score += coverage * 8.0
    matched_essentials = [term for term in essentials if term in matched]
    if essentials and not matched_essentials:
        return -999.0, matched
    if matched_essentials:
        score += 10 * len(matched_essentials)
    if any(term in matched for term in ("최종", "정의", "최신", "확정")):
        score += 2.5
    if any(term in matched for term in ("정책", "가이드", "가이드라인", "기준")):
        score += 1.5
    if document_type in preferred_doc_types:
        score += 5.0
    elif preferred_doc_types and document_type == "일반문서":
        score -= 2.0
    if document_type in {"정책", "결정사항"} and any(term in title for term in ("최종", "확정", "정책", "기준")):
        score += 4.0
    if row["space"] in config.official_spaces:
        score += 4.0
    score += config.space_weights.get(row["space"], 0.0)
    score += config.document_type_weights.get(document_type, 0.0)
    return score, matched


def search(conn: sqlite3.Connection, query: str, limit: int = 8) -> list[SearchHit]:
    config = load_config()
    terms = extract_terms(query)
    essentials = essential_terms(query)
    preferred_doc_types = question_intents(query)
    if not terms:
        return []

    rows_by_id: dict[tuple[str, int], sqlite3.Row] = {}
    like_clauses = []
    params = []
    for term in terms[:16]:
        like_clauses.append("(LOWER(title) LIKE ? OR LOWER(text) LIKE ?)")
        like = f"%{term}%"
        params.extend([like, like])

    if like_clauses:
        like_rows = conn.execute(
            f"""
            SELECT page_id, chunk_index, title, text, created_at, last_updated, author, space, url
            FROM page_chunks
            WHERE {" OR ".join(like_clauses)}
            """,
            params,
        ).fetchall()
        rows_by_id.update({(row["page_id"], row["chunk_index"]): row for row in like_rows})

    if not getattr(conn, "is_postgres", False):
        try:
            fts_rows = conn.execute(
                """
                SELECT c.page_id, c.chunk_index, c.title, c.text, c.created_at, c.last_updated, c.author, c.space, c.url
                FROM page_chunks_fts
                JOIN page_chunks c ON c.rowid = page_chunks_fts.rowid
                WHERE page_chunks_fts MATCH ?
                LIMIT ?
                """,
                (fts_query(query), max(limit * 6, 30)),
            ).fetchall()
            rows_by_id.update({(row["page_id"], row["chunk_index"]): row for row in fts_rows})
        except sqlite3.OperationalError:
            pass

    hits = []
    for row in rows_by_id.values():
        score, matched = term_score(row, query, terms, essentials, preferred_doc_types, config)
        if matched and score > -999:
            hits.append(row_to_hit(row, score, matched))
    return sorted(hits, key=lambda hit: (hit.score, hit.last_updated), reverse=True)[:limit]


def derive_queries(question: str, mode: str = "balanced") -> list[str]:
    base = question.strip()
    essentials = " ".join(essential_terms(question))
    prefixes = [
        f"{essentials} 최신 정책 최종 정의",
        f"{essentials} 상태값 기준",
        f"{essentials} 의사결정 회의록 배경",
        f"{essentials} 리스크 상충 예외",
    ]
    if mode == "strict":
        prefixes = [
            f"{essentials} 정확한 기준 최종 확정",
            f"{essentials} 정책 매뉴얼 적용 범위",
        ]
    elif mode == "broad":
        prefixes.extend(
            [
                f"{base} 관련 참고",
                f"{base} 예외 변경 이력",
                f"{base} 운영 가이드",
            ]
        )
    elif mode == "recent":
        prefixes.insert(0, f"{essentials} 최신 변경 최근 업데이트")
    return ordered_unique([base, *prefixes])


def diversify_hits(hits: list[SearchHit], limit: int = 18, per_page_limit: int = 2) -> list[SearchHit]:
    selected = []
    page_counts: dict[str, int] = {}
    for hit in hits:
        if page_counts.get(hit.page_id, 0) >= per_page_limit:
            continue
        selected.append(hit)
        page_counts[hit.page_id] = page_counts.get(hit.page_id, 0) + 1
        if len(selected) >= limit:
            break
    if len(selected) < min(limit, len(hits)):
        selected_ids = {(hit.page_id, hit.chunk_index) for hit in selected}
        for hit in hits:
            key = (hit.page_id, hit.chunk_index)
            if key in selected_ids:
                continue
            selected.append(hit)
            if len(selected) >= limit:
                break
    return selected


def mode_rank_key(hit: SearchHit, mode: str) -> tuple[float, str]:
    if mode == "recent":
        return (recency_boost(hit.last_updated) * 6 + hit.score, hit.last_updated)
    if mode == "strict":
        return (hit.score + len(hit.matched_terms) * 1.5, hit.last_updated)
    if mode == "broad":
        return (hit.score - max(hit.score - 30, 0) * 0.25, hit.last_updated)
    return (hit.score, hit.last_updated)


def merged_hits(conn: sqlite3.Connection, question: str, mode: str = "balanced") -> list[SearchHit]:
    mode = mode if mode in {"balanced", "strict", "broad", "recent"} else "balanced"
    by_id: dict[tuple[str, int], SearchHit] = {}
    per_query_limit = 10 if mode == "broad" else 7 if mode == "recent" else 6
    for query in derive_queries(question, mode):
        for hit in search(conn, query, limit=per_query_limit):
            key = (hit.page_id, hit.chunk_index)
            existing = by_id.get(key)
            if existing is None or hit.score > existing.score:
                by_id[key] = hit
    ranked = sorted(by_id.values(), key=lambda hit: mode_rank_key(hit, mode), reverse=True)
    return diversify_hits(ranked, per_page_limit=3 if mode == "strict" else 2)


def search_meta(question: str, hits: list[SearchHit], mode: str = "balanced") -> dict[str, object]:
    page_hits = unique_page_hits(hits)
    return {
        "mode": mode if mode in {"balanced", "strict", "broad", "recent"} else "balanced",
        "confidence": confidence_label(page_hits),
        "keywords": essential_terms(question)[:10] or extract_terms(question)[:10],
        "preferred_doc_types": sorted(question_intents(question)),
        "page_count": len(page_hits),
        "chunk_count": len(hits),
        "top_score": round(page_hits[0].score, 2) if page_hits else 0,
        "doc_type_counts": {
            doc_type: sum(1 for hit in page_hits if hit.document_type == doc_type)
            for doc_type in sorted({hit.document_type for hit in page_hits})
        },
    }


def excerpt(text: str, size: int = 700) -> str:
    return text[:size] + ("..." if len(text) > size else "")


def unique_page_hits(hits: list[SearchHit]) -> list[SearchHit]:
    by_page: dict[str, SearchHit] = {}
    for hit in hits:
        existing = by_page.get(hit.page_id)
        if existing is None or hit.score > existing.score:
            by_page[hit.page_id] = hit
    return sorted(by_page.values(), key=lambda hit: (hit.score, hit.last_updated), reverse=True)


def hit_summary(hit: SearchHit) -> str:
    return (
        f"{hit.title} | 유형={hit.document_type} | 등록={hit.created_at or '-'} | "
        f"수정={hit.last_updated or '-'} | score={hit.score:.2f} | {hit.url}"
    )


def confidence_label(page_hits: list[SearchHit]) -> str:
    if not page_hits:
        return "낮음"
    top = page_hits[0]
    official_count = sum(1 for hit in page_hits[:8] if hit.document_type in {"정책", "매뉴얼", "결정사항"})
    if top.score >= 28 and official_count >= 2:
        return "높음"
    if top.score >= 16 or official_count >= 1:
        return "중간"
    return "낮음"


def report(question: str, hits: list[SearchHit]) -> str:
    terms = extract_terms(question)
    essentials = essential_terms(question)
    preferred_doc_types = question_intents(question)
    lines = [
        "# 검색 기반 답변",
        "",
        f"질문: {question}",
        f"핵심 키워드: {', '.join(essentials[:10]) if essentials else ', '.join(terms[:10]) if terms else '-'}",
        f"우선 문서 유형: {', '.join(sorted(preferred_doc_types)) if preferred_doc_types else '질문 키워드 기반'}",
        "",
    ]
    if not hits:
        return "\n".join(lines + ["검색 결과가 없습니다. 수집 범위나 키워드를 넓혀야 합니다."])

    page_hits = unique_page_hits(hits)
    confidence = confidence_label(page_hits)
    latest_hit = max(page_hits, key=lambda hit: hit.last_updated)
    top_hit = page_hits[0]
    official_like = [
        hit
        for hit in page_hits
        if hit.document_type in {"정책", "매뉴얼", "결정사항"}
        or any(term in hit.title for term in ("최종", "확정", "정책", "기준", "가이드"))
    ]
    conclusion_hit = official_like[0] if official_like else top_hit

    lines += [
        "## 1. 결론 후보",
        f"- 검색 신뢰도: `{confidence}`. 후보 문서 {len(page_hits)}개, 근거 chunk {len(hits)}개를 비교했습니다.",
        f"- 현재 검색 기준으로는 `{conclusion_hit.title}` 문서를 가장 먼저 확인하는 것이 적절합니다.",
        f"- 문서 유형은 `{conclusion_hit.document_type}`이고, 마지막 수정일은 `{conclusion_hit.last_updated or '-'}`입니다.",
        "- 아래 근거만으로 정상 여부를 확정하기 어렵다면 같은 주제의 정책/매뉴얼/이슈 문서를 추가로 확인해야 합니다.",
        "",
        "## 2. 우선 확인 문서",
    ]

    for hit in page_hits[:7]:
        lines.append(f"- {hit_summary(hit)}")

    lines += [
        "",
        "## 3. 최신성 비교",
        f"- 검색 후보 중 가장 최근 수정 문서는 `{latest_hit.title}`입니다. 수정일: {latest_hit.last_updated or '-'}",
    ]
    for hit in sorted(page_hits, key=lambda hit: hit.last_updated, reverse=True)[:8]:
        marker = "최종/정책 후보" if hit in official_like[:3] else hit.document_type
        lines.append(f"- {hit.last_updated or '-'} | {marker} | {hit.title} | {hit.url}")

    lines += [
        "",
        "## 4. 의사결정 히스토리",
    ]
    history_hits = [hit for hit in page_hits if hit.document_type in {"회의록", "결정사항", "기획서"}]
    for hit in (history_hits or page_hits[:3])[:5]:
        lines.append(f"- {hit_summary(hit)}")

    lines += [
        "",
        "## 5. 잠재 리스크",
    ]
    stale = [hit for hit in hits if recency_boost(hit.last_updated) < 0]
    issue_hits = [hit for hit in page_hits if hit.document_type == "이슈"]
    title_groups: dict[str, list[SearchHit]] = {}
    for hit in page_hits:
        normalized_title = re.sub(r"[\s_()\[\]\-]+", "", hit.title.lower())
        title_groups.setdefault(normalized_title[:24], []).append(hit)
    version_conflicts = [
        group for group in title_groups.values() if len({hit.last_updated for hit in group}) > 1 and len(group) > 1
    ]
    if stale:
        for hit in stale[:3]:
            lines.append(f"- 1년 이상 갱신되지 않았을 가능성: {hit.title} | updated={hit.last_updated}")
    if issue_hits:
        for hit in issue_hits[:3]:
            lines.append(f"- 이슈/예외 문서 후보: {hit.title} | updated={hit.last_updated} | {hit.url}")
    if version_conflicts:
        for group in version_conflicts[:2]:
            titles = ", ".join(hit.title for hit in group[:3])
            lines.append(f"- 유사 제목 문서가 여러 버전으로 검색됨: {titles}")
    if not stale and not issue_hits and not version_conflicts:
        lines.append("- 검색 결과만으로는 명확한 리스크를 특정하지 못했습니다.")
    lines.append("- 정상 여부 판단은 정책/매뉴얼 후보와 이슈/예외 후보가 같은 기준을 말하는지 대조해야 합니다.")

    lines += [
        "",
        "## 6. 추가 확인 필요",
    ]
    if not official_like:
        lines.append("- 제목이나 본문에서 `정책`, `기준`, `최종`, `확정` 성격의 문서가 강하게 검색되지 않았습니다.")
    else:
        lines.append("- 최종 판단 전 우선 확인 문서의 실제 본문에서 적용 범위, 예외 조건, 시행일을 확인하세요.")
    if len(page_hits) < 3:
        lines.append("- 검색 후보가 적습니다. 질문 키워드를 더 구체화하거나 수집 스페이스를 늘려야 합니다.")

    lines += ["", "## 검색 근거"]
    for hit in hits:
        lines.append(f"### {hit.title} ({hit.document_type})")
        lines.append(
            f"- page_id={hit.page_id}, 등록={hit.created_at or '-'}, 수정={hit.last_updated}, "
            f"chunk={hit.chunk_index}, score={hit.score:.2f}, matched={', '.join(hit.matched_terms[:10])}, url={hit.url}"
        )
        lines.append(excerpt(hit.text))
        lines.append("")
    return "\n".join(lines)


def generate_answer(question: str, hits: list[SearchHit]) -> tuple[str, str]:
    return report(question, hits), "search"


def ask(args: argparse.Namespace) -> None:
    conn = connect_db()
    hits = merged_hits(conn, args.question)
    answer, mode = generate_answer(args.question, hits)
    print(f"answer_mode={mode}\n")
    print(answer)


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Confluence context QNA prototype")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest", help="Confluence 문서를 수집합니다.")
    ingest_parser.add_argument("--space", help="Confluence space key")
    ingest_parser.add_argument("--all-spaces", action="store_true", help="접근 가능한 모든 global space를 수집합니다.")
    ingest_parser.add_argument("--limit", type=int, help="스페이스별 수집할 최대 페이지 수")
    ingest_parser.set_defaults(func=ingest)

    diagnose_parser = subparsers.add_parser("diagnose", help="Confluence 인증과 기본 권한을 점검합니다.")
    diagnose_parser.set_defaults(func=diagnose)

    ask_parser = subparsers.add_parser("ask", help="수집된 문서에서 질문 답변을 생성합니다.")
    ask_parser.add_argument("question")
    ask_parser.set_defaults(func=ask)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
