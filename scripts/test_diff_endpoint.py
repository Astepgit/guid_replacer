"""Тест /api/diff endpoint для UX14: Diff-viewer."""
import io
import os
import sys

os.environ['DB_PATH'] = '/tmp/test_diff_endpoint.db'
os.environ['UPLOAD_FOLDER'] = '/tmp/test_diff_endpoint_uploads'
os.environ['FLASK_DEBUG'] = 'false'
os.environ['LOG_LEVEL'] = 'WARNING'
os.environ['MCP_QUERY_URL'] = 'http://localhost:9999/never_called'

if os.path.exists(os.environ['DB_PATH']):
    os.remove(os.environ['DB_PATH'])
os.makedirs(os.environ['UPLOAD_FOLDER'], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, init_db  # noqa: E402
from db import get_db_connection  # noqa: E402

init_db()
conn = get_db_connection()
conn.execute(
    'INSERT OR REPLACE INTO objects (type, name, guid, is_folder) VALUES (?, ?, ?, ?)',
    ('Справочник.Номенклатура', 'Товар А', 'newguid-aaaa-1111-1111-111111111111', 0)
)
conn.commit()
conn.close()

# BOM + XML
BOM = b'\xef\xbb\xbf'
XML_CONTENT = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<root xmlns:v8="http://v8.1c.ru/8.1/data/enterprise/current-config">\n'
    '  <v8:CatalogObject.Номенклатура>\n'
    '    <v8:Ref>oldguid-0000-0000-0000-000000000001</v8:Ref>\n'
    '    <v8:Description>Товар А</v8:Description>\n'
    '  </v8:CatalogObject.Номенклатура>\n'
    '</root>\n'
).encode()
XML_WITH_BOM = BOM + XML_CONTENT


def main():
    with app.test_client() as client:
        # 1. Upload
        r = client.post('/upload', data={'file': (io.BytesIO(XML_WITH_BOM), 't.xml')},
                       content_type='multipart/form-data')
        assert r.status_code == 200
        j = r.get_json()
        sid = j['session_id']
        objs = j['objects']

        # 2. Auto-replace
        r = client.post('/api/auto_replace', json={'objects': objs, 'session_id': sid})
        mod = r.get_json()['objects']

        # 3. Diff endpoint
        r = client.post('/api/diff', json={'objects': mod, 'session_id': sid})
        assert r.status_code == 200, f'Expected 200, got {r.status_code}'
        j = r.get_json()
        assert j['total_changes'] == 1, f"Expected 1 change, got {j['total_changes']}"
        assert 'newguid-aaaa' in j['modified']
        assert 'newguid-aaaa' not in j['original']
        assert 'oldguid-0000' in j['original']
        assert 'oldguid-0000' not in j['modified']
        print(f'Diff endpoint: total_changes={j["total_changes"]}, '
              f'changed_lines={j["changed_lines"]}, total_lines={j["total_lines"]}')
        print('OK: /api/diff работает корректно')


if __name__ == '__main__':
    main()
