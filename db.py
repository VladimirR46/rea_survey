import os
import sqlite3

from flask import g

DB_PATH = os.environ.get("DB_PATH", "/data/survey.sqlite3")

# Сколько ждать освобождения блокировки записи, прежде чем отдать ошибку.
# При нескольких воркерах в SQLite пишет кто-то один, остальные ждут здесь.
BUSY_TIMEOUT_S = 10.0


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_S)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app):
    """Создаёт таблицы и индексы. Скрипт идемпотентный, выполняется при
    каждом запуске каждого воркера."""
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_S)
    # WAL: чтения не блокируют записи
    conn.execute("PRAGMA journal_mode = WAL")

    schema = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    with open(schema, encoding="utf-8") as f:
        conn.executescript(f.read())

    conn.commit()
    conn.close()

    app.teardown_appcontext(close_db)
