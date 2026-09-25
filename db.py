"""Общий модуль работы с SQLite базой.

Вынесен из app.py и sync_db.py, чтобы избежать дублирования init_db/create_db.
"""
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get('DB_PATH', 'guid_cache.db')


def get_db_connection():
    """Возвращает подключение к SQLite с row_factory и WAL-режимом."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # S3.5: WAL даёт значительный буст производительности записи
    # и позволяет concurrent-чтение во время записи.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Создаёт таблицу objects и индексы, если их ещё нет. Идемпотентно.

    Безопасно вызывать при старте приложения (на уровне модуля).
    """
    conn = get_db_connection()
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS objects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT,
                name TEXT,
                guid TEXT,
                is_folder INTEGER DEFAULT 0,
                UNIQUE(type, name, is_folder)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_type_name ON objects(type, name)')
        # S3.1: дополнительный индекс для быстрого lookup по (type, name, is_folder)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_type_name_folder ON objects(type, name, is_folder)')
        conn.commit()
    finally:
        conn.close()


def load_guid_index():
    """Загружает всю таблицу objects в dict для O(1) lookup в auto_replace.

    Возвращает:
        guid_index: dict {(type, name, is_folder: bool) -> guid: str}
        guid_index_no_folder: dict {(type, name) -> [guid, ...]}
            для fallback-поиска, когда в БД нет is_folder-инфы или
            точное совпадение по is_folder не найдено.
    """
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT type, name, is_folder, guid FROM objects").fetchall()
        guid_index = {}
        guid_index_no_folder = {}
        for r in rows:
            key = (r['type'], r['name'], bool(r['is_folder']))
            guid_index[key] = r['guid']
            no_folder_key = (r['type'], r['name'])
            guid_index_no_folder.setdefault(no_folder_key, []).append(r['guid'])
        return guid_index, guid_index_no_folder
    finally:
        conn.close()
