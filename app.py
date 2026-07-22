from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

from confluence_qna import connect_db, generate_answer, ingest, merged_hits


app = Flask(__name__)


def init_history_table() -> None:
    conn = connect_db()
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
    }


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
        cursor = conn.execute(
            """
            INSERT INTO query_history(question, answer, hits_json, hit_count, answer_mode, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (question, answer, json.dumps(serialized, ensure_ascii=False), len(hits), answer_mode, created_at),
        )
        conn.commit()
        return jsonify(
            {
                "id": cursor.lastrowid,
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
    limit = int(payload.get("limit") or 100)

    class Args:
        all_spaces = True
        space = None

        def __init__(self, limit_value: int):
            self.limit = limit_value

    ingest(Args(limit))
    return jsonify({"status": "completed", "limit": limit})


if __name__ == "__main__":
    init_history_table()
    port = int(os.getenv("PORT", "5050"))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
