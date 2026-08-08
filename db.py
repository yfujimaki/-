"""生成した記事解説をローカルのSQLiteに蓄積・検索するためのモジュール"""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    focus_point TEXT NOT NULL DEFAULT '',
    extra_instruction TEXT NOT NULL DEFAULT '',
    difficulty TEXT NOT NULL DEFAULT '',
    length TEXT NOT NULL DEFAULT '',
    management_view INTEGER NOT NULL DEFAULT 0,
    own_company TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    analysis_json TEXT NOT NULL
);
"""


def get_connection(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path=DB_PATH):
    with get_connection(db_path) as conn:
        conn.execute(SCHEMA)


def save_entry(
    created_at,
    title,
    body,
    focus_point,
    extra_instruction,
    difficulty,
    length,
    management_view,
    own_company,
    model,
    analysis_json,
    db_path=DB_PATH,
):
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO articles (
                created_at, title, body, focus_point, extra_instruction,
                difficulty, length, management_view, own_company, model, analysis_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                title,
                body,
                focus_point,
                extra_instruction,
                difficulty,
                length,
                int(management_view),
                own_company,
                model,
                analysis_json,
            ),
        )
        return cursor.lastrowid


def search_entries(keyword, db_path=DB_PATH):
    with get_connection(db_path) as conn:
        if keyword and keyword.strip():
            pattern = f"%{keyword.strip()}%"
            rows = conn.execute(
                """
                SELECT * FROM articles
                WHERE title LIKE ? OR body LIKE ? OR analysis_json LIKE ?
                ORDER BY created_at DESC
                """,
                (pattern, pattern, pattern),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM articles ORDER BY created_at DESC"
            ).fetchall()
        return rows


def get_entry(entry_id, db_path=DB_PATH):
    with get_connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM articles WHERE id = ?", (entry_id,)
        ).fetchone()


def delete_entry(entry_id, db_path=DB_PATH):
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM articles WHERE id = ?", (entry_id,))
