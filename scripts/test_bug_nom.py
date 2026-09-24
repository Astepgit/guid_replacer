"""Воспроизведение бага: Номенклатура с группами и элементами.

Сценарий:
- 1С вернула для Номенклатура только элементы (без ЭтоГруппа),
  потому что первый вариант запроса "Наименование + ЭтоГруппа" упал.
- В XML есть и группы (IsFolder=true), и элементы (IsFolder=false).
- В fallback-варианте sync_db.py не получает ЭтоГруппа, ставит is_folder=0
  для всех строк. Группы в XML не находятся в БД → not_found.
"""
import io
import os
import sys

os.environ['DB_PATH'] = '/tmp/test_bug_nom.db'
os.environ['UPLOAD_FOLDER'] = '/tmp/test_bug_uploads'
os.environ['FLASK_DEBUG'] = 'false'
os.environ['LOG_LEVEL'] = 'WARNING'
os.environ['MCP_QUERY_URL'] = 'http://localhost:9999/never_called'

if os.path.exists(os.environ['DB_PATH']):
    os.remove(os.environ['DB_PATH'])
os.makedirs(os.environ['UPLOAD_FOLDER'], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, init_db
from db import get_db_connection

init_db()

# Сценарий 1: 1С вернула Номенклатура без ЭтоГруппа
# (fallback-вариант "Наименование" без ЭтоГруппа).
# Все строки в БД имеют is_folder=0.
conn = get_db_connection()
test_guids = [
    ('Справочник.Номенклатура', 'Группа А', '11111111-aaaa-aaaa-aaaa-111111111111', 0),  # реально группа
    ('Справочник.Номенклатура', 'Товар 1', '22222222-aaaa-aaaa-aaaa-222222222222', 0),
    ('Справочник.Номенклатура', 'Товар 2', '33333333-aaaa-aaaa-aaaa-333333333333', 0),
]
for t, n, g, f in test_guids:
    conn.execute(
        'INSERT OR REPLACE INTO objects (type, name, guid, is_folder) VALUES (?, ?, ?, ?)',
        (t, n, g, f)
    )
conn.commit()
conn.close()

# XML с группой (IsFolder=true) и элементами (IsFolder=false)
XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<root xmlns:v8="http://v8.1c.ru/8.1/data/enterprise/current-config">\n'
    '  <v8:CatalogObject.Номенклатура>\n'
    '    <v8:Ref>old00000-0000-0000-0000-000000000001</v8:Ref>\n'
    '    <v8:Description>Группа А</v8:Description>\n'
    '    <v8:IsFolder>true</v8:IsFolder>\n'
    '  </v8:CatalogObject.Номенклатура>\n'
    '  <v8:CatalogObject.Номенклатура>\n'
    '    <v8:Ref>old00000-0000-0000-0000-000000000002</v8:Ref>\n'
    '    <v8:Description>Товар 1</v8:Description>\n'
    '    <v8:IsFolder>false</v8:IsFolder>\n'
    '  </v8:CatalogObject.Номенклатура>\n'
    '  <v8:CatalogObject.Номенклатура>\n'
    '    <v8:Ref>old00000-0000-0000-0000-000000000003</v8:Ref>\n'
    '    <v8:Description>Товар 2</v8:Description>\n'
    '    <v8:IsFolder>false</v8:IsFolder>\n'
    '  </v8:CatalogObject.Номенклатура>\n'
    '</root>\n'
).encode()


def main():
    with app.test_client() as client:
        # 1. Загрузка XML
        r = client.post('/upload',
                       data={'file': (io.BytesIO(XML), 'test.xml')},
                       content_type='multipart/form-data')
        j = r.get_json()
        session_id = j['session_id']
        objects = j['objects']
        print(f'1. Загружено объектов: {len(objects)}')
        for o in objects:
            print(f'   - {o.get("Description")!r} IsFolder={o.get("IsFolder")} Ref={o.get("Ref")[:8]}...')

        # 2. Автозамена
        r = client.post('/api/auto_replace',
                       json={'objects': objects, 'session_id': session_id})
        j = r.get_json()
        print(f'\n2. Автозамена: заменено={j["total_replaced"]}')
        print(f'   stats: {j["stats"]}')
        print('\n   Детально по объектам:')
        for orig, mod in zip(objects, j['objects']):
            old = orig.get('Ref')
            new = mod.get('Ref')
            folder = mod.get('IsFolder')
            status = '✓ ЗАМЕНЕНО' if new != old else ('already_matched' if new == old else 'not_found')
            print(f'   - {mod.get("Description")!r} IsFolder={folder}: {old[:8]}... → {new[:8] if new else "None"}... [{status}]')

        # Ожидаемый результат: 3 замены (Группа А + Товар 1 + Товар 2)
        # Фактический баг: Группа А не найдена (is_folder=True vs DB is_folder=0)
        assert j['total_replaced'] == 3, f'BUG: ожидается 3 замены, фактически {j["total_replaced"]}. Группа А не найдена из-за is_folder mismatch!'
        print('\n✓ Все GUID-ы заменены, баг не воспроизвёлся')


if __name__ == '__main__':
    main()
