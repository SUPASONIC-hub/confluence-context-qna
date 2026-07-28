from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from confluence_qna import (
    connect_db,
    generate_answer,
    ingest,
    ingest_batch,
    ingest_progress_status,
    load_config,
    merged_hits,
    search_meta,
    upsert_stored_page,
)


app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
logger = logging.getLogger(__name__)
INGEST_LOCK = threading.Lock()
INGEST_STATE = {
    "running": False,
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "limit": None,
    "error": None,
}
STATS_LOCK = threading.Lock()
ASK_CACHE_LOCK = threading.Lock()
ASK_CACHE: dict[str, dict[str, object]] = {}
LAST_STATS = {
    "page_count": 0,
    "spaces": [],
    "latest_updated": None,
    "history_count": 0,
    "answer_mode": "검색 보고서",
    "ingest": INGEST_STATE.copy(),
    "stale": True,
}


def ask_cache_ttl_seconds() -> int:
    try:
        return max(0, int(os.getenv("ASK_CACHE_TTL_SECONDS", "600")))
    except ValueError:
        return 600


def ask_cache_key(question: str, search_mode: str) -> str:
    return json.dumps(
        {"question": " ".join(question.lower().split()), "mode": search_mode},
        ensure_ascii=False,
        sort_keys=True,
    )


def get_cached_ask(question: str, search_mode: str) -> dict[str, object] | None:
    ttl = ask_cache_ttl_seconds()
    if ttl <= 0:
        return None
    key = ask_cache_key(question, search_mode)
    now = time.monotonic()
    with ASK_CACHE_LOCK:
        cached = ASK_CACHE.get(key)
        if not cached:
            return None
        if now - float(cached.get("cached_at_monotonic", 0)) > ttl:
            ASK_CACHE.pop(key, None)
            return None
        payload = dict(cached["payload"])
    payload["search_meta"] = dict(payload.get("search_meta") or {})
    payload["search_meta"]["cache_hit"] = True
    payload["search_meta"]["cache_age_seconds"] = int(now - float(cached.get("cached_at_monotonic", now)))
    return payload


def put_cached_ask(question: str, search_mode: str, payload: dict[str, object]) -> None:
    if ask_cache_ttl_seconds() <= 0:
        return
    key = ask_cache_key(question, search_mode)
    cached_payload = dict(payload)
    cached_payload.pop("id", None)
    cached_payload["search_meta"] = dict(cached_payload.get("search_meta") or {})
    cached_payload["search_meta"]["cache_hit"] = False
    with ASK_CACHE_LOCK:
        if len(ASK_CACHE) >= 80:
            oldest_key = min(ASK_CACHE, key=lambda item: ASK_CACHE[item].get("cached_at_monotonic", 0))
            ASK_CACHE.pop(oldest_key, None)
        ASK_CACHE[key] = {"cached_at_monotonic": time.monotonic(), "payload": cached_payload}


def clear_ask_cache() -> None:
    with ASK_CACHE_LOCK:
        ASK_CACHE.clear()


def database_url_info() -> dict[str, object]:
    database_url = os.getenv("DATABASE_URL", "")
    parsed = urlparse(database_url) if database_url else None
    host = parsed.hostname if parsed else None
    looks_internal = bool(host and host.startswith("dpg-") and host.endswith("-a"))
    return {
        "database_url_set": bool(database_url),
        "database_url_is_postgres": database_url.startswith(("postgres://", "postgresql://")),
        "database_url_host": host,
        "database_url_looks_internal": looks_internal,
    }


@app.after_request
def add_cache_headers(response):
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": f"API endpoint not found: {request.path}"}), 404
    return error


@app.errorhandler(500)
def internal_error(error):
    if request.path.startswith("/api/"):
        logger.exception("Unhandled API error on %s", request.path)
        return jsonify({"error": "Internal server error"}), 500
    return error


def admin_required() -> bool:
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected:
        return True
    auth = request.headers.get("Authorization", "")
    supplied = request.headers.get("X-Admin-Token", "")
    if auth.startswith("Bearer "):
        supplied = auth.removeprefix("Bearer ").strip()
    return supplied == expected


def require_admin_response():
    if admin_required():
        return None
    return jsonify({"error": "admin token required"}), 401


def init_history_table() -> None:
    conn = connect_db()
    if getattr(conn, "is_postgres", False):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS query_history (
                id SERIAL PRIMARY KEY,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                hits_json TEXT NOT NULL,
                hit_count INTEGER NOT NULL,
                answer_mode TEXT NOT NULL DEFAULT '',
                search_meta_json TEXT NOT NULL DEFAULT '{}',
                feedback TEXT NOT NULL DEFAULT '',
                feedback_note TEXT NOT NULL DEFAULT '',
                feedback_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("ALTER TABLE query_history ADD COLUMN IF NOT EXISTS search_meta_json TEXT NOT NULL DEFAULT '{}'")
        conn.execute("ALTER TABLE query_history ADD COLUMN IF NOT EXISTS feedback TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE query_history ADD COLUMN IF NOT EXISTS feedback_note TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE query_history ADD COLUMN IF NOT EXISTS feedback_at TEXT NOT NULL DEFAULT ''")
    else:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS query_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                hits_json TEXT NOT NULL,
                hit_count INTEGER NOT NULL,
                answer_mode TEXT NOT NULL DEFAULT '',
                search_meta_json TEXT NOT NULL DEFAULT '{}',
                feedback TEXT NOT NULL DEFAULT '',
                feedback_note TEXT NOT NULL DEFAULT '',
                feedback_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(query_history)").fetchall()}
        if "answer_mode" not in columns:
            conn.execute("ALTER TABLE query_history ADD COLUMN answer_mode TEXT NOT NULL DEFAULT ''")
        if "search_meta_json" not in columns:
            conn.execute("ALTER TABLE query_history ADD COLUMN search_meta_json TEXT NOT NULL DEFAULT '{}'")
        if "feedback" not in columns:
            conn.execute("ALTER TABLE query_history ADD COLUMN feedback TEXT NOT NULL DEFAULT ''")
        if "feedback_note" not in columns:
            conn.execute("ALTER TABLE query_history ADD COLUMN feedback_note TEXT NOT NULL DEFAULT ''")
        if "feedback_at" not in columns:
            conn.execute("ALTER TABLE query_history ADD COLUMN feedback_at TEXT NOT NULL DEFAULT ''")
    conn.commit()
    conn.close()


def page_stats(conn: sqlite3.Connection) -> dict[str, object]:
    config = load_config()
    uses_postgres = getattr(conn, "is_postgres", False)
    page_count = conn.execute("SELECT COUNT(*) AS count FROM pages").fetchone()["count"]
    chunk_count = conn.execute("SELECT COUNT(*) AS count FROM page_chunks").fetchone()["count"]
    space_rows = conn.execute(
        "SELECT space, COUNT(*) AS count FROM pages GROUP BY space ORDER BY count DESC"
    ).fetchall()
    latest = conn.execute("SELECT MAX(last_updated) AS latest FROM pages").fetchone()["latest"]
    history_count = conn.execute("SELECT COUNT(*) AS count FROM query_history").fetchone()["count"]
    feedback_rows = conn.execute(
        "SELECT feedback, COUNT(*) AS count FROM query_history WHERE feedback <> '' GROUP BY feedback"
    ).fetchall()
    ingest_state = dict(INGEST_STATE)
    ingest_state["progress"] = ingest_progress_status(conn)
    chunks_per_page = round(chunk_count / max(page_count, 1), 1) if page_count else 0
    index_health = "empty" if page_count == 0 else "thin" if chunks_per_page < 1 else "ready"
    return {
        "page_count": page_count,
        "chunk_count": chunk_count,
        "spaces": [{"space": row["space"], "count": row["count"]} for row in space_rows],
        "latest_updated": latest,
        "history_count": history_count,
        "feedback": {row["feedback"]: row["count"] for row in feedback_rows},
        "answer_mode": "검색 보고서",
        "ingest": ingest_state,
        "weights": {
            "official_spaces": list(config.official_spaces),
            "space_weights": config.space_weights,
            "document_type_weights": config.document_type_weights,
        },
        "search_health": {
            "chunks_per_page": chunks_per_page,
            "official_spaces_configured": bool(config.official_spaces),
            "ranking_weights_configured": bool(
                config.official_spaces or config.space_weights or config.document_type_weights
            ),
            "index_health": index_health,
            "ask_cache_entries": len(ASK_CACHE),
            "ask_cache_ttl_seconds": ask_cache_ttl_seconds(),
        },
        "database": "postgres" if uses_postgres else "sqlite",
        "persistence": {
            "uses_persistent_database": uses_postgres,
            "warning": None
            if uses_postgres
            else "DATABASE_URL이 없으면 배포/재시작 시 서버 DB가 초기화될 수 있습니다.",
        },
        "stale": False,
    }


def read_page_stats_with_retry(max_attempts: int = 2) -> dict[str, object]:
    last_error = None
    for attempt in range(max_attempts):
        conn = None
        try:
            init_history_table()
            conn = connect_db()
            stats_payload = page_stats(conn)
            with STATS_LOCK:
                LAST_STATS.clear()
                LAST_STATS.update(stats_payload)
            return stats_payload
        except Exception as error:
            last_error = error
            time.sleep(0.15 * (attempt + 1))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    with STATS_LOCK:
        fallback = dict(LAST_STATS)
    fallback["ingest"] = dict(INGEST_STATE)
    fallback["stale"] = True
    fallback["database"] = "unavailable"
    fallback["persistence"] = {
        "uses_persistent_database": False,
        "warning": f"DB 연결 실패: {last_error}",
    }
    fallback["warning"] = f"통계 조회 재시도 후 마지막 정상 값을 표시합니다: {last_error}"
    return fallback


def error_payload(error: Exception) -> dict[str, str]:
    message = str(error).strip() or error.__class__.__name__
    return {"error": message[:1200]}


def focused_excerpt(text: str, terms: list[str], size: int = 520) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= size:
        return compact
    lowered = compact.lower()
    positions = [lowered.find(term.lower()) for term in terms if term and lowered.find(term.lower()) >= 0]
    center = min(positions) if positions else 0
    start = max(center - size // 3, 0)
    end = min(start + size, len(compact))
    start = max(end - size, 0)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(compact) else ""
    return f"{prefix}{compact[start:end]}{suffix}"


def db_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])


def backup_pages_payload(conn: sqlite3.Connection) -> dict[str, object]:
    rows = conn.execute(
        """
        SELECT page_id, title, text, created_at, last_updated, author, space, url, raw_json
        FROM pages
        ORDER BY space, title
        """
    ).fetchall()
    return {
        "format": "confluence-context-qna-pages",
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "page_count": len(rows),
        "pages": [
            {
                "page_id": row["page_id"],
                "title": row["title"],
                "text": row["text"],
                "created_at": row["created_at"],
                "last_updated": row["last_updated"],
                "author": row["author"],
                "space": row["space"],
                "url": row["url"],
                "raw_json": row["raw_json"],
            }
            for row in rows
        ],
    }


def run_ingest_job(limit: int | None) -> None:
    with INGEST_LOCK:
        INGEST_STATE.update(
            {
                "running": True,
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "limit": limit,
                "error": None,
            }
        )
    try:
        class Args:
            all_spaces = True
            space = None

            def __init__(self, limit_value: int | None):
                self.limit = limit_value

        ingest(Args(limit))
        clear_ask_cache()
        with INGEST_LOCK:
            INGEST_STATE.update(
                {
                    "running": False,
                    "status": "completed",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )
    except Exception as error:
        with INGEST_LOCK:
            INGEST_STATE.update(
                {
                    "running": False,
                    "status": "failed",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "error": str(error),
                }
            )


def hit_match_diagnostics(hit, question: str) -> dict[str, object]:
    from confluence_qna import (
        compact_text,
        context_match_score,
        context_profile,
        essential_terms,
        hit_quality_label,
        phrase_candidates,
        recency_boost,
        term_is_covered,
    )

    keywords = essential_terms(question)[:10]
    profile = context_profile(question)
    covered = [term for term in keywords if term_is_covered(term, hit.matched_terms)]
    coverage = round(len(covered) / max(len(keywords), 1), 2) if keywords else 0
    _, context_signals, context_diagnostics = context_match_score(hit.title, hit.text, profile)
    title = compact_text(hit.title)
    phrase_hits = [phrase for phrase in phrase_candidates(question)[:4] if phrase in title]
    freshness_score = recency_boost(hit.last_updated)
    reasons = []
    if coverage >= 0.75:
        reasons.append("핵심어 대부분 매칭")
    elif coverage >= 0.4:
        reasons.append("핵심어 일부 매칭")
    if phrase_hits:
        reasons.append("질문 문구 제목 매칭")
    if hit.document_type in {"정책", "매뉴얼", "결정사항"}:
        reasons.append("공식 근거 유형")
    if any(term in hit.title for term in covered):
        reasons.append("제목 매칭")
    if context_signals:
        reasons.append(context_signals[0])
    if not reasons:
        reasons.append("문맥 유사 후보")
    ranking_signals = []
    if coverage:
        ranking_signals.append({"label": "핵심어", "value": f"{round(coverage * 100)}%"})
    if hit.document_type in {"정책", "매뉴얼", "결정사항"}:
        ranking_signals.append({"label": "공식성", "value": hit.document_type})
    if phrase_hits:
        ranking_signals.append({"label": "문구", "value": "제목 일치"})
    if any(term in hit.title for term in covered):
        ranking_signals.append({"label": "제목", "value": "매칭"})
    if context_diagnostics.get("context_coverage"):
        ranking_signals.append(
            {"label": "문맥", "value": f"{round(float(context_diagnostics['context_coverage']) * 100)}%"}
        )
    for signal in context_signals[:2]:
        ranking_signals.append({"label": "신호", "value": signal})
    if freshness_score > 0:
        ranking_signals.append({"label": "최신성", "value": "양호"})
    elif freshness_score < 0:
        ranking_signals.append({"label": "최신성", "value": "오래됨"})
    if hit.score >= 28:
        ranking_signals.append({"label": "점수", "value": "강함"})
    return {
        "keyword_coverage": coverage,
        "covered_keywords": covered,
        "title_phrase_matches": phrase_hits,
        "freshness_score": round(freshness_score, 2),
        "context_coverage": context_diagnostics.get("context_coverage", 0.0),
        "context_signals": context_signals,
        "context_subject_hits": context_diagnostics.get("subject_hits", []),
        "context_intent_hits": context_diagnostics.get("intent_hits", []),
        "context_constraint_hits": context_diagnostics.get("constraint_hits", []),
        "match_reason": " · ".join(reasons[:3]),
        "quality": hit_quality_label(hit, question),
        "ranking_signals": ranking_signals[:5],
    }


def serialize_hits(hits, question: str = "") -> list[dict[str, object]]:
    from confluence_qna import essential_terms

    excerpt_terms = list(dict.fromkeys([*essential_terms(question), *(term for hit in hits for term in hit.matched_terms)]))
    return [
        {
            "page_id": hit.page_id,
            "title": hit.title,
            "created_at": hit.created_at,
            "last_updated": hit.last_updated,
            "author": hit.author,
            "space": hit.space,
            "url": hit.url,
            "score": round(hit.score, 2),
            "document_type": hit.document_type,
            "matched_terms": list(hit.matched_terms),
            "chunk_index": hit.chunk_index,
            "excerpt": focused_excerpt(hit.text, excerpt_terms),
            **hit_match_diagnostics(hit, question),
        }
        for hit in hits
    ]


@app.get("/")
def index():
    try:
        init_history_table()
    except Exception:
        logger.exception("Initial DB setup failed")
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.get("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "profile-logo.jpg", mimetype="image/jpeg")


@app.get("/api/stats")
def stats():
    return jsonify(read_page_stats_with_retry())


@app.get("/api/history")
def history():
    try:
        init_history_table()
        conn = connect_db()
    except Exception as error:
        logger.exception("History load failed")
        return jsonify([])
    try:
        rows = conn.execute(
            """
            SELECT id, question, hit_count, created_at
            FROM query_history
            ORDER BY id DESC
            LIMIT 100
            """
        ).fetchall()
        return jsonify(
            [
                {
                    "id": row["id"],
                    "question": row["question"],
                    "hit_count": row["hit_count"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        )
    finally:
        conn.close()


@app.get("/api/history/<int:history_id>")
def history_detail(history_id: int):
    init_history_table()
    conn = connect_db()
    try:
        row = conn.execute(
            """
            SELECT id, question, answer, hits_json, hit_count, answer_mode, search_meta_json,
                   feedback, feedback_note, feedback_at, created_at
            FROM query_history
            WHERE id = ?
            """,
            (history_id,),
        ).fetchone()
        if row is None:
            return jsonify({"error": "history not found"}), 404
        return jsonify(
            {
                "id": row["id"],
                "question": row["question"],
                "answer": row["answer"],
                "hits": json.loads(row["hits_json"]),
                "hit_count": row["hit_count"],
                "answer_mode": row["answer_mode"],
                "search_meta": json.loads(row["search_meta_json"] or "{}"),
                "feedback": row["feedback"],
                "feedback_note": row["feedback_note"],
                "feedback_at": row["feedback_at"],
                "created_at": row["created_at"],
            }
        )
    finally:
        conn.close()


@app.post("/api/ask")
def ask_api():
    init_history_table()
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()
    search_mode = str(payload.get("search_mode", "balanced")).strip() or "balanced"
    if not question:
        return jsonify({"error": "question is required"}), 400

    conn = connect_db()
    started = time.monotonic()
    try:
        cached_payload = get_cached_ask(question, search_mode)
        if cached_payload:
            answer = str(cached_payload["answer"])
            answer_mode = str(cached_payload["answer_mode"])
            serialized = list(cached_payload["hits"])
            meta = dict(cached_payload["search_meta"])
            meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            meta["slow_query"] = False
        else:
            hits = merged_hits(conn, question, search_mode)
            answer, answer_mode = generate_answer(question, hits)
            serialized = serialize_hits(hits, question)
            meta = search_meta(question, hits, search_mode)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            meta["elapsed_ms"] = elapsed_ms
            meta["slow_query"] = elapsed_ms >= int(os.getenv("SLOW_SEARCH_MS", "6500"))
            meta["cache_hit"] = False
            if meta["slow_query"]:
                meta.setdefault("quality_notes", []).insert(
                    0,
                    "검색 시간이 길었습니다. 정밀 모드나 더 구체적인 질문으로 후보 범위를 줄이면 안정적입니다.",
                )
        created_at = datetime.now(timezone.utc).isoformat()
        if getattr(conn, "is_postgres", False):
            cursor = conn.execute(
                """
                INSERT INTO query_history(question, answer, hits_json, hit_count, answer_mode, search_meta_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    question,
                    answer,
                    json.dumps(serialized, ensure_ascii=False),
                    len(serialized),
                    answer_mode,
                    json.dumps(meta, ensure_ascii=False),
                    created_at,
                ),
            )
            history_id = cursor.fetchone()["id"]
        else:
            cursor = conn.execute(
                """
                INSERT INTO query_history(question, answer, hits_json, hit_count, answer_mode, search_meta_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    question,
                    answer,
                    json.dumps(serialized, ensure_ascii=False),
                    len(serialized),
                    answer_mode,
                    json.dumps(meta, ensure_ascii=False),
                    created_at,
                ),
            )
            history_id = cursor.lastrowid
        conn.commit()
        response_payload = {
                "id": history_id,
                "question": question,
                "answer": answer,
                "hits": serialized,
                "hit_count": len(serialized),
                "answer_mode": answer_mode,
                "search_meta": meta,
                "created_at": created_at,
            }
        if not cached_payload:
            put_cached_ask(question, search_mode, response_payload)
        return jsonify(response_payload)
    except Exception as error:
        logger.exception("Ask API failed")
        return jsonify(error_payload(error)), 500
    finally:
        conn.close()


@app.post("/api/history/<int:history_id>/feedback")
def history_feedback(history_id: int):
    init_history_table()
    payload = request.get_json(silent=True) or {}
    feedback = str(payload.get("feedback", "")).strip()
    note = str(payload.get("note", "")).strip()[:500]
    if feedback not in {"useful", "partial", "bad"}:
        return jsonify({"error": "feedback must be useful, partial, or bad"}), 400
    conn = connect_db()
    try:
        row = conn.execute("SELECT id FROM query_history WHERE id = ?", (history_id,)).fetchone()
        if row is None:
            return jsonify({"error": "history not found"}), 404
        feedback_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE query_history
            SET feedback = ?, feedback_note = ?, feedback_at = ?
            WHERE id = ?
            """,
            (feedback, note, feedback_at, history_id),
        )
        conn.commit()
        clear_ask_cache()
        return jsonify(
            {
                "id": history_id,
                "feedback": feedback,
                "feedback_note": note,
                "feedback_at": feedback_at,
            }
        )
    finally:
        conn.close()


@app.post("/api/ingest")
def ingest_api():
    auth_error = require_admin_response()
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True) or {}
    raw_limit = payload.get("limit", 100)
    limit = None if raw_limit in (None, "", 0, "0", "all") else int(raw_limit)
    async_mode = bool(payload.get("async", limit is None))

    if async_mode:
        with INGEST_LOCK:
            if INGEST_STATE["running"]:
                return jsonify(INGEST_STATE), 409
            INGEST_STATE.update({"status": "queued", "limit": limit, "error": None})
        thread = threading.Thread(target=run_ingest_job, args=(limit,), daemon=True)
        thread.start()
        return jsonify(INGEST_STATE), 202

    run_ingest_job(limit)
    return jsonify(INGEST_STATE)


@app.get("/api/ingest/status")
def ingest_status():
    payload = dict(INGEST_STATE)
    try:
        conn = connect_db()
        try:
            payload["progress"] = ingest_progress_status(conn)
        finally:
            conn.close()
    except Exception as error:
        payload["progress_error"] = str(error)
    return jsonify(payload)


@app.post("/api/ingest/batch")
def ingest_batch_api():
    auth_error = require_admin_response()
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True) or {}
    batch_size = int(payload.get("batch_size") or 80)
    reset = bool(payload.get("reset"))
    batch_size = max(1, min(batch_size, 100))
    try:
        result = ingest_batch(batch_size=batch_size, reset=reset)
        if result.get("processed") or result.get("status") == "completed":
            clear_ask_cache()
    except Exception as error:
        logger.exception("Batch ingest failed")
        return jsonify(error_payload(error)), 502
    return jsonify(result)


@app.get("/api/admin/config")
def admin_config():
    config_error = None
    try:
        config = load_config()
        official_spaces = list(config.official_spaces)
        space_weights = config.space_weights
        document_type_weights = config.document_type_weights
    except Exception as error:
        logger.exception("Admin config load failed")
        config_error = str(error)
        official_spaces = []
        space_weights = {}
        document_type_weights = {}
    db_info = database_url_info()
    return jsonify(
        {
            "admin_token_required": bool(os.getenv("ADMIN_TOKEN", "")),
            "database_connection_error": None,
            "database_connection_ok": None,
            "database_connection_checked": False,
            **db_info,
            "document_type_weights": document_type_weights,
            "error": config_error,
            "official_spaces": official_spaces,
            "render_git_commit": os.getenv("RENDER_GIT_COMMIT", ""),
            "space_weights": space_weights,
        }
    )


@app.get("/api/admin/diagnostics")
def admin_diagnostics():
    auth_error = require_admin_response()
    if auth_error:
        return auth_error
    config = load_config()
    conn = None
    try:
        init_history_table()
        conn = connect_db()
        db_info = database_url_info()
        progress = ingest_progress_status(conn)
        page_count = db_count(conn, "pages")
        chunk_count = db_count(conn, "page_chunks")
        chunks_per_page = round(chunk_count / max(page_count, 1), 1) if page_count else 0
        index_health = "empty" if page_count == 0 else "thin" if chunks_per_page < 1 else "ready"
        payload = {
            "status": "ok",
            "database": "postgres" if getattr(conn, "is_postgres", False) else "sqlite",
            "counts": {
                "pages": page_count,
                "chunks": chunk_count,
                "history": db_count(conn, "query_history"),
            },
            "search_health": {
                "chunks_per_page": chunks_per_page,
                "index_health": index_health,
                "official_spaces_configured": bool(config.official_spaces),
                "ranking_weights_configured": bool(
                    config.official_spaces or config.space_weights or config.document_type_weights
                ),
            },
            "config": {
                "base_url_set": bool(config.base_url),
                "email_set": bool(config.email),
                "api_token_set": bool(config.api_token),
                "space_key": config.space_key,
                "admin_token_required": bool(os.getenv("ADMIN_TOKEN", "")),
                **db_info,
            },
            "persistence": {
                "uses_persistent_database": getattr(conn, "is_postgres", False),
                "warning": None
                if getattr(conn, "is_postgres", False)
                else "DATABASE_URL이 없으면 Render 배포/재시작 시 SQLite 데이터가 사라질 수 있습니다.",
            },
            "ingest_progress": progress,
        }
        missing = [
            name
            for name, is_set in (
                ("CONFLUENCE_BASE_URL", bool(config.base_url)),
                ("CONFLUENCE_EMAIL", bool(config.email)),
                ("CONFLUENCE_API_TOKEN", bool(config.api_token)),
            )
            if not is_set
        ]
        if missing:
            payload["status"] = "warning"
            payload["warning"] = f"Missing required env vars: {', '.join(missing)}"
        return jsonify(payload)
    except Exception as error:
        logger.exception("Diagnostics failed")
        db_info = database_url_info()
        config_error = None
        try:
            config = load_config()
            config_payload = {
                "base_url_set": bool(config.base_url),
                "email_set": bool(config.email),
                "api_token_set": bool(config.api_token),
                "space_key": config.space_key,
                "admin_token_required": bool(os.getenv("ADMIN_TOKEN", "")),
                **db_info,
            }
        except Exception as load_error:
            config_error = str(load_error)
            config_payload = {
                "base_url_set": False,
                "email_set": False,
                "api_token_set": False,
                "space_key": None,
                "admin_token_required": bool(os.getenv("ADMIN_TOKEN", "")),
                **db_info,
            }
        return jsonify(
            {
                "status": "error",
                "database": "postgres"
                if db_info["database_url_is_postgres"]
                else "sqlite",
                "error": str(error),
                "config_error": config_error,
                "counts": {"pages": 0, "chunks": 0, "history": 0},
                "config": config_payload,
                "persistence": {
                    "uses_persistent_database": False,
                    "warning": f"DB 연결 실패: {error}",
                },
                "ingest_progress": {
                    "total_spaces": 0,
                    "completed_spaces": 0,
                    "indexed_offsets": 0,
                    "remaining": 0,
                    "active_space": None,
                    "completed": False,
                    "spaces": [],
                },
            }
        )
    finally:
        if conn is not None:
            conn.close()


@app.get("/api/export/pages.csv")
def export_pages_csv():
    auth_error = require_admin_response()
    if auth_error:
        return auth_error
    conn = connect_db()
    try:
        rows = conn.execute(
            """
            SELECT page_id, title, created_at, last_updated, author, space, url
            FROM pages
            ORDER BY space, title
            """
        ).fetchall()
    finally:
        conn.close()

    def generate():
        yield "page_id,title,created_at,last_updated,author,space,url\n"
        for row in rows:
            values = [
                row["page_id"],
                row["title"],
                row["created_at"],
                row["last_updated"],
                row["author"],
                row["space"],
                row["url"],
            ]
            escaped = ['"' + str(value or "").replace('"', '""') + '"' for value in values]
            yield ",".join(escaped) + "\n"

    return Response(
        generate(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=confluence_pages.csv"},
    )


@app.get("/api/export/pages.json")
def export_pages_json():
    auth_error = require_admin_response()
    if auth_error:
        return auth_error
    conn = connect_db()
    try:
        payload = backup_pages_payload(conn)
    finally:
        conn.close()
    body = json.dumps(payload, ensure_ascii=False)
    return Response(
        body,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=confluence_pages_backup.json"},
    )


@app.post("/api/import/pages.json")
def import_pages_json():
    auth_error = require_admin_response()
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON backup payload is required"}), 400
    pages = payload.get("pages")
    if not isinstance(pages, list):
        return jsonify({"error": "backup payload must contain a pages array"}), 400

    conn = connect_db()
    imported = 0
    try:
        for item in pages:
            if not isinstance(item, dict):
                continue
            upsert_stored_page(conn, item)
            imported += 1
        conn.commit()
        clear_ask_cache()
        return jsonify({"status": "ok", "imported": imported, "page_count": db_count(conn, "pages")})
    except Exception as error:
        logger.exception("Page backup import failed")
        return jsonify(error_payload(error)), 400
    finally:
        conn.close()


if __name__ == "__main__":
    init_history_table()
    port = int(os.getenv("PORT", "5050"))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
