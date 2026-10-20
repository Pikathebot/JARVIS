"""Full-text index over stored messages for ``recall`` (PLAN 4.18 phase 3).

An FTS5 table with external content (``messages``): the text lives once, in ``messages``; the
index is kept in step by triggers, so ``append_message``, ``replace_messages`` and deletes need no
changes. Alembic 008 creates it on the app's database; ``ensure_message_index`` does the same on a
database built from the models (the tests), and is safe to run on either.
"""
from typing import Any

STATEMENTS = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
    "content, content='messages', content_rowid='id', tokenize=\"unicode61 tokenchars '_.-'\")",
    "CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN "
    "INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content); END",
    "CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN "
    "INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content); END",
    "CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF content ON messages BEGIN "
    "INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content); "
    "INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content); END",
)
REBUILD = "INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')"
DROP = (
    "DROP TRIGGER IF EXISTS messages_fts_ai",
    "DROP TRIGGER IF EXISTS messages_fts_ad",
    "DROP TRIGGER IF EXISTS messages_fts_au",
    "DROP TABLE IF EXISTS messages_fts",
)


def ensure_message_index(connection: Any) -> None:
    """Create the index and its triggers if missing, then fill it from the existing messages.
    ``connection`` is a DB-API or SQLAlchemy connection (anything with ``execute(str)``)."""
    from sqlalchemy import text

    def run(sql: str) -> None:
        try:
            connection.execute(text(sql))
        except Exception:
            connection.execute(sql)

    exists = False
    try:
        exists = bool(connection.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'")).fetchone())
    except Exception:
        pass
    for sql in STATEMENTS:
        run(sql)
    if not exists:
        run(REBUILD)
