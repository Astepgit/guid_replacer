#!/usr/bin/env python3

import json

import requests
from requests.auth import HTTPBasicAuth

BASE_URL = "http://erp-700/Vysotskaya_TA/hs/mcp-1c"
PATHS = [
    "",                         # корневой путь
    "/query",                   # мы пробовали
    "/ЗапросPOST",              # русское имя метода из кода
    "/Запрос",                  # сокращённое
    "/execute_query",           # английское имя
]
USER = "mcp"
PASSWORD = "mcp"
QUERY = "SELECT 1 AS Кол"
AUTH = HTTPBasicAuth(USER, PASSWORD)

def test_path(path, method="POST", payload=None):
    url = BASE_URL + path
    if payload is None:
        payload = {"query": QUERY}

    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream'
    }

    try:
        response = requests.post(url, json=payload, headers=headers, auth=AUTH, timeout=30)
        print(f"Путь {path}: статус {response.status_code}")
        if response.status_code == 200:
            try:
                data = response.json()
                print(f"  Ответ: {json.dumps(data, indent=2, ensure_ascii=False)[:300]}")
            except Exception:
                print(f"  Ответ: {response.text[:200]}")
            return True
        else:
            print(f"  Ошибка: {response.text[:100]}")
    except Exception as e:
        print(f"  Исключение: {e}")
    return False

def test_jsonrpc():
    print("\n=== JSON-RPC на /mcp ===")
    url = BASE_URL + "/mcp"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "execute_query",
            "arguments": {
                "query": QUERY
            }
        }
    }
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
    try:
        response = requests.post(url, json=payload, headers=headers, auth=AUTH, timeout=30)
        print(f"/mcp: статус {response.status_code}")
        print(f"  Ответ: {response.text[:200]}")
        return response.status_code == 200
    except Exception as e:
        print(f"  Ошибка: {e}")
        return False

if __name__ == "__main__":
    print(f"Проверка HTTP-сервиса 1С: {BASE_URL}")
    print(f"Пользователь: {USER}")

    print("\n=== Тестирование путей ===")
    success = False
    for path in PATHS:
        if test_path(path):
            success = True
            break

    if not success:
        # Пробуем JSON-RPC
        test_jsonrpc()

    print("\nГотово.")
