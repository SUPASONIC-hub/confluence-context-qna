from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from confluence_qna import connect_db, generate_answer, ingest, ingest_batch, ingest_progress_status, load_config, merged_hits


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
LAST_STATS = {
    "page_count": 0,
    "spaces": [],
    "latest_updated": None,
    "history_count": 0,
    "answer_mode": "검색 보고서",
    "ingest": INGEST_STATE.copy(),
    "stale": True,
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
                created_at TEXT NOT NULL
            )
            """
        )
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
                created_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(query_history)").fetchall()}
        if "answer_mode" not in columns:
            conn.execute("ALTER TABLE query_history ADD COLUMN answer_mode TEXT NOT NULL DEFAULT ''")
    conn.commit()
    conn.close()


def page_stats(conn: sqlite3.Connection) -> dict[str, object]:
    config = load_config()
    page_count = conn.execute("SELECT COUNT(*) AS count FROM pages").fetchone()["count"]
    space_rows = conn.execute(
        "SELECT space, COUNT(*) AS count FROM pages GROUP BY space ORDER BY count DESC"
    ).fetchall()
    latest = conn.execute("SELECT MAX(last_updated) AS latest FROM pages").fetchone()["latest"]
    history_count = conn.execute("SELECT COUNT(*) AS count FROM query_history").fetchone()["count"]
    ingest_state = dict(INGEST_STATE)
    ingest_state["progress"] = ingest_progress_status(conn)
    return {
        "page_count": page_count,
        "spaces": [{"space": row["space"], "count": row["count"]} for row in space_rows],
        "latest_updated": latest,
        "history_count": history_count,
        "answer_mode": "검색 보고서",
        "ingest": ingest_state,
        "weights": {
            "official_spaces": list(config.official_spaces),
            "space_weights": config.space_weights,
            "document_type_weights": config.document_type_weights,
        },
        "stale": False,
    }


def read_page_stats_with_retry(max_attempts: int = 4) -> dict[str, object]:
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
    fallback["warning"] = f"통계 조회 재시도 후 마지막 정상 값을 표시합니다: {last_error}"
    return fallback


def error_payload(error: Exception) -> dict[str, str]:
    message = str(error).strip() or error.__class__.__name__
    return {"error": message[:1200]}


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


def serialize_hits(hits) -> list[dict[str, object]]:
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
            "excerpt": hit.text[:420] + ("..." if len(hit.text) > 420 else ""),
        }
        for hit in hits
    ]


@app.get("/")
def index():
    init_history_table()
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
    init_history_table()
    conn = connect_db()
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
            SELECT id, question, answer, hits_json, hit_count, answer_mode, created_at
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
    if not question:
        return jsonify({"error": "question is required"}), 400

    conn = connect_db()
    try:
        hits = merged_hits(conn, question)
        answer, answer_mode = generate_answer(question, hits)
        serialized = serialize_hits(hits)
        created_at = datetime.now(timezone.utc).isoformat()
        if getattr(conn, "is_postgres", False):
            cursor = conn.execute(
                """
                INSERT INTO query_history(question, answer, hits_json, hit_count, answer_mode, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (question, answer, json.dumps(serialized, ensure_ascii=False), len(hits), answer_mode, created_at),
            )
            history_id = cursor.fetchone()["id"]
        else:
            cursor = conn.execute(
                """
                INSERT INTO query_history(question, answer, hits_json, hit_count, answer_mode, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (question, answer, json.dumps(serialized, ensure_ascii=False), len(hits), answer_mode, created_at),
            )
            history_id = cursor.lastrowid
        conn.commit()
        return jsonify(
            {
                "id": history_id,
                "question": question,
                "answer": answer,
                "hits": serialized,
                "hit_count": len(hits),
                "answer_mode": answer_mode,
                "created_at": created_at,
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
    batch_size = max(1, min(batch_size, 80))
    try:
        result = ingest_batch(batch_size=batch_size, reset=reset)
    except Exception as error:
        logger.exception("Batch ingest failed")
        return jsonify(error_payload(error)), 502
    return jsonify(result)


@app.get("/api/admin/config")
def admin_config():
    config = load_config()
    return jsonify(
        {
            "admin_token_required": bool(os.getenv("ADMIN_TOKEN", "")),
            "official_spaces": list(config.official_spaces),
            "space_weights": config.space_weights,
            "document_type_weights": config.document_type_weights,
        }
    )


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


if __name__ == "__main__":
    init_history_table()
    port = int(os.getenv("PORT", "5050"))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
