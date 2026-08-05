#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Скрипт для синхронизации GUID из 1С в локальную SQLite базу.
Читает XML-файл выгрузки 1С, определяет уникальные типы объектов,
для каждого типа выполняет запрос к 1С через HTTP-сервис и сохраняет
полученные GUID в локальную базу данных.

Использование:
    python sync_db.py <path_to_xml_file>

Переменные окружения:
    MCP_USER - логин для Basic Auth (по умолчанию mcp)
    MCP_PASSWORD - пароль для Basic Auth (по умолчанию mcp)
    MCP_QUERY_URL - прямой URL HTTP-сервиса 1С
"""

import sqlite3
import json
import sys
import requests
from requests.auth import HTTPBasicAuth
from lxml import etree as ET
import os
import time
import re
from parsing import get_object_type, parse_xml as parse_xml_objects

# URL HTTP-сервиса 1С
MCP_QUERY_URL = os.environ.get('MCP_QUERY_URL', 'http://erp-700/Vysotskaya_TA/hs/mcp-1c/query')
DB_PATH = os.environ.get('DB_PATH', 'guid_cache.db')
MCP_USER = os.environ.get('MCP_USER', 'mcp')
MCP_PASSWORD = os.environ.get('MCP_PASSWORD', 'mcp')

# Маппинг категорий для правильных имён таблиц в 1С
CATEGORY_MAP = {
    'Справочник': 'Справочник',
    'Документ': 'Документ',
    'План видов характеристик': 'ПланВидовХарактеристик',
    'ПланВидовХарактеристик': 'ПланВидовХарактеристик',
    'Регистр сведений': 'РегистрСведений',
    'РегистрСведений': 'РегистрСведений',
    'Регистр накопления': 'РегистрНакопления',
    'РегистрНакопления': 'РегистрНакопления',
    'Перечисление': 'Перечисление',
    'Бизнес-процесс': 'БизнесПроцесс',
    'БизнесПроцесс': 'БизнесПроцесс',
    'Задача': 'Задача',
    'Задачи': 'Задача',
    'План счетов': 'ПланСчетов',
    'ПланСчетов': 'ПланСчетов',
    'План видов расчетов': 'ПланВидовРасчета',
    'ПланВидовРасчета': 'ПланВидовРасчета',
    'Общий реквизит': 'ОбщийРеквизит',
    'ОбщийРеквизит': 'ОбщийРеквизит',
    'Нумератор': 'Нумератор',
    'Нумераторы': 'Нумератор',
}


def parse_xml(file_path):
    """Парсит XML-файл и возвращает множество уникальных типов объектов."""
    objects, _ = parse_xml_objects(file_path)
    types = set()
    for obj in objects:
        type_str = obj.get('_type')
        if type_str:
            types.add(type_str)
    return types


def create_db(db_path):
    """Создаёт таблицу objects, если её нет."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS objects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            name TEXT,
            guid TEXT,
            is_folder INTEGER DEFAULT 0,
            UNIQUE(type, name, is_folder)
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_type_name ON objects(type, name)')
    conn.commit()
    conn.close()


def execute_query(query):
    """Выполняет запрос к 1С через HTTP-сервис с Basic Auth.
    
    Возвращает:
      list — список строк при успешном запросе (может быть пустым)
      None  — при ошибке (HTTP, JSON, таймаут и т.д.)
    """
    auth = HTTPBasicAuth(MCP_USER, MCP_PASSWORD)
    print(f"Используется Basic Auth для пользователя {MCP_USER}")

    # Очищаем запрос от лишних пробелов, переносов и BOM
    query_clean = query.strip()
    if query_clean.startswith('\ufeff'):
        query_clean = query_clean[1:].strip()
    query_clean = re.sub(r'^[\s\ufeff]+', '', query_clean)
    query_clean = ' '.join(query_clean.split())
    
    print(f"Отправляемый запрос (repr): {repr(query_clean)}")
    print(f"Отправляемый запрос (как есть):\n{query_clean}")

    payload = {"query": query_clean}
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }
    
    print(f"Отправка POST на {MCP_QUERY_URL}")

    try:
        response = requests.post(MCP_QUERY_URL, json=payload, headers=headers, auth=auth, timeout=60)
        print(f"Статус ответа: {response.status_code}")
        
        if response.status_code == 200:
            try:
                data = response.json()
                print(f"Ответ получен, колонки: {data.get('columns', [])}")
                print(f"Количество строк: {len(data.get('rows', []))}")
                return data.get('rows', [])
            except json.JSONDecodeError as e:
                print(f"Ошибка парсинга JSON: {e}")
                print(f"Ответ: {response.text[:500]}")
                return None
        else:
            print(f"Ошибка HTTP: {response.status_code}")
            print(f"Тело: {response.text[:500]}")
            return None
    except Exception as e:
        print(f"Ошибка запроса: {e}")
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python sync_db.py <path_to_xml_file>")
        sys.exit(1)
    
    xml_file = sys.argv[1]
    if not os.path.exists(xml_file):
        print(f"Файл {xml_file} не найден.")
        sys.exit(1)
    
    print(f"Парсинг XML файла {xml_file}...")
    types = parse_xml(xml_file)
    if not types:
        print("Не найдено ни одного объекта в XML.")
        sys.exit(0)
    
    print(f"Найдено уникальных типов объектов: {len(types)}")
    for t in types:
        print(f"  - {t}")
    print(f"ВСЕГО_ТИПОВ: {len(types)}")
    
    print("\nСоздание локальной базы данных...")
    create_db(DB_PATH)
    
    # Сначала собираем данные со всех типов, прежде чем трогать БД
    all_results = {}  # type_str -> list of rows
    total_fetched = 0
    error_types = []
    
    for type_str in types:
        parts = type_str.split('.', 1)
        if len(parts) != 2:
            print(f"Пропускаем тип '{type_str}' (не удалось разобрать категорию и имя)")
            continue
        category, name = parts
        mapped_category = CATEGORY_MAP.get(category, category)
        full_table = f"{mapped_category}.{name}"
        is_catalog = (mapped_category == 'Справочник')
        
        # Список запросов для перебора: от наиболее предпочтительного к запасным
        # 1. Наименование + ЭтоГруппа (только для справочников)
        # 2. Наименование без ЭтоГруппа
        # 3. ПРЕДСТАВЛЕНИЕ + ЭтоГруппа (для справочников без Наименование)
        # 4. ПРЕДСТАВЛЕНИЕ без ЭтоГруппа
        # 5. Код + ЭтоГруппа (для справочников, где нет ни Наименования, ни ПРЕДСТАВЛЕНИЯ)
        # 6. Код без ЭтоГруппа
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
        
        print(f"\nОбрабатывается: {full_table}")
        rows = None
        used_variant = None
        for variant_name, query in queries:
            rows = execute_query(query)
            if rows is not None:
                # Запрос успешен (даже если 0 строк — справочник может быть пустым)
                used_variant = variant_name
                break
            print(f"  Вариант '{variant_name}' не удался, пробуем следующий...")
        
        if rows is not None:
            all_results[type_str] = rows
            total_fetched += len(rows)
            print(f"Получено {len(rows)} записей для {full_table} (вариант: {used_variant})")
        else:
            error_types.append(full_table)
            print(f"Не удалось получить данные для {full_table}")
        time.sleep(0.5)
    
    # Все данные собраны — теперь атомарно заменяем БД
    print(f"\nВсего получено: {total_fetched} записей. Обновление базы данных...")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        # Очищаем и вставляем в одной транзакции
        c.execute('DELETE FROM objects')
        for type_str, rows in all_results.items():
            for row in rows:
                if len(row) >= 2:
                    n = str(row[0]) if row[0] is not None else ''
                    g = str(row[1]) if row[1] is not None else ''
                    is_folder = 0
                    if len(row) >= 3 and row[2] is not None:
                        is_folder = 1 if str(row[2]).lower() in ('true', '1', 'истина') else 0
                    if n and g:
                        c.execute('INSERT OR REPLACE INTO objects (type, name, guid, is_folder) VALUES (?, ?, ?, ?)',
                                  (type_str, n, g, is_folder))
        conn.commit()
        if error_types:
            print(f"Синхронизация завершена с предупреждениями. Не удалось получить данные для: {', '.join(error_types)}")
        else:
            print(f"Синхронизация завершена. Всего сохранено {total_fetched} записей в {DB_PATH}")
    except Exception as e:
        conn.rollback()
        print(f"\nОшибка синхронизации: {e}. Данные откатаны (существующие данные сохранены).")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
