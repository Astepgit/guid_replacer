#!/usr/bin/env python3

"""
Синхронизация GUID из 1С в локальную SQLite базу.

Читает XML-файл выгрузки 1С, определяет уникальные типы объектов,
для каждого типа выполняет запрос к 1С через HTTP-сервис и сохраняет
полученные GUID в локальную базу данных.

Может использоваться двумя способами:
1. CLI: `python sync_db.py <path_to_xml_file>` — печатает прогресс в stdout.
2. Как модуль: `from sync_db import run_sync; run_sync(xml_path, log_callback)`
   — вызывает log_callback(msg: str) для каждой строки прогресса, что позволяет
   использовать в Flask без subprocess (см. app.py: sync_db_stream).

Переменные окружения:
    MCP_USER - логин для Basic Auth (по умолчанию mcp)
    MCP_PASSWORD - пароль для Basic Auth (по умолчанию mcp)
    MCP_QUERY_URL - прямой URL HTTP-сервиса 1С
    DB_PATH - путь к локальной SQLite базе
    SYNC_HTTP_TIMEOUT - таймаут HTTP-запроса в секундах (по умолчанию 60)
    SYNC_INTER_QUERY_DELAY - пауза между запросами в секундах (по умолчанию 0.5)
"""

import json
import logging
import os
import re
import sys
import time

import requests
from requests.auth import HTTPBasicAuth

from db import get_db_connection, init_db
from parsing import CATEGORY_MAP
from parsing import parse_xml as parse_xml_objects

logger = logging.getLogger(__name__)

# URL HTTP-сервиса 1С
MCP_QUERY_URL = os.environ.get('MCP_QUERY_URL', 'http://erp-700/Vysotskaya_TA/hs/mcp-1c/query')
MCP_USER = os.environ.get('MCP_USER', 'mcp')
MCP_PASSWORD = os.environ.get('MCP_PASSWORD', 'mcp')

# Настройки синхронизации
SYNC_HTTP_TIMEOUT = int(os.environ.get('SYNC_HTTP_TIMEOUT', 60))
SYNC_INTER_QUERY_DELAY = float(os.environ.get('SYNC_INTER_QUERY_DELAY', 0.5))


def parse_xml(file_path):
    """Парсит XML-файл и возвращает множество уникальных типов объектов."""
    objects, _ = parse_xml_objects(file_path)
    types = set()
    for obj in objects:
        type_str = obj.get('_type')
        if type_str:
            types.add(type_str)
    return types


def execute_query(query):
    """Выполняет запрос к 1С через HTTP-сервис с Basic Auth.

    Возвращает:
      list — список строк при успешном запросе (может быть пустым)
      None  — при ошибке (HTTP, JSON, таймаут и т.д.)
    """
    auth = HTTPBasicAuth(MCP_USER, MCP_PASSWORD)

    # Очищаем запрос от лишних пробелов, переносов и BOM
    query_clean = query.strip()
    if query_clean.startswith('\ufeff'):
        query_clean = query_clean[1:].strip()
    query_clean = re.sub(r'^[\s\ufeff]+', '', query_clean)
    query_clean = ' '.join(query_clean.split())

    payload = {"query": query_clean}
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }

    try:
        response = requests.post(
            MCP_QUERY_URL, json=payload, headers=headers, auth=auth,
            timeout=SYNC_HTTP_TIMEOUT
        )
        if response.status_code == 200:
            try:
                data = response.json()
                return data.get('rows', [])
            except json.JSONDecodeError:
                logger.error("Ошибка парсинга JSON ответа 1С: %s", response.text[:500])
                return None
        else:
            logger.error("Ошибка HTTP %d: %s", response.status_code, response.text[:500])
            return None
    except requests.exceptions.RequestException as e:
        logger.error("Ошибка запроса к 1С: %s", e)
        return None


def _parse_is_folder(value):
    """Расширенный парсинг признака группы из 1С.

    1С может возвращать ЭтоГруппа в разных форматах:
      - JSON boolean: true / false
      - String: 'true' / 'false'
      - Russian: 'Истина' / 'Ложь' (или 'Да' / 'Нет')
      - Integer: 1 / 0
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('true', '1', 'истина', 'да', 'yes', 'y', 't')


def _fetch_is_folder_map(name, full_table):
    """Делает отдельный запрос к 1С для получения (GUID, ЭтоГруппа) для каталога.

    Используется, когда основной fallback-вариант не вернул колонку ЭтоГруппа
    (тогда все строки получили is_folder=0, и группы не будут сопоставлены).
    Возвращает dict {guid: bool} или None при ошибке.
    """
    query = f"""
    ВЫБРАТЬ
        УНИКАЛЬНЫЙИДЕНТИФИКАТОР({name}.Ссылка) КАК ГУИД,
        {name}.ЭтоГруппа КАК ЭтоГруппа
    ИЗ
        {full_table} КАК {name}
    """
    rows = execute_query(query)
    if rows is None:
        return None
    result = {}
    for row in rows:
        if len(row) >= 2 and row[0] is not None:
            guid = str(row[0])
            is_folder = _parse_is_folder(row[1]) if row[1] is not None else False
            result[guid] = is_folder
    return result


def _build_queries(name, full_table, is_catalog):
    """Возвращает список (variant_name, sql_query) для перебора.

    Порядок: от наиболее предпочтительного к запасным.
    """
    queries = []
    if is_catalog:
        queries.append(("Наименование + ЭтоГруппа", f"""
        ВЫБРАТЬ
            {name}.Наименование КАК Наименование,
            УНИКАЛЬНЫЙИДЕНТИФИКАТОР({name}.Ссылка) КАК ГУИД,
            {name}.ЭтоГруппа КАК ЭтоГруппа
        ИЗ
            {full_table} КАК {name}
        """))
    queries.append(("Наименование", f"""
    ВЫБРАТЬ
        {name}.Наименование КАК Наименование,
        УНИКАЛЬНЫЙИДЕНТИФИКАТОР({name}.Ссылка) КАК ГУИД
    ИЗ
        {full_table} КАК {name}
    """))
    if is_catalog:
        queries.append(("ПРЕДСТАВЛЕНИЕ + ЭтоГруппа", f"""
        ВЫБРАТЬ
            ПРЕДСТАВЛЕНИЕ({name}.Ссылка) КАК Наименование,
            УНИКАЛЬНЫЙИДЕНТИФИКАТОР({name}.Ссылка) КАК ГУИД,
            {name}.ЭтоГруппа КАК ЭтоГруппа
        ИЗ
            {full_table} КАК {name}
        """))
    queries.append(("ПРЕДСТАВЛЕНИЕ", f"""
    ВЫБРАТЬ
        ПРЕДСТАВЛЕНИЕ({name}.Ссылка) КАК Наименование,
        УНИКАЛЬНЫЙИДЕНТИФИКАТОР({name}.Ссылка) КАК ГУИД
    ИЗ
        {full_table} КАК {name}
    """))
    if is_catalog:
        queries.append(("Код + ЭтоГруппа", f"""
        ВЫБРАТЬ
            {name}.Код КАК Наименование,
            УНИКАЛЬНЫЙИДЕНТИФИКАТОР({name}.Ссылка) КАК ГУИД,
            {name}.ЭтоГруппа КАК ЭтоГруппа
        ИЗ
            {full_table} КАК {name}
        """))
    queries.append(("Код", f"""
    ВЫБРАТЬ
        {name}.Код КАК Наименование,
        УНИКАЛЬНЫЙИДЕНТИФИКАТОР({name}.Ссылка) КАК ГУИД
    ИЗ
        {full_table} КАК {name}
    """))
    return queries


def run_sync(xml_file, log_callback=print):
    """Запускает синхронизацию в том же процессе.

    Аргументы:
        xml_file: путь к XML-файлу выгрузки 1С
        log_callback: callable(str) — вызывается для каждой строки прогресса.
                      По умолчанию print (для CLI-режима).

    Возвращает:
        dict с ключами: total_fetched, error_types, success (bool), message (str)
    """
    if not os.path.exists(xml_file):
        msg = f"Файл {xml_file} не найден."
        log_callback(msg)
        return {'success': False, 'message': msg, 'total_fetched': 0, 'error_types': []}

    log_callback(f"Парсинг XML файла {xml_file}...")
    types = parse_xml(xml_file)
    if not types:
        msg = "Не найдено ни одного объекта в XML."
        log_callback(msg)
        return {'success': True, 'message': msg, 'total_fetched': 0, 'error_types': []}

    log_callback(f"Найдено уникальных типов объектов: {len(types)}")
    for t in types:
        log_callback(f"  - {t}")
    log_callback(f"ВСЕГО_ТИПОВ: {len(types)}")

    # Сначала собираем данные со всех типов, прежде чем трогать БД
    all_results = {}
    total_fetched = 0
    error_types = []

    for type_str in types:
        parts = type_str.split('.', 1)
        if len(parts) != 2:
            log_callback(f"Пропускаем тип '{type_str}' (не удалось разобрать категорию и имя)")
            continue
        category, name = parts
        mapped_category = CATEGORY_MAP.get(category, category)
        full_table = f"{mapped_category}.{name}"
        is_catalog = (mapped_category == 'Справочник')

        queries = _build_queries(name, full_table, is_catalog)
        log_callback(f"Обрабатывается: {full_table}")
        rows = None
        used_variant = None
        for variant_name, query in queries:
            rows = execute_query(query)
            if rows is not None:
                used_variant = variant_name
                break
            log_callback(f"  Вариант '{variant_name}' не удался, пробуем следующий...")

        # Fix: если для каталога использовался fallback-вариант БЕЗ ЭтоГруппа
        # (т.е. строки не содержат 3-й колонки или она None), делаем отдельный
        # запрос для получения (GUID, ЭтоГруппа) и дополняем строки.
        # Иначе все группы получат is_folder=0 и не будут найдены при автозамене.
        if is_catalog and rows is not None and len(rows) > 0:
            needs_is_folder = (
                len(rows[0]) < 3 or
                all(r[2] is None for r in rows if len(r) >= 3)
            )
            if needs_is_folder and 'ЭтоГруппа' not in used_variant:
                log_callback(f"  Вариант '{used_variant}' без ЭтоГруппа — дозапрашиваем...")
                is_folder_map = _fetch_is_folder_map(name, full_table)
                if is_folder_map:
                    # Дополняем каждую строку колонкой ЭтоГруппа из map
                    augmented_rows = []
                    for row in rows:
                        if len(row) >= 2 and row[1] is not None:
                            guid = str(row[1])
                            is_folder_val = is_folder_map.get(guid, False)
                            augmented_rows.append([row[0], row[1], is_folder_val])
                        else:
                            augmented_rows.append(row)
                    rows = augmented_rows
                    log_callback(f"  ЭтоГруппа получен для {len(is_folder_map)} GUID-ов")
                else:
                    log_callback(f"  Не удалось получить ЭтоГруппа для {full_table}")

        if rows is not None:
            all_results[type_str] = rows
            total_fetched += len(rows)
            log_callback(f"Получено {len(rows)} записей для {full_table} (вариант: {used_variant})")
        else:
            error_types.append(full_table)
            log_callback(f"Не удалось получить данные для {full_table}")
        time.sleep(SYNC_INTER_QUERY_DELAY)

    # Все данные собраны — теперь атомарно заменяем БД
    log_callback(f"\nВсего получено: {total_fetched} записей. Обновление базы данных...")
    # S2.2: убеждаемся, что таблица существует (на случай, если run_sync вызывается без app.py)
    init_db()
    conn = get_db_connection()
    try:
        # BEGIN IMMEDIATE берёт write-lock сразу, до начала долгой работы.
        # Без этого другой процесс может успеть записать между DELETE и INSERT.
        conn.execute("BEGIN IMMEDIATE")
        c = conn.cursor()
        c.execute('DELETE FROM objects')

        # S3.6: батч-вставка через executemany — в 5-10 раз быстрее, чем построчно.
        # Используем расширенный _parse_is_folder для поддержки разных форматов 1С.
        batch = []
        for type_str, rows in all_results.items():
            for row in rows:
                if len(row) >= 2:
                    n = str(row[0]) if row[0] is not None else ''
                    g = str(row[1]) if row[1] is not None else ''
                    is_folder_val = row[2] if len(row) >= 3 else False
                    is_folder = 1 if _parse_is_folder(is_folder_val) else 0
                    if n and g:
                        batch.append((type_str, n, g, is_folder))
        if batch:
            c.executemany(
                'INSERT OR REPLACE INTO objects (type, name, guid, is_folder) VALUES (?, ?, ?, ?)',
                batch
            )
        conn.commit()
        if error_types:
            msg = (f"Синхронизация завершена с предупреждениями. "
                   f"Всего сохранено {total_fetched} записей. "
                   f"Не удалось получить данные для: {', '.join(error_types)}")
        else:
            # Используем os.environ здесь, потому что DB_PATH в db.py вычисляется при импорте.
            db_path_display = os.environ.get('DB_PATH', 'guid_cache.db')
            msg = f"Синхронизация завершена. Всего сохранено {total_fetched} записей в {db_path_display}"
        log_callback(msg)
        return {
            'success': True,
            'message': msg,
            'total_fetched': total_fetched,
            'error_types': error_types,
        }
    except Exception as e:
        conn.rollback()
        err_msg = f"\nОшибка синхронизации: {e}. Данные откатаны (существующие данные сохранены)."
        log_callback(err_msg)
        logger.exception("Ошибка синхронизации")
        return {
            'success': False,
            'message': err_msg,
            'total_fetched': 0,
            'error_types': error_types,
        }
    finally:
        conn.close()


def main():
    """CLI-точка входа. Использует print() как log_callback."""
    if len(sys.argv) < 2:
        print("Usage: python sync_db.py <path_to_xml_file>")
        sys.exit(1)

    xml_file = sys.argv[1]
    result = run_sync(xml_file, log_callback=print)
    if not result['success']:
        sys.exit(1)


if __name__ == '__main__':
    main()
