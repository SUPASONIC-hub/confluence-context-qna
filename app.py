from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

from confluence_qna import connect_db, generate_answer, ingest, merged_hits


app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
INGEST_LOCK = threading.Lock()
INGEST_STATE = {
    "running": False,
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "limit": None,
    "error": None,
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
        return jsonify({"error": "Internal server error"}), 500
    return error


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
    page_count = conn.execute("SELECT COUNT(*) AS count FROM pages").fetchone()["count"]
    space_rows = conn.execute(
        "SELECT space, COUNT(*) AS count FROM pages GROUP BY space ORDER BY count DESC"
    ).fetchall()
    latest = conn.execute("SELECT MAX(last_updated) AS latest FROM pages").fetchone()["latest"]
    history_count = conn.execute("SELECT COUNT(*) AS count FROM query_history").fetchone()["count"]
    return {
        "page_count": page_count,
        "spaces": [{"space": row["space"], "count": row["count"]} for row in space_rows],
        "latest_updated": latest,
        "history_count": history_count,
        "answer_mode": "검색 보고서",
        "ingest": INGEST_STATE,
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


@app.get("/api/stats")
def stats():
    init_history_table()
    conn = connect_db()
    try:
        return jsonify(page_stats(conn))
    finally:
        conn.close()


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
    return jsonify(INGEST_STATE)


if __name__ == "__main__":
    init_history_table()
    port = int(os.getenv("PORT", "5050"))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
