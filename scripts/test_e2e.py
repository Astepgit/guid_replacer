"""End-to-end тест: загрузка XML → автозамена → скачивание.

Проверяет полный цикл работы приложения с синтетическими данными.
"""
import io
import os
import sys

# Подготовка тестового окружения
os.environ['DB_PATH'] = '/tmp/test_guid_e2e.db'
os.environ['UPLOAD_FOLDER'] = '/tmp/test_uploads_e2e'
os.environ['FLASK_DEBUG'] = 'false'
os.environ['LOG_LEVEL'] = 'WARNING'
os.environ['MCP_QUERY_URL'] = 'http://localhost:9999/never_called'

# Удаляем предыдущую тестовую БД
if os.path.exists(os.environ['DB_PATH']):
    os.remove(os.environ['DB_PATH'])
os.makedirs(os.environ['UPLOAD_FOLDER'], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, init_db  # noqa: E402
from db import get_db_connection  # noqa: E402

init_db()

# Подготовка тестовых данных в БД (как будто уже синхронизировано с 1С)
conn = get_db_connection()
conn.execute(
    'INSERT OR REPLACE INTO objects (type, name, guid, is_folder) VALUES (?, ?, ?, ?)',
    ('Справочник.Номенклатура', 'Товар А', 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', 0)
)
conn.execute(
    'INSERT OR REPLACE INTO objects (type, name, guid, is_folder) VALUES (?, ?, ?, ?)',
    ('Справочник.Номенклатура', 'Товар с <script>alert(1)</script> XSS', 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff', 1)
)
conn.commit()
conn.close()

# Тестовый XML (UTF-8 с BOM)
BOM = b'\xef\xbb\xbf'
XML_CONTENT = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<root xmlns:v8="http://v8.1c.ru/8.1/data/enterprise/current-config" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
    '  <v8:CatalogObject.Номенклатура>\n'
    '    <v8:Ref>12345678-1234-1234-1234-123456789012</v8:Ref>\n'
    '    <v8:Description>Товар А</v8:Description>\n'
    '    <v8:Code>00001</v8:Code>\n'
    '    <v8:IsFolder>false</v8:IsFolder>\n'
    '  </v8:CatalogObject.Номенклатура>\n'
    '</root>\n'
).encode()
XML_WITH_BOM = BOM + XML_CONTENT


def main():
    with app.test_client() as client:
        # 1. GET / — страница загружается
        r = client.get('/')
        assert r.status_code == 200, f'GET / failed: {r.status_code}'
        assert b'<title>' in r.data, 'HTML должен содержать title'
        print(f'1. GET /: OK (статус {r.status_code}, {len(r.data)} байт)')

        # 2. POST /upload — загрузка XML
        r = client.post(
            '/upload',
            data={'file': (io.BytesIO(XML_WITH_BOM), 'test.xml')},
            content_type='multipart/form-data'
        )
        assert r.status_code == 200, f'Upload failed: {r.status_code} {r.data}'
        j = r.get_json()
        assert 'objects' in j, f'No objects in response: {j}'
        assert len(j['objects']) == 1, f'Expected 1 object, got {len(j["objects"])}'
        session_id = j['session_id']
        objects = j['objects']
        print(f'2. POST /upload: OK (объектов={len(objects)}, session={session_id[:8]}...)')

        # 3. GET /api/guid_db — пагинация работает
        r = client.get('/api/guid_db?page=1&per_page=10')
        j = r.get_json()
        assert j['total'] == 2, f'Expected 2 records, got {j["total"]}'
        assert j['pages'] == 1
        assert len(j['rows']) == 2
        print(f'3. GET /api/guid_db: OK (total={j["total"]}, pages={j["pages"]})')

        # 4. POST /api/auto_replace — замена
        r = client.post('/api/auto_replace', json={
            'objects': objects, 'session_id': session_id
        })
        assert r.status_code == 200
        j = r.get_json()
        assert j['total_replaced'] == 1, f'Expected 1 replacement, got {j["total_replaced"]}'
        assert j['stats']['replaced'] == 1
        assert j['stats']['already_matched'] == 0
        assert j['objects'][0]['Ref'] == 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        print(f'4. POST /api/auto_replace: OK (replaced={j["total_replaced"]}, stats={j["stats"]})')

        # 5. POST /api/download — генерация и скачивание
        r = client.post('/api/download', json={
            'objects': j['objects'], 'session_id': session_id
        })
        assert r.status_code == 200, f'Download failed: {r.status_code} {r.data[:200]}'
        assert r.data.startswith(b'\xef\xbb\xbf'), 'File should have BOM'
        content = r.data[3:]  # skip BOM
        assert b'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee' in content, 'New GUID must be in file'
        assert b'12345678-1234-1234-1234-123456789012' not in content, 'Old GUID must be replaced'
        print('5. POST /api/download: OK (BOM, new GUID, old GUID replaced)')

        # 6. GET /api/sync_stream без активной сессии — SSE-ошибка
        r = client.get('/api/sync_stream?session_id=invalid-session-id')
        assert r.status_code == 200
        assert b'error' in r.data
        print('6. GET /api/sync_stream (invalid session): OK (вернул SSE-ошибку)')

    print('\nВсе end-to-end тесты пройдены!')


if __name__ == '__main__':
    main()
