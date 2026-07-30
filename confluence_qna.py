from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import requests
from requests import HTTPError
from bs4 import BeautifulSoup
from dotenv import load_dotenv

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None


DB_PATH = Path("data/confluence_qna.sqlite3")
DEFAULT_INGEST_FETCH_LIMIT = 20
DEFAULT_INGEST_TIME_BUDGET_SECONDS = 12
DEFAULT_INGEST_MEMORY_SOFT_LIMIT_MB = 360
DEFAULT_INGEST_MAX_PAGE_TEXT_CHARS = 450_000
POSTGRES_SCHEMA_LOCK = threading.Lock()
POSTGRES_SCHEMA_READY = False


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

    def rollback(self) -> None:
        self.conn.rollback()

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


@dataclass(frozen=True)
class QueryContext:
    subjects: tuple[str, ...]
    intents: tuple[str, ...]
    constraints: tuple[str, ...]
    temporal: tuple[str, ...]
    polarity: tuple[str, ...]
    focus_terms: tuple[str, ...]


def load_config() -> Config:
    load_dotenv()
    space_weights = parse_weight_map(os.getenv("CONFLUENCE_SPACE_WEIGHTS", ""))
    doc_type_weights = parse_weight_map(os.getenv("CONFLUENCE_DOCUMENT_TYPE_WEIGHTS", ""))
    return Config(
        base_url=os.getenv("CONFLUENCE_BASE_URL", "").rstrip("/"),
        email=os.getenv("CONFLUENCE_EMAIL", ""),
        api_token=os.getenv("CONFLUENCE_API_TOKEN", ""),
        space_key=os.getenv("CONFLUENCE_SPACE_KEY") or None,
        page_limit=parse_int_env("CONFLUENCE_PAGE_LIMIT", 0),
        official_spaces=tuple(
            space.strip()
            for space in os.getenv("CONFLUENCE_OFFICIAL_SPACES", "").split(",")
            if space.strip()
        ),
        space_weights=space_weights,
        document_type_weights=doc_type_weights,
    )


def parse_int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def parse_float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


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
    global POSTGRES_SCHEMA_READY
    if psycopg is None or dict_row is None:
        raise RuntimeError("Postgres 사용을 위해 `pip install -r requirements.txt`를 실행하세요.")
    raw_conn = psycopg.connect(database_url, row_factory=dict_row, connect_timeout=3)
    conn = PostgresConnection(raw_conn)
    schema_timeout_ms = max(1000, parse_int_env("DB_SCHEMA_TIMEOUT_MS", 15000))
    statement_timeout_ms = max(1000, parse_int_env("DB_STATEMENT_TIMEOUT_MS", 4500))
    set_postgres_statement_timeout(conn, schema_timeout_ms)
    if not POSTGRES_SCHEMA_READY:
        with POSTGRES_SCHEMA_LOCK:
            if not POSTGRES_SCHEMA_READY:
                ensure_postgres_schema(conn)
                POSTGRES_SCHEMA_READY = True
    set_postgres_statement_timeout(conn, statement_timeout_ms)
    return conn


def set_postgres_statement_timeout(conn: PostgresConnection, timeout_ms: int) -> None:
    safe_timeout_ms = max(1000, min(int(timeout_ms), 120000))
    conn.execute(f"SET statement_timeout = {safe_timeout_ms}")


def ensure_postgres_schema(conn: PostgresConnection) -> None:
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
            updated_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute("ALTER TABLE ingest_progress ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE ingest_progress ADD COLUMN IF NOT EXISTS message TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_page_chunks_space ON page_chunks(space)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_page_chunks_updated ON page_chunks(last_updated)")
    conn.commit()
    backfill_page_chunks(conn)


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
            updated_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT ''
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
    ensure_column(conn, "ingest_progress", "status", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "ingest_progress", "message", "TEXT NOT NULL DEFAULT ''")
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


def current_rss_mb() -> float:
    try:
        status = Path("/proc/self/status")
        if status.exists():
            for line in status.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) / 1024
    except Exception:
        pass
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return usage / 1024 if sys.platform != "darwin" else usage / (1024 * 1024)
    except Exception:
        return 0.0


def ingest_memory_soft_limit_mb() -> float:
    return max(0.0, parse_float_env("INGEST_MEMORY_SOFT_LIMIT_MB", DEFAULT_INGEST_MEMORY_SOFT_LIMIT_MB))


def ingest_memory_status() -> dict[str, object]:
    rss_mb = round(current_rss_mb(), 1)
    limit_mb = ingest_memory_soft_limit_mb()
    return {
        "rss_mb": rss_mb,
        "soft_limit_mb": limit_mb,
        "near_limit": bool(limit_mb and rss_mb and rss_mb >= limit_mb),
    }


def should_pause_ingest(started_at: float, processed: int) -> tuple[bool, str]:
    memory = ingest_memory_status()
    if memory["near_limit"]:
        return True, f"메모리 소프트 리밋 도달: RSS {memory['rss_mb']}MB / limit {memory['soft_limit_mb']}MB"
    budget = parse_float_env("INGEST_BATCH_TIME_BUDGET_SECONDS", DEFAULT_INGEST_TIME_BUDGET_SECONDS)
    if processed > 0 and budget > 0 and time.monotonic() - started_at >= budget:
        return True, f"요청 시간 예산 도달: {round(time.monotonic() - started_at, 1)}s / {budget}s"
    return False, ""


def slim_raw_json(item: dict) -> str:
    raw = {
        key: value
        for key, value in item.items()
        if key not in {"body", "extensions", "metadata"}
    }
    return json.dumps(raw, ensure_ascii=False)


def page_record(config: Config, item: dict) -> dict[str, object]:
    version = item.get("version", {})
    history = item.get("history", {})
    author = (version.get("by") or {}).get("displayName", "unknown")
    space = (item.get("space") or {}).get("key", "")
    body = ((item.get("body") or {}).get("storage") or {}).get("value", "")
    title = item.get("title", "")
    text = clean_html(body)
    max_text_chars = max(0, parse_int_env("INGEST_MAX_PAGE_TEXT_CHARS", DEFAULT_INGEST_MAX_PAGE_TEXT_CHARS))
    if max_text_chars and len(text) > max_text_chars:
        text = text[:max_text_chars]
    created_at = history.get("createdDate", "")
    last_updated = version.get("when", "")
    url = page_url(config, item)
    return {
        "page_id": item["id"],
        "title": title,
        "text": text,
        "created_at": created_at,
        "last_updated": last_updated,
        "author": author,
        "space": space,
        "url": url,
        "raw_json": slim_raw_json(item),
        "chunks": [
            (item["id"], index, title, chunk, created_at, last_updated, author, space, url)
            for index, chunk in enumerate(split_chunks(text))
        ],
    }


def upsert_page_records(conn: sqlite3.Connection, records: list[dict[str, object]]) -> None:
    if not records:
        return
    conn.executemany(
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
        [
            (
                record["page_id"],
                record["title"],
                record["text"],
                record["created_at"],
                record["last_updated"],
                record["author"],
                record["space"],
                record["url"],
                record["raw_json"],
            )
            for record in records
        ],
    )
    page_ids = [str(record["page_id"]) for record in records]
    placeholders = ", ".join("?" for _ in page_ids)
    conn.execute(f"DELETE FROM page_chunks WHERE page_id IN ({placeholders})", page_ids)
    chunk_rows = [chunk for record in records for chunk in record["chunks"]]
    conn.executemany(
        """
        INSERT INTO page_chunks(
            page_id, chunk_index, title, text, created_at, last_updated, author, space, url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        chunk_rows,
    )


def upsert_page(conn: sqlite3.Connection, config: Config, item: dict) -> None:
    upsert_page_records(conn, [page_record(config, item)])


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
            INSERT INTO ingest_progress(space, next_start, completed, updated_at, status, message)
            VALUES (?, 0, ?, ?, ?, ?)
            ON CONFLICT(space) DO NOTHING
            """,
            (space, False, utc_now_text(), "pending", ""),
        )
    conn.commit()


def ingest_progress_status(conn) -> dict[str, object]:
    rows = conn.execute(
        """
        SELECT space, next_start, completed, updated_at, status, message
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
            "status": row["status"],
            "message": row["message"],
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
        "memory": ingest_memory_status(),
        "last_message": next((space["message"] for space in spaces if space["message"]), ""),
    }


def ingest_batch(batch_size: int = 100, reset: bool = False) -> dict[str, object]:
    config = load_config()
    require_confluence_config(config)
    conn = connect_db()
    processed = 0
    touched_spaces = []
    started_at = time.monotonic()
    requested_batch_size = max(1, batch_size)
    fetch_limit = max(1, min(parse_int_env("INGEST_FETCH_LIMIT", DEFAULT_INGEST_FETCH_LIMIT), requested_batch_size, 100))
    pause_reason = ""
    try:
        initialize_ingest_progress(conn, config, reset=reset)
        while processed < requested_batch_size:
            should_pause, pause_reason = should_pause_ingest(started_at, processed)
            if should_pause:
                break
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

            current_limit = min(fetch_limit, requested_batch_size - processed)
            results = fetch_page_batch(config, row["space"], int(row["next_start"]), current_limit)
            touched_spaces.append(row["space"])
            if not results:
                conn.execute(
                    """
                    UPDATE ingest_progress
                    SET completed = ?, status = ?, message = ?, updated_at = ?
                    WHERE space = ?
                    """,
                    (True, "completed", "스페이스 수집 완료", utc_now_text(), row["space"]),
                )
                conn.commit()
                continue

            next_start = int(row["next_start"])
            for item in results:
                upsert_page_records(conn, [page_record(config, item)])
                processed += 1
                next_start += 1
                conn.execute(
                    """
                    UPDATE ingest_progress
                    SET next_start = ?, completed = ?, status = ?, message = ?, updated_at = ?
                    WHERE space = ?
                    """,
                    (next_start, False, "running", f"{next_start}번째 위치까지 저장", utc_now_text(), row["space"]),
                )
                conn.commit()
                should_pause, pause_reason = should_pause_ingest(started_at, processed)
                if should_pause or processed >= requested_batch_size:
                    break

            if pause_reason or processed >= requested_batch_size:
                break

            if len(results) < current_limit:
                conn.execute(
                    """
                    UPDATE ingest_progress
                    SET completed = ?, status = ?, message = ?, updated_at = ?
                    WHERE space = ?
                    """,
                    (True, "completed", "스페이스 수집 완료", utc_now_text(), row["space"]),
                )
                conn.commit()

        status = ingest_progress_status(conn)
        if pause_reason:
            active_space = status.get("active_space")
            if active_space:
                conn.execute(
                    """
                    UPDATE ingest_progress
                    SET status = ?, message = ?, updated_at = ?
                    WHERE space = ?
                    """,
                    ("paused", pause_reason, utc_now_text(), active_space),
                )
                conn.commit()
                status = ingest_progress_status(conn)
        return {
            "status": "completed" if status["completed"] else "paused" if pause_reason else "running",
            "batch_size": requested_batch_size,
            "fetch_limit": fetch_limit,
            "processed": processed,
            "pause_reason": pause_reason,
            "elapsed_seconds": round(time.monotonic() - started_at, 1),
            "memory": ingest_memory_status(),
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
    return fts_query_for_terms(extract_terms(text), operator="OR") or text


def fts_query_for_terms(values: Iterable[str], operator: str = "OR") -> str:
    terms = []
    for term in values:
        if re.search(r"[\"'*()]", term):
            continue
        terms.append(term)
    safe_terms = ordered_unique(terms)[:18]
    joiner = " AND " if operator.upper() == "AND" else " OR "
    return joiner.join(safe_terms)


INTENT_KEYWORDS = {
    "최종": ("최종", "정의", "최신", "확정", "최종안", "최종본"),
    "정의": ("정의", "기준", "정책", "규칙", "가이드", "가이드라인"),
    "상태값": ("상태값", "상태", "status", "값"),
    "발주": ("발주", "주문", "오더", "order"),
    "정책": ("정책", "가이드", "가이드라인", "기준", "프로세스"),
    "리스크": ("리스크", "위험", "문제", "이슈", "상충", "예외"),
    "정상": ("정상", "검증", "점검", "확인", "이슈", "리스크"),
}

SYNONYM_GROUPS = (
    ("정책", "규정", "기준", "룰", "rule", "policy"),
    ("가이드", "가이드라인", "매뉴얼", "manual", "sop", "운영방법", "처리방법"),
    ("상태", "상태값", "status", "스테이터스"),
    ("주문", "발주", "오더", "order"),
    ("결정", "결정사항", "확정", "승인", "최종", "decision"),
    ("리스크", "위험", "문제", "이슈", "예외", "상충"),
    ("회의", "회의록", "미팅", "논의", "sync", "싱크"),
    ("정상", "검증", "점검", "확인", "유효", "valid"),
    ("변경", "수정", "업데이트", "최신", "최근", "이력"),
)

SYNONYM_LOOKUP = {
    alias.lower(): tuple(term.lower() for term in group)
    for group in SYNONYM_GROUPS
    for alias in group
}

QUERY_REWRITE_HINTS = {
    "정상": ("최신 정책 기준 예외 리스크", "운영 기준 검증 체크리스트"),
    "최종": ("최종 확정 결정사항 승인", "정책 기준 변경 이력"),
    "리스크": ("예외 상충 이슈 위험", "장애 문제 회고"),
    "상태": ("상태값 status 정의 기준", "상태 전이 프로세스"),
    "발주": ("발주 주문 오더 처리 기준", "발주 프로세스 예외"),
}

CONTEXT_INTENT_TERMS = {
    "정책 기준": ("정책", "기준", "규정", "가이드", "가이드라인", "매뉴얼", "프로세스", "sop", "rule", "policy"),
    "정상 검증": ("정상", "검증", "점검", "확인", "유효", "적용", "준수", "valid"),
    "리스크 예외": ("리스크", "위험", "문제", "이슈", "예외", "상충", "장애", "누락", "오류"),
    "결정 추적": ("결정", "결정사항", "확정", "승인", "최종", "회의록", "논의", "히스토리", "decision"),
    "상태 정의": ("상태", "상태값", "status", "전이", "값", "정의"),
    "변경 최신성": ("최신", "최근", "변경", "수정", "업데이트", "시행", "개정", "이력"),
}

CONTEXT_CONSTRAINT_TERMS = {
    "조건": ("조건", "범위", "대상", "적용", "예외", "케이스", "상태값", "상태", "권한", "역할"),
    "기간": ("최신", "최근", "현재", "최종", "변경", "개정", "시행", "이력", "업데이트"),
    "부정": ("아닌", "안됨", "안되는", "불가", "없음", "누락", "오류", "실패", "문제", "중단"),
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
    "중인지",
    "중인",
    "확인해줘",
    "알려줘",
    "찾아줘",
}

KOREAN_SUFFIXES = (
    "으로써",
    "으로서",
    "에서는",
    "에게는",
    "부터는",
    "까지는",
    "이라는",
    "해주세요",
    "해줘",
    "라는",
    "인지",
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


@lru_cache(maxsize=2048)
def context_profile(question: str) -> QueryContext:
    tokens = question_tokens(question)
    normalized = compact_text(question)
    subjects = [
        token
        for token in tokens
        if token not in INTENT_ONLY_TERMS
        and token not in STOPWORDS
        and not any(token in terms for terms in CONTEXT_INTENT_TERMS.values())
        and not any(token in terms for terms in CONTEXT_CONSTRAINT_TERMS.values())
    ]
    intents = [
        label
        for label, terms in CONTEXT_INTENT_TERMS.items()
        if any(term in normalized for term in terms)
    ]
    constraints = [
        token
        for token in tokens
        if any(token in terms for terms in CONTEXT_CONSTRAINT_TERMS.values())
        or token in {"상태값", "상태", "예외", "권한", "범위", "대상", "조건"}
    ]
    temporal = [
        token
        for token in tokens
        if token in CONTEXT_CONSTRAINT_TERMS["기간"]
    ]
    polarity = [
        token
        for token in tokens
        if token in CONTEXT_CONSTRAINT_TERMS["부정"]
    ]
    focus_terms = ordered_unique(
        [
            *subjects[:8],
            *constraints[:5],
            *temporal[:4],
            *polarity[:4],
            *(term for label in intents for term in CONTEXT_INTENT_TERMS.get(label, ())[:4]),
        ]
    )
    return QueryContext(
        subjects=tuple(subjects[:10]),
        intents=tuple(intents),
        constraints=tuple(constraints[:8]),
        temporal=tuple(temporal[:6]),
        polarity=tuple(polarity[:6]),
        focus_terms=tuple(focus_terms[:18]),
    )


def query_context_summary(question: str) -> dict[str, object]:
    profile = context_profile(question)
    missing_dimensions = context_missing_dimensions(profile)
    return {
        "subjects": list(profile.subjects),
        "intents": list(profile.intents),
        "constraints": list(profile.constraints),
        "temporal": list(profile.temporal),
        "polarity": list(profile.polarity),
        "focus_terms": list(profile.focus_terms),
        "completeness": context_completeness(profile),
        "missing_dimensions": missing_dimensions,
        "readiness": "충분" if not missing_dimensions else "보강 필요" if len(missing_dimensions) <= 2 else "부족",
    }


def context_completeness(profile: QueryContext) -> float:
    checks = [
        bool(profile.subjects),
        bool(profile.intents),
        bool(profile.constraints),
        bool(profile.temporal),
        bool(profile.polarity) or "리스크 예외" in profile.intents or "정상 검증" in profile.intents,
    ]
    return round(sum(1 for item in checks if item) / len(checks), 2)


def context_missing_dimensions(profile: QueryContext) -> list[str]:
    missing = []
    if not profile.subjects:
        missing.append("대상")
    if not profile.intents:
        missing.append("의도")
    if not profile.constraints:
        missing.append("판단 기준")
    if not profile.temporal:
        missing.append("최신성")
    if not profile.polarity and "리스크 예외" not in profile.intents and "정상 검증" not in profile.intents:
        missing.append("예외/리스크")
    return missing


def context_match_score(title: str, text: str, profile: QueryContext) -> tuple[float, list[str], dict[str, object]]:
    haystack = f"{compact_text(title)} {compact_text(text)}"
    title_text = compact_text(title)
    body_text = compact_text(text)
    score = 0.0
    signals: list[str] = []
    subject_hits = [term for term in profile.subjects if term_in_text(term, haystack)]
    body_subject_hits = [term for term in profile.subjects if term_in_text(term, body_text)]
    if profile.subjects:
        subject_ratio = len(subject_hits) / max(len(profile.subjects[:8]), 1)
        score += subject_ratio * 14.0
        if subject_ratio >= 0.5:
            signals.append("대상 매칭")
        if any(term_in_text(term, title_text) for term in subject_hits):
            score += 5.0
            signals.append("대상 제목 매칭")
        if body_subject_hits:
            score += min(len(body_subject_hits), 4) * 1.8
            signals.append("대상 본문 매칭")
    else:
        subject_ratio = 0.0

    intent_hits = []
    for label in profile.intents:
        terms = CONTEXT_INTENT_TERMS.get(label, ())
        if any(term in haystack for term in terms):
            intent_hits.append(label)
    if profile.intents:
        intent_ratio = len(intent_hits) / max(len(profile.intents), 1)
        score += intent_ratio * 10.0
        if intent_hits:
            signals.append("의도 매칭")
    else:
        intent_ratio = 0.0

    constraint_hits = [term for term in profile.constraints if term_in_text(term, haystack)]
    body_constraint_hits = [term for term in profile.constraints if term_in_text(term, body_text)]
    if profile.constraints:
        constraint_ratio = len(constraint_hits) / max(len(profile.constraints), 1)
        score += constraint_ratio * 7.0
        if constraint_hits:
            signals.append("조건 매칭")
        if body_subject_hits and body_constraint_hits:
            score += 6.0
            signals.append("대상-조건 본문 동시매칭")
    else:
        constraint_ratio = 0.0

    temporal_hits = [term for term in profile.temporal if term_in_text(term, haystack)]
    if profile.temporal:
        score += min(len(temporal_hits), 3) * 2.2
        if temporal_hits:
            signals.append("최신성 문맥")

    polarity_hits = [term for term in profile.polarity if term_in_text(term, haystack)]
    if profile.polarity:
        score += min(len(polarity_hits), 3) * 2.4
        if polarity_hits:
            signals.append("부정/예외 문맥")

    sentence_score, sentence_hits = best_context_sentence_match(text, profile)
    if sentence_score:
        score += sentence_score * 9.0
        signals.append("문장 단위 문맥 매칭")

    overall = (subject_ratio * 0.44) + (intent_ratio * 0.34) + (constraint_ratio * 0.22)
    diagnostics = {
        "context_coverage": round(overall, 2),
        "subject_hits": subject_hits[:6],
        "intent_hits": intent_hits[:6],
        "constraint_hits": constraint_hits[:6],
        "temporal_hits": temporal_hits[:4],
        "polarity_hits": polarity_hits[:4],
        "body_subject_hits": body_subject_hits[:6],
        "body_constraint_hits": body_constraint_hits[:6],
        "sentence_context_hits": sentence_hits[:8],
    }
    return score, ordered_unique(signals), diagnostics


def term_in_text(term: str, text: str) -> bool:
    if not term:
        return False
    normalized_term = compact_text(term)
    normalized_text = compact_text(text)
    return normalized_term in normalized_text or nospace_text(normalized_term) in nospace_text(normalized_text)


def best_context_sentence_match(text: str, profile: QueryContext) -> tuple[float, list[str]]:
    important_groups = [
        list(profile.subjects[:6]),
        list(profile.constraints[:4]),
        [term for label in profile.intents for term in CONTEXT_INTENT_TERMS.get(label, ())[:3]],
        list(profile.temporal[:3]),
        list(profile.polarity[:3]),
    ]
    important_groups = [group for group in important_groups if group]
    if not important_groups:
        return 0.0, []
    best_score = 0.0
    best_hits: list[str] = []
    scan_limit = max(4, parse_int_env("SEARCH_SENTENCE_SCAN_LIMIT", 8))
    scan_chars = max(1200, parse_int_env("SEARCH_TEXT_SCAN_CHARS", 3600))
    for sentence in sentence_units(text[:scan_chars])[:scan_limit]:
        hits = []
        covered_groups = 0
        for group in important_groups:
            group_hits = [term for term in group if term_in_text(term, sentence)]
            if group_hits:
                covered_groups += 1
                hits.extend(group_hits[:2])
        if not hits:
            continue
        score = covered_groups / max(len(important_groups), 1)
        if score > best_score:
            best_score = score
            best_hits = ordered_unique(hits)
    return best_score, best_hits


def synonyms_for(term: str) -> list[str]:
    normalized = normalize_token(term)
    if not normalized:
        return []
    direct = list(SYNONYM_LOOKUP.get(normalized, ()))
    contained = [
        synonym
        for key, synonyms in SYNONYM_LOOKUP.items()
        if key in normalized or normalized in key
        for synonym in synonyms
    ]
    return ordered_unique([*direct, *contained])


def expand_terms_with_synonyms(terms: Iterable[str]) -> list[str]:
    expanded: list[str] = []
    for term in terms:
        expanded.append(term)
        expanded.extend(synonyms_for(term))
    return ordered_unique(expanded)


def term_is_covered(term: str, matched_terms: Iterable[str]) -> bool:
    matched = set(ordered_unique(matched_terms))
    equivalents = set(expand_terms_with_synonyms([term]))
    return bool(matched & equivalents)


def coverage_ratio_for_terms(terms: list[str], matched_terms: Iterable[str]) -> float:
    if not terms:
        return 0.0
    covered = [term for term in terms if term_is_covered(term, matched_terms)]
    return len(covered) / max(len(terms), 1)


@lru_cache(maxsize=4096)
def semantic_tokens(text: str) -> tuple[str, ...]:
    base_tokens = expand_terms_with_synonyms(question_tokens(text))
    grams: list[str] = []
    for token in base_tokens:
        if re.search(r"[가-힣]", token) and len(token) >= 4:
            grams.extend(token[index : index + 2] for index in range(len(token) - 1))
            grams.extend(token[index : index + 3] for index in range(len(token) - 2))
    return tuple(ordered_unique([*base_tokens, *grams]))


@lru_cache(maxsize=4096)
def sentence_units(text: str) -> tuple[str, ...]:
    compact = " ".join(str(text or "").split())
    return tuple(
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?。！？])\s+|(?<=다\.)\s+|(?<=요\.)\s+", compact)
        if sentence.strip()
    )


def weighted_token_overlap(query_tokens: Iterable[str], doc_tokens: Iterable[str]) -> float:
    query_token_list = list(query_tokens)
    doc_token_list = list(doc_tokens)
    if not query_token_list or not doc_token_list:
        return 0.0
    doc_counts: dict[str, int] = {}
    for token in doc_token_list:
        doc_counts[token] = doc_counts.get(token, 0) + 1
    score = 0.0
    for token in query_token_list:
        if token not in doc_counts:
            continue
        length_weight = 1.0 + min(len(token), 8) / 8
        rarity_weight = 1.0 / (1.0 + min(doc_counts[token] - 1, 6) * 0.18)
        score += length_weight * rarity_weight
    return score / max(len(query_token_list), 1)


def best_sentence_overlap(query_tokens: list[str], text: str) -> float:
    best = 0.0
    scan_limit = max(4, parse_int_env("SEARCH_SENTENCE_SCAN_LIMIT", 8))
    scan_chars = max(1200, parse_int_env("SEARCH_TEXT_SCAN_CHARS", 3600))
    for sentence in sentence_units(text[:scan_chars])[:scan_limit]:
        score = weighted_token_overlap(query_tokens, semantic_tokens(sentence))
        if score > best:
            best = score
    return best


def adjacent_pair_hits(tokens: list[str], text: str) -> int:
    if len(tokens) < 2:
        return 0
    compact = text
    hits = 0
    for left, right in zip(tokens, tokens[1:]):
        if left in compact and right in compact:
            left_pos = compact.find(left)
            right_pos = compact.find(right)
            if left_pos >= 0 and right_pos >= 0 and abs(left_pos - right_pos) <= 160:
                hits += 1
    return hits


def context_score(
    row: sqlite3.Row,
    query: str,
    terms: list[str],
    essentials: list[str],
    preferred_doc_types: set[str],
    config: Config,
) -> tuple[float, list[str]]:
    title = compact_text(row["title"])
    text = compact_text(row["text"])
    title_no_space = nospace_text(row["title"])
    text_no_space = nospace_text(row["text"])
    document_type = classify_document(row["title"], row["text"])
    profile = context_profile(query)
    query_semantic = semantic_tokens(query)
    title_semantic = semantic_tokens(row["title"])
    text_semantic = semantic_tokens(row["text"])
    matched = ordered_unique(
        token
        for token in terms
        if token and (token in title or token in text)
    )
    semantic_overlap = weighted_token_overlap(query_semantic, text_semantic)
    title_overlap = weighted_token_overlap(query_semantic, title_semantic)
    sentence_overlap = best_sentence_overlap(query_semantic, row["text"])
    query_core = essentials[:8] or question_tokens(query)[:8]
    phrase_hits = adjacent_pair_hits(query_core, title) * 2 + adjacent_pair_hits(query_core, text)
    sentence_context_score, sentence_context_hits = best_context_sentence_match(row["text"], profile)
    context_bonus, context_signals, context_diagnostics = context_match_score(row["title"], row["text"], profile)
    score = recency_boost(row["last_updated"])
    score += semantic_overlap * 30.0
    score += title_overlap * 24.0
    score += sentence_overlap * 28.0
    score += sentence_context_score * 16.0
    score += min(phrase_hits, 6) * 4.0
    score += exactness_bonus(row, query, essentials)
    score += proximity_bonus(title, essentials) * 1.8
    score += proximity_bonus(text, essentials) * 1.2
    score += context_bonus
    matched_essentials = [term for term in essentials if term in title or term in text]
    body_essential_hits = [term for term in essentials if term_in_text(term, text)]
    if essentials:
        essential_ratio = len(matched_essentials) / max(len(essentials[:8]), 1)
        score += essential_ratio * 18.0
        score += min(len(body_essential_hits), 6) * 2.4
        if not matched_essentials and semantic_overlap < 0.18:
            score -= 18.0
        if matched_essentials and not body_essential_hits and title_overlap > semantic_overlap * 1.7:
            score -= 5.0
    if document_type in preferred_doc_types:
        score += 7.0
    elif preferred_doc_types and document_type == "일반문서":
        score -= 3.0
    if document_type in {"정책", "매뉴얼", "결정사항"}:
        score += 2.5
    if row["space"] in config.official_spaces:
        score += 4.0
    score += config.space_weights.get(row["space"], 0.0)
    score += config.document_type_weights.get(document_type, 0.0)
    for phrase in phrase_candidates(query)[:10]:
        if phrase in title:
            score += 8.0
            matched.append(phrase)
        elif phrase in text:
            score += 3.0
            matched.append(phrase)
        elif nospace_text(phrase) in title_no_space:
            score += 6.0
            matched.append(phrase)
        elif nospace_text(phrase) in text_no_space:
            score += 2.5
            matched.append(phrase)
    if semantic_overlap < 0.08 and title_overlap < 0.08:
        score -= 12.0
    context_matches = [
        *context_diagnostics.get("subject_hits", []),
        *context_diagnostics.get("constraint_hits", []),
        *context_diagnostics.get("temporal_hits", []),
        *context_diagnostics.get("polarity_hits", []),
        *sentence_context_hits,
        *context_signals,
    ]
    return score, ordered_unique([*matched, *matched_essentials, *context_matches])[:18]


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
    profile = context_profile(question)
    expanded = []
    for token in ordered_unique([*profile.focus_terms, *tokens]):
        expanded.append(token)
        expanded.extend(synonyms_for(token))
        for domain_term in DOMAIN_TERMS:
            if domain_term in token:
                expanded.append(domain_term)
                expanded.extend(synonyms_for(domain_term))
        if re.search(r"[가-힣]", token) and len(token) >= 5:
            expanded.extend(char_ngrams(token))
    compact_question = nospace_text(question)
    if re.search(r"[가-힣]", compact_question) and len(compact_question) >= 5:
        expanded.extend(char_ngrams(compact_question))
    for trigger, synonyms in INTENT_KEYWORDS.items():
        if trigger in question:
            expanded.extend(synonyms)
            expanded.extend(expand_terms_with_synonyms(synonyms))
    for intent in profile.intents:
        expanded.extend(CONTEXT_INTENT_TERMS.get(intent, ()))
    return ordered_unique(expanded)


def essential_terms(question: str) -> list[str]:
    terms = []
    profile = context_profile(question)
    terms.extend(profile.subjects)
    terms.extend(profile.constraints)
    for term in DOMAIN_TERMS:
        if term in question:
            terms.append(term)
    tokens = question_tokens(question)
    for token in tokens:
        if len(token) >= 2 and token not in STOPWORDS:
            terms.append(token)
    return ordered_unique(terms)


def row_to_hit(row: sqlite3.Row, score: float, matched_terms: Iterable[str]) -> SearchHit:
    document_type = classify_document(row["title"], row["text"])
    visible_terms = [
        term
        for term in ordered_unique(matched_terms)
        if len(term) >= 3 or term in DOMAIN_TERMS
    ]
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
        matched_terms=tuple(visible_terms),
    )


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def nospace_text(value: str) -> str:
    return re.sub(r"\s+", "", compact_text(value))


def char_ngrams(value: str, sizes: tuple[int, ...] = (2, 3, 4)) -> list[str]:
    compact = nospace_text(value)
    grams = []
    for size in sizes:
        if len(compact) < size:
            continue
        grams.extend(compact[index : index + size] for index in range(len(compact) - size + 1))
    return ordered_unique(grams)


def phrase_candidates(question: str) -> list[str]:
    tokens = [token for token in question_tokens(question) if token not in INTENT_ONLY_TERMS]
    profile = context_profile(question)
    phrases = []
    normalized = compact_text(question)
    if len(normalized) >= 4:
        phrases.append(normalized)
    no_space = nospace_text(question)
    if len(no_space) >= 4 and no_space != normalized:
        phrases.append(no_space)
    phrases.extend(" ".join(tokens[index : index + 2]) for index in range(len(tokens) - 1))
    phrases.extend(" ".join(profile.subjects[index : index + 2]) for index in range(len(profile.subjects) - 1))
    phrases.extend(f"{subject} {constraint}" for subject in profile.subjects[:4] for constraint in profile.constraints[:3])
    phrases.extend(token for token in tokens if len(token) >= 4)
    return ordered_unique(phrases)


def core_query_text(question: str) -> str:
    profile = context_profile(question)
    terms = [term for term in [*profile.subjects, *profile.constraints, *essential_terms(question)] if term not in INTENT_ONLY_TERMS]
    if len(terms) >= 2:
        return " ".join(terms[:8])
    return " ".join(question_tokens(question)[:8])


def proximity_bonus(text: str, essentials: list[str]) -> float:
    positions = [text.find(term) for term in essentials[:8] if term and text.find(term) >= 0]
    if len(positions) < 2:
        return 0.0
    spread = max(positions) - min(positions)
    if spread <= 120:
        return 7.0
    if spread <= 260:
        return 4.0
    if spread <= 520:
        return 2.0
    return 0.0


def exactness_bonus(row: sqlite3.Row, query: str, essentials: list[str]) -> float:
    title = compact_text(row["title"])
    text = compact_text(row["text"])
    query_phrase = compact_text(query)
    query_no_space = nospace_text(query)
    title_no_space = nospace_text(row["title"])
    text_no_space = nospace_text(row["text"])
    score = 0.0
    if len(query_phrase) >= 6:
        if query_phrase in title:
            score += 16.0
        elif query_phrase in text:
            score += 7.0
    if len(query_no_space) >= 6:
        if query_no_space in title_no_space:
            score += 13.0
        elif query_no_space in text_no_space:
            score += 5.0
    title_essential_hits = sum(1 for term in essentials[:8] if term in title or nospace_text(term) in title_no_space)
    if title_essential_hits:
        score += min(title_essential_hits, 5) * 3.0
    return score


def hit_quality_score(hit: SearchHit, question: str) -> float:
    keywords = essential_terms(question)[:10] or extract_terms(question)[:10]
    profile = context_profile(question)
    coverage = coverage_ratio_for_terms(keywords, hit.matched_terms)
    context_bonus, _, context_diagnostics = context_match_score(hit.title, hit.text, profile)
    context_coverage = float(context_diagnostics.get("context_coverage", 0.0))
    title_coverage = sum(1 for term in keywords if term_in_text(term, hit.title)) / max(len(keywords), 1) if keywords else 0.0
    official = 1.0 if hit.document_type in {"정책", "매뉴얼", "결정사항"} else 0.0
    title_match = 1.0 if any(term in compact_text(hit.title) for term in keywords[:6]) else 0.0
    exact_phrase = 1.0 if any(phrase in compact_text(hit.title) for phrase in phrase_candidates(question)[:4]) else 0.0
    fresh = max(min(recency_boost(hit.last_updated), 2.0), -0.8)
    return (
        hit.score
        + coverage * 16.0
        + context_coverage * 14.0
        + title_coverage * 10.0
        + min(context_bonus, 12.0)
        + official * 6.0
        + title_match * 5.0
        + exact_phrase * 5.0
        + fresh
    )


def bm25_lite_score(title: str, text: str, terms: list[str], essentials: list[str]) -> tuple[float, list[str]]:
    title_text = compact_text(title)
    body_text = compact_text(text)
    title_no_space = nospace_text(title)
    body_no_space = nospace_text(text)
    doc_len = max(len(question_tokens(body_text)) + len(body_text) / 120, 1.0)
    avg_len = 220.0
    k1 = 1.2
    b = 0.72
    score = 0.0
    matched = []
    for term in ordered_unique(terms):
        if len(term) < 2:
            continue
        term_no_space = nospace_text(term)
        title_tf = title_text.count(term) + title_no_space.count(term_no_space)
        body_tf = body_text.count(term) + body_no_space.count(term_no_space)
        tf = title_tf * 3.0 + body_tf
        if tf <= 0:
            continue
        matched.append(term)
        idf = 1.0 + math.log1p(12.0 / max(1.0, len(term)))
        saturation = (tf * (k1 + 1.0)) / (tf + k1 * (1.0 - b + b * doc_len / avg_len))
        weight = 1.8 if term in essentials else 1.0
        score += idf * saturation * weight
    return score, matched


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
    bm25_score, bm25_matched = bm25_lite_score(row["title"], row["text"], terms, essentials)
    score += bm25_score * 2.8
    matched.extend(bm25_matched)
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
            matched.append(phrase)
        elif phrase in text:
            score += 6.0
            matched.append(phrase)
        elif nospace_text(phrase) in nospace_text(row["title"]):
            score += 9.0
            matched.append(phrase)
        elif nospace_text(phrase) in nospace_text(row["text"]):
            score += 4.5
            matched.append(phrase)
    score += exactness_bonus(row, query, essentials)
    score += proximity_bonus(title, essentials) * 1.6
    score += proximity_bonus(text, essentials)
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


def safe_fetchall(conn, sql: str, params: Iterable[object]) -> list:
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:
        if hasattr(conn, "rollback"):
            try:
                conn.rollback()
            except Exception:
                pass
        return []


def fast_fallback_rows(conn, query: str, limit: int = 8) -> list:
    terms = [
        term
        for term in ordered_unique([*essential_terms(query), *question_tokens(query)])
        if len(term) >= 2 and term not in INTENT_ONLY_TERMS
    ][:5]
    if not terms:
        terms = extract_terms(query)[:5]
    rows_by_id = {}
    for term in terms[:4]:
        like = f"%{term}%"
        rows = safe_fetchall(
            conn,
            """
            SELECT page_id, chunk_index, title, text, created_at, last_updated, author, space, url
            FROM page_chunks
            WHERE LOWER(title) LIKE ?
            LIMIT ?
            """,
            (like, max(limit * 3, 18)),
        )
        rows_by_id.update({(row["page_id"], row["chunk_index"]): row for row in rows})
        if len(rows_by_id) >= limit:
            break
    if len(rows_by_id) < limit and terms:
        like = f"%{terms[0]}%"
        rows = safe_fetchall(
            conn,
            """
            SELECT page_id, chunk_index, title, text, created_at, last_updated, author, space, url
            FROM page_chunks
            WHERE LOWER(text) LIKE ?
            LIMIT ?
            """,
            (like, max(limit * 2, 12)),
        )
        rows_by_id.update({(row["page_id"], row["chunk_index"]): row for row in rows})
    return list(rows_by_id.values())[: max(limit * 3, 18)]


def search(conn: sqlite3.Connection, query: str, limit: int = 8, deadline: float | None = None) -> list[SearchHit]:
    config = load_config()
    terms = extract_terms(query)
    essentials = essential_terms(query)
    profile = context_profile(query)
    preferred_doc_types = question_intents(query)
    if not terms:
        return []

    uses_postgres = getattr(conn, "is_postgres", False)
    rows_by_id: dict[tuple[str, int], sqlite3.Row] = {}
    like_clauses = []
    params = []
    candidate_limit = 8 if uses_postgres else 12
    max_candidate_floor = parse_int_env("SEARCH_MAX_CANDIDATES", 96 if uses_postgres else 120)
    candidate_terms = [
        term
        for term in ordered_unique([*profile.focus_terms, *essentials, *terms, *question_tokens(query), *expand_terms_with_synonyms(essentials)])
        if len(term) >= 2
    ][:candidate_limit]
    phrase_terms = [
        nospace_text(phrase)
        for phrase in phrase_candidates(query)
        if len(nospace_text(phrase)) >= 4
    ][:2 if uses_postgres else 5]
    candidate_terms = ordered_unique([*candidate_terms, *phrase_terms])[: candidate_limit + len(phrase_terms)]
    max_candidates = min(
        max(limit * (10 if uses_postgres else 16), 56 if uses_postgres else 72),
        max(max_candidate_floor, limit * 4),
    )

    strict_title_terms = [
        term
        for term in ordered_unique([*profile.subjects[:5], *essentials[:6]])
        if len(term) >= 2 and term not in INTENT_ONLY_TERMS
    ][:4]
    if strict_title_terms:
        title_clauses = []
        title_params = []
        for term in strict_title_terms:
            title_clauses.append("LOWER(title) LIKE ?")
            title_params.append(f"%{term}%")
        title_rows = safe_fetchall(
            conn,
            f"""
            SELECT page_id, chunk_index, title, text, created_at, last_updated, author, space, url
            FROM page_chunks
            WHERE {" AND ".join(title_clauses)}
            {"ORDER BY last_updated DESC" if not uses_postgres else ""}
            LIMIT ?
            """,
            [*title_params, max(limit * 4, 20)],
        )
        rows_by_id.update({(row["page_id"], row["chunk_index"]): row for row in title_rows})

    if not uses_postgres:
        strict_terms = ordered_unique([*profile.subjects[:4], *profile.constraints[:2]])
        if strict_terms:
            try:
                strict_rows = safe_fetchall(
                    conn,
                    """
                    SELECT c.page_id, c.chunk_index, c.title, c.text, c.created_at, c.last_updated, c.author, c.space, c.url
                    FROM page_chunks_fts
                    JOIN page_chunks c ON c.rowid = page_chunks_fts.rowid
                    WHERE page_chunks_fts MATCH ?
                    ORDER BY bm25(page_chunks_fts)
                    LIMIT ?
                    """,
                    (fts_query_for_terms(strict_terms, operator="AND"), max(limit * 7, 36)),
                )
                rows_by_id.update({(row["page_id"], row["chunk_index"]): row for row in strict_rows})
            except sqlite3.OperationalError:
                pass
        try:
            fts_rows = safe_fetchall(
                conn,
                """
                SELECT c.page_id, c.chunk_index, c.title, c.text, c.created_at, c.last_updated, c.author, c.space, c.url
                FROM page_chunks_fts
                JOIN page_chunks c ON c.rowid = page_chunks_fts.rowid
                WHERE page_chunks_fts MATCH ?
                ORDER BY bm25(page_chunks_fts)
                LIMIT ?
                """,
                (fts_query(query), max(limit * 8, 42)),
            )
            rows_by_id.update({(row["page_id"], row["chunk_index"]): row for row in fts_rows})
        except sqlite3.OperationalError:
            pass

    if deadline and time.monotonic() >= deadline:
        return []

    if not uses_postgres and len(rows_by_id) >= max(limit * 6, 36):
        candidate_terms = candidate_terms[: max(4, min(8, len(essentials) + 3))]

    exact_phrase_terms = [
        phrase
        for phrase in phrase_candidates(query)[:6 if uses_postgres else 10]
        if len(phrase) >= 4 and len(phrase) <= 80
    ]
    if exact_phrase_terms and len(rows_by_id) < max(limit * 6, 36):
        phrase_clauses = []
        phrase_params = []
        for phrase in exact_phrase_terms:
            phrase_clauses.append("(LOWER(title) LIKE ? OR LOWER(text) LIKE ?)")
            like = f"%{phrase}%"
            phrase_params.extend([like, like])
        phrase_rows = safe_fetchall(
            conn,
            f"""
            SELECT page_id, chunk_index, title, text, created_at, last_updated, author, space, url
            FROM page_chunks
            WHERE {" OR ".join(phrase_clauses)}
            {"ORDER BY last_updated DESC" if not uses_postgres else ""}
            LIMIT ?
            """,
            [*phrase_params, max(limit * 7, 42)],
        )
        rows_by_id.update({(row["page_id"], row["chunk_index"]): row for row in phrase_rows})

    if deadline and time.monotonic() >= deadline:
        return []

    strict_like_terms = [term for term in profile.subjects[:3] if len(term) >= 2]
    if strict_like_terms and len(rows_by_id) < max(limit * 5, 30):
        strict_clauses = []
        strict_params = []
        for term in strict_like_terms:
            strict_clauses.append("(LOWER(title) LIKE ? OR LOWER(text) LIKE ?)")
            like = f"%{term}%"
            strict_params.extend([like, like])
        strict_rows = safe_fetchall(
            conn,
            f"""
            SELECT page_id, chunk_index, title, text, created_at, last_updated, author, space, url
            FROM page_chunks
            WHERE {" AND ".join(strict_clauses)}
            {"ORDER BY last_updated DESC" if not uses_postgres else ""}
            LIMIT ?
            """,
            [*strict_params, max(limit * 8, 48)],
        )
        rows_by_id.update({(row["page_id"], row["chunk_index"]): row for row in strict_rows})

    for term in candidate_terms:
        like_clauses.append("(LOWER(title) LIKE ? OR LOWER(text) LIKE ?)")
        like = f"%{term}%"
        params.extend([like, like])

    if like_clauses:
        order_clause = "" if uses_postgres else "ORDER BY last_updated DESC"
        like_rows = safe_fetchall(
            conn,
            f"""
            SELECT page_id, chunk_index, title, text, created_at, last_updated, author, space, url
            FROM page_chunks
            WHERE {" OR ".join(like_clauses)}
            {order_clause}
            LIMIT ?
            """,
            [*params, max_candidates],
        )
        rows_by_id.update({(row["page_id"], row["chunk_index"]): row for row in like_rows})

    if not rows_by_id:
        rows_by_id.update({(row["page_id"], row["chunk_index"]): row for row in fast_fallback_rows(conn, query, limit)})

    hits = []
    for row in rows_by_id.values():
        if deadline and time.monotonic() >= deadline:
            break
        keyword_score, keyword_matched = term_score(row, query, terms, essentials, preferred_doc_types, config)
        semantic_score, semantic_matched = context_score(row, query, terms, essentials, preferred_doc_types, config)
        score = semantic_score + max(keyword_score, -30.0) * 0.42
        matched = ordered_unique([*semantic_matched, *keyword_matched])
        if matched and score > -20:
            hits.append(row_to_hit(row, score, matched))
    if not hits and rows_by_id:
        fallback_terms = essential_terms(query)[:8] or question_tokens(query)[:8]
        for row in list(rows_by_id.values())[:limit]:
            matched = [term for term in fallback_terms if term_in_text(term, row["title"]) or term_in_text(term, row["text"])]
            if matched:
                hits.append(row_to_hit(row, recency_boost(row["last_updated"]) + len(matched) * 6.0, matched))
    return sorted(hits, key=lambda hit: (hit.score, hit.last_updated), reverse=True)[:limit]


def derive_queries(question: str, mode: str = "balanced") -> list[str]:
    base = question.strip()
    profile = context_profile(question)
    essentials = " ".join(essential_terms(question))
    core = core_query_text(question)
    phrases = phrase_candidates(question)[:4]
    subject_query = " ".join(profile.subjects[:6])
    intent_query = " ".join(term for intent in profile.intents for term in CONTEXT_INTENT_TERMS.get(intent, ())[:3])
    context_query = " ".join(ordered_unique([*profile.subjects[:5], *profile.constraints[:4], *profile.temporal[:3], *profile.polarity[:3]]))
    rewrite_hints = [
        f"{essentials} {hint}"
        for term in essential_terms(question)[:6]
        for key, hints in QUERY_REWRITE_HINTS.items()
        if key in term or term in key
        for hint in hints
    ]
    synonym_query = " ".join(expand_terms_with_synonyms(essential_terms(question)[:6])[:10])
    prefixes = [
        core,
        context_query,
        f"{subject_query} {intent_query}".strip(),
        *phrases,
        f"{essentials} 최신 정책 최종 정의",
        f"{essentials} 상태값 기준",
        f"{essentials} 의사결정 회의록 배경",
        f"{essentials} 리스크 상충 예외",
        synonym_query,
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
    return compact_query_variants([base, core, context_query, *rewrite_hints, *prefixes])


def compact_query_variants(queries: Iterable[str]) -> list[str]:
    variants = []
    seen_signatures = set()
    for query in queries:
        terms = ordered_unique(question_tokens(query))
        if not terms:
            continue
        signature = " ".join(terms[:8])
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        variants.append(" ".join(terms[:12]))
    return variants


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
        official_bonus = 6.0 if hit.document_type in {"정책", "매뉴얼", "결정사항"} else -2.0
        return (hit.score + len(hit.matched_terms) * 1.5 + official_bonus, hit.last_updated)
    if mode == "broad":
        return (hit.score - max(hit.score - 30, 0) * 0.25, hit.last_updated)
    return (hit.score, hit.last_updated)


def final_rank_key(hit: SearchHit, question: str, mode: str) -> tuple[float, str]:
    base_score = mode_rank_key(hit, mode)[0]
    quality = hit_quality_score(hit, question)
    keywords = essential_terms(question)[:10] or extract_terms(question)[:10]
    profile = context_profile(question)
    coverage = coverage_ratio_for_terms(keywords, hit.matched_terms)
    title_coverage = sum(1 for term in keywords if term_in_text(term, hit.title)) / max(len(keywords), 1) if keywords else 0.0
    _, _, context_diagnostics = context_match_score(hit.title, hit.text, profile)
    context_coverage = float(context_diagnostics.get("context_coverage", 0.0))
    phrase_bonus = 0.0
    title = compact_text(hit.title)
    text = compact_text(hit.text)
    for phrase in phrase_candidates(question)[:4]:
        if phrase in title:
            phrase_bonus += 5.0
        elif phrase in text:
            phrase_bonus += 1.5
    coverage_penalty = 8.0 if keywords and coverage < 0.25 else 0.0
    context_penalty = 7.0 if profile.subjects and context_coverage < 0.25 else 0.0
    title_penalty = 5.0 if keywords and title_coverage < 0.15 and coverage < 0.55 else 0.0
    return (
        base_score * 0.58
        + quality * 0.34
        + context_coverage * 9.0
        + title_coverage * 14.0
        + min(phrase_bonus, 10.0)
        - coverage_penalty
        - context_penalty
        - title_penalty,
        hit.last_updated,
    )


def feedback_adjustment_map(conn: sqlite3.Connection) -> dict[str, float]:
    try:
        rows = conn.execute(
            """
            SELECT feedback, hits_json
            FROM query_history
            WHERE feedback IN ('useful', 'partial', 'bad')
            ORDER BY id DESC
            LIMIT 200
            """
        ).fetchall()
    except Exception:
        return {}
    weights = {"useful": 0.45, "partial": 0.12, "bad": -0.9}
    adjustments: dict[str, float] = {}
    for row in rows:
        try:
            hits = json.loads(row["hits_json"] or "[]")
        except Exception:
            continue
        page_ids = ordered_unique(str(hit.get("page_id") or "") for hit in hits[:6] if isinstance(hit, dict))
        feedback = str(row["feedback"] or "")
        weight = weights.get(feedback, 0.0)
        for page_id in page_ids:
            adjustments[page_id] = adjustments.get(page_id, 0.0) + weight
    return {
        page_id: max(min(score, 4.0), -6.0)
        for page_id, score in adjustments.items()
        if abs(score) >= 0.1
    }


def apply_feedback_adjustments(conn: sqlite3.Connection, hits: Iterable[SearchHit]) -> list[SearchHit]:
    adjustments = feedback_adjustment_map(conn)
    if not adjustments:
        return list(hits)
    adjusted = []
    for hit in hits:
        delta = adjustments.get(hit.page_id, 0.0)
        adjusted.append(replace(hit, score=hit.score + delta) if delta else hit)
    return adjusted


def apply_page_support_boosts(hits: Iterable[SearchHit]) -> list[SearchHit]:
    hit_list = list(hits)
    if not hit_list:
        return []
    by_page: dict[str, list[SearchHit]] = {}
    for hit in hit_list:
        by_page.setdefault(hit.page_id, []).append(hit)
    boosted = []
    for hit in hit_list:
        siblings = by_page.get(hit.page_id, [])
        page_terms = ordered_unique(term for sibling in siblings for term in sibling.matched_terms)
        chunk_support = min(max(len(siblings) - 1, 0), 4) * 1.8
        term_support = min(len(page_terms), 10) * 0.45
        official_support = 1.2 if hit.document_type in {"정책", "매뉴얼", "결정사항"} and len(siblings) >= 2 else 0.0
        boost = min(chunk_support + term_support + official_support, 9.0)
        boosted.append(replace(hit, score=hit.score + boost) if boost else hit)
    return boosted


def recent_page_frequency_penalty(conn: sqlite3.Connection) -> dict[str, float]:
    try:
        rows = conn.execute(
            """
            SELECT hits_json
            FROM query_history
            ORDER BY id DESC
            LIMIT 40
            """
        ).fetchall()
    except Exception:
        return {}
    counts: dict[str, int] = {}
    for row in rows:
        try:
            hits = json.loads(row["hits_json"] or "[]")
        except Exception:
            continue
        for page_id in ordered_unique(str(hit.get("page_id") or "") for hit in hits[:5] if isinstance(hit, dict)):
            counts[page_id] = counts.get(page_id, 0) + 1
    return {
        page_id: min(max(count - 2, 0) * 0.8, 5.5)
        for page_id, count in counts.items()
        if count >= 4
    }


def apply_recent_repeat_penalties(conn: sqlite3.Connection, hits: Iterable[SearchHit], question: str) -> list[SearchHit]:
    penalties = recent_page_frequency_penalty(conn)
    if not penalties:
        return list(hits)
    profile = context_profile(question)
    adjusted = []
    for hit in hits:
        penalty = penalties.get(hit.page_id, 0.0)
        if not penalty:
            adjusted.append(hit)
            continue
        _, _, diagnostics = context_match_score(hit.title, hit.text, profile)
        context_coverage = float(diagnostics.get("context_coverage", 0.0))
        effective_penalty = penalty * (1.0 - min(context_coverage, 0.85) * 0.7)
        adjusted.append(replace(hit, score=hit.score - effective_penalty))
    return adjusted


def merged_hits(
    conn: sqlite3.Connection,
    question: str,
    mode: str = "balanced",
    time_budget_seconds: float | None = None,
) -> list[SearchHit]:
    mode = mode if mode in {"balanced", "strict", "broad", "recent"} else "balanced"
    by_id: dict[tuple[str, int], SearchHit] = {}
    votes: dict[tuple[str, int], int] = {}
    budget = parse_float_env("SEARCH_TIME_BUDGET_SECONDS", 4.8) if time_budget_seconds is None else time_budget_seconds
    deadline = time.monotonic() + max(1.0, float(budget))
    per_query_limit = 8 if mode == "broad" else 6 if mode == "recent" else 5
    queries = derive_queries(question, mode)
    queries = queries[:5 if mode == "broad" else 4]
    if getattr(conn, "is_postgres", False):
        queries = queries[:3] if mode == "broad" else queries[:2]
        per_query_limit = min(per_query_limit, 6)
    for query in queries:
        if time.monotonic() >= deadline:
            break
        for hit in search(conn, query, limit=per_query_limit, deadline=deadline):
            key = (hit.page_id, hit.chunk_index)
            votes[key] = votes.get(key, 0) + 1
            existing = by_id.get(key)
            if existing is None or hit.score > existing.score:
                by_id[key] = hit
    consensus_hits = [
        replace(hit, score=hit.score + min(max(votes.get(key, 1) - 1, 0), 4) * 2.8)
        for key, hit in by_id.items()
    ]
    if time.monotonic() >= deadline:
        if not consensus_hits:
            fallback_rows = fast_fallback_rows(conn, question, limit=8)
            fallback_hits = []
            fallback_terms = essential_terms(question)[:8] or question_tokens(question)[:8]
            for row in fallback_rows:
                matched = [term for term in fallback_terms if term_in_text(term, row["title"]) or term_in_text(term, row["text"])]
                if matched:
                    fallback_hits.append(row_to_hit(row, recency_boost(row["last_updated"]) + len(matched) * 6.0, matched))
            return diversify_hits(sorted(fallback_hits, key=lambda hit: (hit.score, hit.last_updated), reverse=True), limit=10, per_page_limit=1)
        ranked_timeout_hits = sorted(consensus_hits, key=lambda hit: (hit.score, hit.last_updated), reverse=True)
        return diversify_hits(ranked_timeout_hits, limit=10, per_page_limit=1)
    adjusted_hits = apply_recent_repeat_penalties(
        conn,
        apply_feedback_adjustments(conn, apply_page_support_boosts(consensus_hits)),
        question,
    )
    ranked = sorted(adjusted_hits, key=lambda hit: final_rank_key(hit, question, mode), reverse=True)
    return diversify_hits(ranked, per_page_limit=2 if mode == "strict" else 1)


def search_meta(question: str, hits: list[SearchHit], mode: str = "balanced") -> dict[str, object]:
    page_hits = unique_page_hits(hits)
    keywords = essential_terms(question)[:10] or extract_terms(question)[:10]
    profile = context_profile(question)
    context = query_context_summary(question)
    diagnostic_hits = page_hits[:6]
    context_coverages = [
        context_match_score(hit.title, hit.text, profile)[2].get("context_coverage", 0.0)
        for hit in diagnostic_hits
    ]
    context_coverage = round(sum(float(value) for value in context_coverages) / max(len(context_coverages), 1), 2) if context_coverages else 0.0
    official_count = sum(1 for hit in page_hits if hit.document_type in {"정책", "매뉴얼", "결정사항"})
    stale_count = sum(1 for hit in page_hits if recency_boost(hit.last_updated) < 0)
    title_body_distribution = match_scope_distribution(diagnostic_hits, keywords)
    scope_coverage = match_scope_coverage(diagnostic_hits, keywords)
    context_gaps = query_context_gaps(diagnostic_hits, profile)
    matched_keywords = {
        keyword
        for hit in hits
        for keyword in keywords
        if term_is_covered(keyword, hit.matched_terms)
    }
    coverage_ratio = round(len(matched_keywords) / max(len(keywords), 1), 2) if keywords else 0
    missing_keywords = [keyword for keyword in keywords if keyword not in matched_keywords]
    quality_distribution = hit_quality_distribution(hits, question)
    scorecard = search_scorecard(page_hits, hits, coverage_ratio, official_count, stale_count)
    issue_code = search_quality_issue_code(page_hits, coverage_ratio, official_count, stale_count, scorecard)
    derived_queries = derive_queries(question, mode)
    score_margin = round(page_hits[0].score - page_hits[1].score, 2) if len(page_hits) >= 2 else None
    risk_flags = search_risk_flags(page_hits, coverage_ratio, official_count, stale_count, scorecard, context_coverage)
    return {
        "mode": mode if mode in {"balanced", "strict", "broad", "recent"} else "balanced",
        "confidence": confidence_label(page_hits),
        "decision_readiness": decision_readiness_label(page_hits, scorecard, official_count, context_coverage),
        "risk_flags": risk_flags,
        "keywords": keywords,
        "query_context": context,
        "context_coverage": context_coverage,
        "preferred_doc_types": sorted(question_intents(question)),
        "page_count": len(page_hits),
        "chunk_count": len(hits),
        "top_score": round(page_hits[0].score, 2) if page_hits else 0,
        "score_margin": score_margin,
        "official_count": official_count,
        "stale_count": stale_count,
        "match_scope_distribution": title_body_distribution,
        "match_scope_coverage": scope_coverage,
        "coverage_ratio": coverage_ratio,
        "missing_keywords": missing_keywords[:6],
        "context_gaps": context_gaps,
        "quality_distribution": quality_distribution,
        "scorecard": scorecard,
        "derived_query_count": len(derived_queries),
        "derived_queries": derived_queries[:6],
        "quality_issue_code": issue_code,
        "remediation_steps": search_remediation_steps(issue_code),
        "recommended_mode": recommended_mode(coverage_ratio, official_count, stale_count, len(page_hits)),
        "source_mix": source_mix_summary(page_hits),
        "latest_updated": max((hit.last_updated for hit in page_hits), default=""),
        "ranker": "hybrid-bm25-context",
        "ranker_features": [
            "BM25-lite 길이 보정",
            "문맥 overlap",
            "질문 문맥 프로파일",
            "대상/의도/조건 매칭",
            "동의어/영문 약어 확장",
            "질문 문구 exactness",
            "띄어쓰기 무시 phrase",
            "2-4글자 n-gram",
            "문서 유형/스페이스 가중치",
            "사용자 피드백 보정",
            "반복 후보 consensus 가점",
            "문서 단위 다중 근거 가점",
            "정밀 모드 공식 근거 우선",
            "본문 문장 단위 동시매칭",
            "제목 전용 후보 감점",
        ],
        "query_suggestions": query_suggestions(question, hits, coverage_ratio, official_count, stale_count),
        "quality_notes": search_quality_notes(page_hits, official_count, stale_count, coverage_ratio, scorecard),
        "doc_type_counts": {
            doc_type: sum(1 for hit in page_hits if hit.document_type == doc_type)
            for doc_type in sorted({hit.document_type for hit in page_hits})
        },
    }


def match_scope_distribution(hits: list[SearchHit], keywords: list[str]) -> dict[str, int]:
    counts = {"title": 0, "body": 0, "title_body": 0, "semantic": 0}
    for hit in hits:
        title = compact_text(hit.title)
        text = compact_text(hit.text)
        title_hit = any(term in title for term in keywords)
        body_hit = any(term in text for term in keywords)
        if title_hit and body_hit:
            counts["title_body"] += 1
        elif title_hit:
            counts["title"] += 1
        elif body_hit:
            counts["body"] += 1
        else:
            counts["semantic"] += 1
    return counts


def match_scope_coverage(hits: list[SearchHit], keywords: list[str]) -> dict[str, object]:
    title_terms = set()
    body_terms = set()
    sentence_terms = set()
    for hit in hits:
        title = compact_text(hit.title)
        body = compact_text(hit.text)
        for keyword in keywords:
            if term_in_text(keyword, title):
                title_terms.add(keyword)
            if term_in_text(keyword, body):
                body_terms.add(keyword)
        for sentence in sentence_units(hit.text)[:10]:
            matched = [keyword for keyword in keywords if term_in_text(keyword, sentence)]
            if len(matched) >= 2:
                sentence_terms.update(matched)
    total = max(len(keywords), 1)
    return {
        "title_ratio": round(len(title_terms) / total, 2) if keywords else 0.0,
        "body_ratio": round(len(body_terms) / total, 2) if keywords else 0.0,
        "sentence_ratio": round(len(sentence_terms) / total, 2) if keywords else 0.0,
        "title_terms": sorted(title_terms)[:8],
        "body_terms": sorted(body_terms)[:8],
        "sentence_terms": sorted(sentence_terms)[:8],
    }


def query_context_gaps(page_hits: list[SearchHit], profile: QueryContext) -> dict[str, list[str]]:
    haystack = " ".join(f"{compact_text(hit.title)} {compact_text(hit.text)}" for hit in page_hits[:8])
    return {
        "subjects": [term for term in profile.subjects[:8] if not term_in_text(term, haystack)],
        "constraints": [term for term in profile.constraints[:6] if not term_in_text(term, haystack)],
        "temporal": [term for term in profile.temporal[:4] if not term_in_text(term, haystack)],
        "polarity": [term for term in profile.polarity[:4] if not term_in_text(term, haystack)],
    }


def search_quality_issue_code(
    page_hits: list[SearchHit],
    coverage_ratio: float,
    official_count: int,
    stale_count: int,
    scorecard: dict[str, object],
) -> str:
    if not page_hits:
        return "no_results"
    if coverage_ratio < 0.35:
        return "low_coverage"
    if official_count == 0:
        return "no_official_sources"
    if stale_count >= max(2, len(page_hits) // 2):
        return "stale_sources"
    if len(page_hits) >= 2 and page_hits[0].score - page_hits[1].score < 3 and float(scorecard.get("overall", 0)) < 0.72:
        return "ambiguous_top_results"
    if float(scorecard.get("diversity", 0)) < 0.35 and len(page_hits) >= 2:
        return "low_diversity"
    if float(scorecard.get("strength", 0)) < 0.35:
        return "weak_top_score"
    return "healthy"


def search_remediation_steps(issue_code: str) -> list[str]:
    return {
        "no_results": [
            "넓게 모드로 재검색",
            "수집 스페이스와 문서 수 확인",
            "업무명/시스템명/영문 약어를 함께 입력",
        ],
        "low_coverage": [
            "누락 핵심어를 포함해 재질문",
            "정책/기준/예외 같은 판단 기준어 추가",
            "넓게 모드로 관련 표현 탐색",
        ],
        "no_official_sources": [
            "정밀 모드로 정책/매뉴얼 후보 우선 확인",
            "CONFLUENCE_OFFICIAL_SPACES 설정 확인",
            "공식 스페이스 가중치 부여",
        ],
        "stale_sources": [
            "최신 모드로 재검색",
            "최근 변경/최종/확정 키워드 추가",
            "문서 수집 배치 재실행",
        ],
        "low_diversity": [
            "넓게 모드로 문서 다양성 확인",
            "근거 문서 목록에서 문서유형 필터 해제",
            "질문 범위를 한 단계 넓혀 재검색",
        ],
        "ambiguous_top_results": [
            "상위 후보 2-3개 본문을 직접 비교",
            "질문에 대상 시스템명/상태값/시행일 추가",
            "정밀 모드로 공식 근거 우선 재검색",
        ],
        "weak_top_score": [
            "질문에 대상 업무와 기준을 구체화",
            "동의어/영문 약어를 함께 입력",
            "정밀 모드로 제목 매칭 후보 확인",
        ],
        "healthy": [
            "상위 공식 근거 본문 확인",
            "최신성 비교 후 최종 판단",
        ],
    }.get(issue_code, ["검색 품질 패널의 누락 핵심어와 추천 검색어를 확인"])


def search_risk_flags(
    page_hits: list[SearchHit],
    coverage_ratio: float,
    official_count: int,
    stale_count: int,
    scorecard: dict[str, object],
    context_coverage: float,
) -> list[str]:
    flags = []
    if not page_hits:
        return ["결과 없음"]
    if coverage_ratio < 0.45:
        flags.append("핵심어 부족")
    if context_coverage < 0.45:
        flags.append("문맥 부족")
    if official_count == 0:
        flags.append("공식 근거 없음")
    if stale_count >= max(2, len(page_hits) // 2):
        flags.append("최신성 취약")
    if len(page_hits) >= 2 and page_hits[0].score - page_hits[1].score < 3:
        flags.append("상위 후보 경합")
    if float(scorecard.get("diversity", 0)) < 0.35 and len(page_hits) >= 2:
        flags.append("근거 다양성 낮음")
    return flags[:5] or ["주요 리스크 낮음"]


def decision_readiness_label(
    page_hits: list[SearchHit],
    scorecard: dict[str, object],
    official_count: int,
    context_coverage: float,
) -> str:
    if not page_hits:
        return "근거 부족"
    overall = float(scorecard.get("overall", 0))
    if overall >= 0.72 and official_count >= 1 and context_coverage >= 0.55:
        return "판단 가능"
    if overall >= 0.45 or official_count >= 1:
        return "추가 확인"
    return "근거 부족"


def source_mix_summary(page_hits: list[SearchHit]) -> dict[str, object]:
    spaces = {hit.space for hit in page_hits if hit.space}
    doc_types = {hit.document_type for hit in page_hits if hit.document_type}
    official_types = {"정책", "매뉴얼", "결정사항"}
    official_like = sum(1 for hit in page_hits if hit.document_type in official_types)
    return {
        "space_count": len(spaces),
        "doc_type_count": len(doc_types),
        "official_ratio": round(official_like / max(len(page_hits), 1), 2) if page_hits else 0.0,
        "spaces": sorted(spaces)[:6],
        "doc_types": sorted(doc_types)[:6],
    }


def ranking_eval_query_for_page(title: str, document_type: str) -> str:
    title_terms = [
        term
        for raw_term in question_tokens(title)
        for term in eval_term_parts(raw_term)
        if is_informative_eval_term(term)
    ][:6]
    intent_hint = {
        "정책": "최신 정책 기준",
        "매뉴얼": "운영 매뉴얼 적용 기준",
        "결정사항": "최종 결정 근거",
        "회의록": "의사결정 배경 회의록",
        "기획서": "기획 배경 범위",
        "이슈": "리스크 예외 이슈",
    }.get(document_type, "관련 기준 확인")
    return " ".join([*title_terms[:5], intent_hint]).strip()


def eval_normalize_term(term: str) -> str:
    return re.sub(r"_+", " ", str(term or "").strip("_")).strip()


def eval_term_parts(term: str) -> list[str]:
    return [
        part
        for part in eval_normalize_term(term).split()
        if len(part) >= 2
    ]


def is_informative_eval_term(term: str) -> bool:
    term = eval_normalize_term(term)
    generic_terms = {
        "정책", "기준", "확인", "최신", "최근", "현재", "최종", "관련", "회의", "회의록",
        "현장", "스토어", "서비스", "관리", "기능", "정의", "자동화", "고객", "운영",
        "문서", "초안", "협의", "가이드", "매뉴얼", "결정", "근거", "배경",
    }
    if term in INTENT_ONLY_TERMS or term in STOPWORDS:
        return False
    if term in generic_terms:
        return False
    if re.match(r"^\d", term):
        return False
    if re.fullmatch(r"\d{1,4}", term):
        return False
    if re.fullmatch(r"v?\d+(\.\d+)*", term.lower()):
        return False
    return bool(re.search(r"[A-Za-z가-힣]", term)) and len(term) >= 2


def eval_relevant_hit_page_ids(question: str, hits: list[dict[str, object]], limit: int = 3) -> list[str]:
    essentials = [
        part
        for raw_term in (essential_terms(question)[:8] or question_tokens(question)[:8])
        for part in eval_term_parts(raw_term)
        if is_informative_eval_term(part)
    ]
    if not essentials:
        return []
    relevant = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        title = str(hit.get("title") or "")
        matched_terms = [str(term) for term in hit.get("matched_terms") or []]
        covered = [
            term
            for term in essentials
            if term_is_covered(term, matched_terms) or term_in_text(term, title)
        ]
        if len(covered) >= max(1, min(2, len(essentials))):
            page_id = str(hit.get("page_id") or "")
            if page_id:
                relevant.append(page_id)
    return ordered_unique(relevant)[:limit]


def ranking_eval_cases_from_history(conn: sqlite3.Connection, limit: int) -> list[dict[str, object]]:
    try:
        rows = conn.execute(
            """
            SELECT id, question, hits_json, feedback, created_at
            FROM query_history
            WHERE feedback IN ('useful', 'partial', 'bad')
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(limit * 2, 12),),
        ).fetchall()
    except Exception:
        return []
    cases = []
    for row in rows:
        try:
            hits = json.loads(row["hits_json"] or "[]")
        except Exception:
            continue
        feedback = str(row["feedback"] or "")
        page_ids = eval_relevant_hit_page_ids(row["question"], hits[:8])
        if not page_ids:
            if feedback != "bad":
                continue
            page_ids = ordered_unique(str(hit.get("page_id") or "") for hit in hits[:3] if isinstance(hit, dict))
            if not page_ids:
                continue
        case = {
            "id": f"history-{row['id']}",
            "source": "feedback",
            "question": row["question"],
            "expected_page_ids": page_ids[:3] if feedback in {"useful", "partial"} else [],
            "avoid_page_ids": page_ids[:2] if feedback == "bad" else [],
            "feedback": feedback,
            "created_at": row["created_at"],
        }
        cases.append(case)
        if len(cases) >= limit:
            break
    return cases


def ranking_eval_cases_from_corpus(conn: sqlite3.Connection, limit: int) -> list[dict[str, object]]:
    try:
        rows = conn.execute(
            """
            SELECT page_id, title, text, created_at, last_updated, author, space, url
            FROM pages
            WHERE LENGTH(title) >= 4 AND LENGTH(text) >= 80
            ORDER BY last_updated DESC, title
            LIMIT ?
            """,
            (max(limit * 8, 80),),
        ).fetchall()
    except Exception:
        return []
    cases = []
    seen_signatures = set()
    preferred_types = {"정책", "매뉴얼", "결정사항", "회의록", "이슈", "기획서"}
    for row in rows:
        document_type = classify_document(row["title"], row["text"])
        title_terms = [
            term
            for raw_term in question_tokens(row["title"])
            for term in eval_term_parts(raw_term)
            if is_informative_eval_term(term)
        ]
        if len(title_terms) < 3:
            continue
        signature = " ".join(title_terms[:4])
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        query = ranking_eval_query_for_page(row["title"], document_type)
        if len(question_tokens(query)) < 3:
            continue
        expected_page_ids = related_eval_page_ids_for_terms(conn, title_terms[:4], str(row["page_id"]))
        priority = 0 if document_type in preferred_types else 1
        cases.append(
            {
                "id": f"corpus-{row['page_id']}",
                "source": "corpus",
                "question": query,
                "expected_page_ids": expected_page_ids,
                "avoid_page_ids": [],
                "document_type": document_type,
                "title": row["title"],
                "space": row["space"],
                "last_updated": row["last_updated"],
                "priority": priority,
            }
        )
    cases.sort(key=lambda item: (item.get("priority", 1), item.get("source", ""), item.get("title", "")))
    return cases[:limit]


def related_eval_page_ids_for_terms(conn: sqlite3.Connection, terms: list[str], fallback_page_id: str) -> list[str]:
    terms = [term for term in terms if is_informative_eval_term(term)][:4]
    if len(terms) < 2:
        return [fallback_page_id]
    clauses = []
    params = []
    for term in terms:
        clauses.append("LOWER(title) LIKE ?")
        params.append(f"%{term.lower()}%")
    try:
        rows = conn.execute(
            f"""
            SELECT page_id, title
            FROM pages
            WHERE {" OR ".join(clauses)}
            ORDER BY last_updated DESC
            LIMIT 80
            """,
            params,
        ).fetchall()
    except Exception:
        return [fallback_page_id]
    threshold = min(3, len(terms))
    related = [
        str(row["page_id"])
        for row in rows
        if sum(1 for term in terms if term_in_text(term, row["title"])) >= threshold
    ]
    page_ids = ordered_unique([fallback_page_id, *related])
    return page_ids[:8]


def build_ranking_eval_cases(conn: sqlite3.Connection, limit: int = 24) -> list[dict[str, object]]:
    limit = max(1, min(int(limit), 80))
    history_cases = ranking_eval_cases_from_history(conn, max(4, min(limit // 3, 16)))
    corpus_cases = ranking_eval_cases_from_corpus(conn, max(1, limit - len(history_cases)))
    by_id = {}
    for case in [*history_cases, *corpus_cases]:
        by_id.setdefault(case["id"], case)
    return list(by_id.values())[:limit]


def evaluate_ranking_case(
    conn: sqlite3.Connection,
    case: dict[str, object],
    mode: str,
    time_budget_seconds: float,
) -> dict[str, object]:
    started = time.monotonic()
    hits = merged_hits(conn, str(case["question"]), mode=mode, time_budget_seconds=time_budget_seconds)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    expected = [str(page_id) for page_id in case.get("expected_page_ids", [])]
    avoid = [str(page_id) for page_id in case.get("avoid_page_ids", [])]
    ranked_page_ids = ordered_unique(hit.page_id for hit in hits)
    rank = None
    for index, page_id in enumerate(ranked_page_ids, start=1):
        if page_id in expected:
            rank = index
            break
    avoided_top = bool(ranked_page_ids and ranked_page_ids[0] in avoid)
    top_hit = hits[0] if hits else None
    return {
        "id": case["id"],
        "source": case.get("source", ""),
        "question": case["question"],
        "expected_page_ids": expected,
        "avoid_page_ids": avoid,
        "rank": rank,
        "hit_at_1": bool(rank and rank <= 1),
        "hit_at_3": bool(rank and rank <= 3),
        "hit_at_5": bool(rank and rank <= 5),
        "mrr": round(1 / rank, 3) if rank else 0.0,
        "avoided_top": avoided_top,
        "elapsed_ms": elapsed_ms,
        "top": {
            "page_id": top_hit.page_id,
            "title": top_hit.title,
            "document_type": top_hit.document_type,
            "score": round(top_hit.score, 2),
            "last_updated": top_hit.last_updated,
            "url": top_hit.url,
        } if top_hit else None,
    }


def ranking_eval_report(
    conn: sqlite3.Connection,
    limit: int = 24,
    mode: str = "balanced",
    time_budget_seconds: float | None = None,
) -> dict[str, object]:
    mode = mode if mode in {"balanced", "strict", "broad", "recent"} else "balanced"
    cases = build_ranking_eval_cases(conn, limit)
    budget = time_budget_seconds if time_budget_seconds is not None else parse_float_env("EVAL_SEARCH_TIME_BUDGET_SECONDS", 1.6)
    results = [evaluate_ranking_case(conn, case, mode, float(budget)) for case in cases]
    judged = [result for result in results if result["expected_page_ids"]]
    avoid_cases = [result for result in results if result["avoid_page_ids"]]
    avg = lambda values: round(sum(values) / max(len(values), 1), 3)
    metrics = {
        "case_count": len(results),
        "judged_count": len(judged),
        "feedback_case_count": sum(1 for result in results if result["source"] == "feedback"),
        "corpus_case_count": sum(1 for result in results if result["source"] == "corpus"),
        "hit_at_1": avg([1 if result["hit_at_1"] else 0 for result in judged]),
        "hit_at_3": avg([1 if result["hit_at_3"] else 0 for result in judged]),
        "hit_at_5": avg([1 if result["hit_at_5"] else 0 for result in judged]),
        "mrr": avg([float(result["mrr"]) for result in judged]),
        "bad_top_rate": avg([1 if result["avoided_top"] else 0 for result in avoid_cases]) if avoid_cases else 0.0,
        "avg_elapsed_ms": int(sum(result["elapsed_ms"] for result in results) / max(len(results), 1)),
    }
    failed = [
        result
        for result in results
        if (result["expected_page_ids"] and not result["hit_at_5"]) or result["avoided_top"]
    ][:8]
    return {
        "mode": mode,
        "time_budget_seconds": float(budget),
        "metrics": metrics,
        "cases": results,
        "failed_cases": failed,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "notes": ranking_eval_notes(metrics),
    }


def ranking_eval_notes(metrics: dict[str, object]) -> list[str]:
    notes = []
    if int(metrics.get("feedback_case_count", 0)) < 8:
        notes.append("피드백 라벨이 적어 corpus 제목 기반 자동 케이스 비중이 높습니다.")
    if float(metrics.get("hit_at_3", 0)) < 0.7:
        notes.append("Hit@3가 낮습니다. 제목 exactness, 공식 스페이스 가중치, 동의어 사전을 보강하세요.")
    if float(metrics.get("bad_top_rate", 0)) > 0:
        notes.append("부정확 피드백을 받은 문서가 다시 1위에 노출되는 사례가 있습니다.")
    if int(metrics.get("avg_elapsed_ms", 0)) > 1800:
        notes.append("평가 평균 검색 시간이 깁니다. 후보 상한이나 평가 시간 예산을 낮춰야 합니다.")
    if not notes:
        notes.append("현재 평가셋 기준으로 치명적인 랭킹 회귀 신호는 없습니다.")
    return notes


def search_scorecard(
    page_hits: list[SearchHit],
    hits: list[SearchHit],
    coverage_ratio: float,
    official_count: int,
    stale_count: int,
) -> dict[str, object]:
    page_count = len(page_hits)
    official_ratio = official_count / max(page_count, 1) if page_count else 0.0
    freshness_ratio = 1.0 - min(stale_count / max(page_count, 1), 1.0) if page_count else 0.0
    diversity_ratio = page_count / max(len(hits), 1) if hits else 0.0
    top_score = page_hits[0].score if page_hits else 0.0
    strength_ratio = min(max(top_score / 40.0, 0.0), 1.0)
    overall = (
        coverage_ratio * 0.34
        + official_ratio * 0.22
        + freshness_ratio * 0.18
        + diversity_ratio * 0.12
        + strength_ratio * 0.14
    )
    if not page_hits:
        label = "낮음"
    elif overall >= 0.72:
        label = "높음"
    elif overall >= 0.45:
        label = "중간"
    else:
        label = "낮음"
    return {
        "overall": round(overall, 2),
        "label": label,
        "coverage": round(coverage_ratio, 2),
        "official": round(official_ratio, 2),
        "freshness": round(freshness_ratio, 2),
        "diversity": round(diversity_ratio, 2),
        "strength": round(strength_ratio, 2),
    }


def recommended_mode(
    coverage_ratio: float,
    official_count: int,
    stale_count: int,
    page_count: int,
) -> str:
    if page_count == 0 or coverage_ratio < 0.35:
        return "broad"
    if official_count == 0 or coverage_ratio < 0.65:
        return "strict"
    if stale_count >= max(2, page_count // 2):
        return "recent"
    return "balanced"


def hit_quality_label(hit: SearchHit, question: str) -> str:
    keywords = essential_terms(question)[:10]
    coverage = len([term for term in keywords if term_is_covered(term, hit.matched_terms)]) / max(len(keywords), 1) if keywords else 0
    if coverage >= 0.75 and hit.score >= 28:
        return "강함"
    if coverage >= 0.4 or hit.score >= 16:
        return "보통"
    return "약함"


def hit_quality_distribution(hits: list[SearchHit], question: str) -> dict[str, int]:
    counts = {"강함": 0, "보통": 0, "약함": 0}
    for hit in hits:
        counts[hit_quality_label(hit, question)] += 1
    return counts


def query_suggestions(
    question: str,
    hits: list[SearchHit],
    coverage_ratio: float,
    official_count: int,
    stale_count: int,
) -> list[str]:
    base_terms = essential_terms(question)[:5] or question_tokens(question)[:5]
    suggestions = []
    for term in base_terms:
        for hint_key, hints in QUERY_REWRITE_HINTS.items():
            if hint_key in term or term in hint_key:
                suggestions.extend(f"{' '.join(base_terms)} {hint}" for hint in hints)
    if coverage_ratio < 0.55:
        suggestions.append(f"{' '.join(base_terms)} 정확한 기준 적용 범위")
    if official_count == 0:
        suggestions.append(f"{' '.join(base_terms)} 정책 매뉴얼 최종 확정")
    if stale_count:
        suggestions.append(f"{' '.join(base_terms)} 최신 변경 이력 최근 업데이트")
    if hits:
        top_terms = ordered_unique(term for hit in hits[:5] for term in hit.matched_terms[:4])
        if top_terms:
            suggestions.append(" ".join(top_terms[:6]))
    return ordered_unique(suggestions)[:4]


def search_quality_notes(
    page_hits: list[SearchHit],
    official_count: int,
    stale_count: int,
    coverage_ratio: float,
    scorecard: dict[str, object] | None = None,
) -> list[str]:
    notes = []
    if not page_hits:
        return ["검색 결과가 없습니다. 수집 범위 또는 질문 키워드를 넓혀야 합니다."]
    if len(page_hits) < 3:
        notes.append("후보 문서가 적습니다. 같은 주제의 다른 표현으로도 재질문하는 것이 좋습니다.")
    if official_count == 0:
        notes.append("정책/매뉴얼/결정사항 문서가 검색되지 않아 최종 근거로 쓰기 어렵습니다.")
    if coverage_ratio < 0.45:
        notes.append("질문 핵심어 일부만 근거에 매칭되었습니다. 질문을 더 구체화하거나 넓게 모드를 사용하세요.")
    if stale_count >= max(2, len(page_hits) // 2):
        notes.append("오래된 문서 비중이 높습니다. 최신순 정렬로 최근 변경 문서를 먼저 확인하세요.")
    if scorecard and float(scorecard.get("diversity", 0)) < 0.35 and len(page_hits) >= 2:
        notes.append("상위 근거가 일부 문서에 몰려 있습니다. 넓게 모드로 문서 다양성을 확인하세요.")
    if len(page_hits) >= 2 and page_hits[0].score - page_hits[1].score < 3:
        notes.append("상위 문서 간 점수 차가 작습니다. 결론 후보를 하나로 확정하기 전 본문을 대조하세요.")
    if not notes:
        notes.append("핵심어, 공식 문서, 최신성 기준에서 우선 검토 가능한 검색 결과입니다.")
    return notes[:4]


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


def eval_ranking(args: argparse.Namespace) -> None:
    conn = connect_db()
    try:
        report_payload = ranking_eval_report(
            conn,
            limit=args.limit,
            mode=args.mode,
            time_budget_seconds=args.time_budget,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report_payload, ensure_ascii=False, indent=2))
        return
    metrics = report_payload["metrics"]
    print("# 랭킹 평가")
    print(f"mode={report_payload['mode']} cases={metrics['case_count']} judged={metrics['judged_count']}")
    print(
        "hit@1={hit_at_1} hit@3={hit_at_3} hit@5={hit_at_5} mrr={mrr} bad_top_rate={bad_top_rate} avg_ms={avg_elapsed_ms}".format(
            **metrics
        )
    )
    for note in report_payload["notes"]:
        print(f"- {note}")
    if report_payload["failed_cases"]:
        print("\n## 실패/주의 케이스")
        for case in report_payload["failed_cases"]:
            top = case.get("top") or {}
            print(
                f"- {case['id']} rank={case['rank']} avoided_top={case['avoided_top']} "
                f"q={case['question']} top={top.get('title', '-')}"
            )


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

    eval_parser = subparsers.add_parser("eval-ranking", help="corpus/히스토리 기반 검색 랭킹 평가를 실행합니다.")
    eval_parser.add_argument("--limit", type=int, default=24, help="평가 케이스 수")
    eval_parser.add_argument("--mode", default="balanced", choices=["balanced", "strict", "broad", "recent"], help="검색 모드")
    eval_parser.add_argument("--time-budget", type=float, default=None, help="케이스별 검색 시간 예산 초")
    eval_parser.add_argument("--json", action="store_true", help="JSON으로 출력")
    eval_parser.set_defaults(func=eval_ranking)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
