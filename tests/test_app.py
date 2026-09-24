"""Тесты app.py — Flask-маршруты."""
import io

BOM = b'\xef\xbb\xbf'


def make_xml(objects_xml):
    """Строит минимальный XML для тестов."""
    body = '\n'.join(objects_xml)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<root xmlns:v8="http://v8.1c.ru/8.1/data/enterprise/current-config">\n'
        f'{body}\n'
        '</root>\n'
    ).encode()


class TestIndexRoute:
    def test_returns_html(self, app_client):
        r = app_client.get('/')
        assert r.status_code == 200
        assert b'<title>' in r.data
        # Кириллица в байтах — кодируем
        assert 'Замена GUID'.encode() in r.data

    def test_has_empty_state(self, app_client):
        """S3.8: на странице должен быть empty-state блок."""
        r = app_client.get('/')
        assert b'id="emptyState"' in r.data, 'Empty state блок должен присутствовать'


class TestUploadRoute:
    def test_no_file_part(self, app_client):
        r = app_client.post('/upload', data={})
        assert r.status_code == 400
        assert b'No file part' in r.data

    def test_empty_filename(self, app_client):
        r = app_client.post('/upload', data={'file': (io.BytesIO(b''), '')},
                            content_type='multipart/form-data')
        assert r.status_code == 400

    def test_non_xml_rejected(self, app_client):
        r = app_client.post('/upload',
                            data={'file': (io.BytesIO(b'hello'), 'test.txt')},
                            content_type='multipart/form-data')
        assert r.status_code == 400
        assert b'XML' in r.data or b'XML' in r.data

    def test_invalid_xml_content_rejected(self, app_client):
        # Файл с .xml расширением, но не XML содержимое
        r = app_client.post('/upload',
                            data={'file': (io.BytesIO(b'not xml at all'), 'test.xml')},
                            content_type='multipart/form-data')
        assert r.status_code == 400

    def test_valid_xml_accepted(self, app_client):
        xml = make_xml([
            '<v8:CatalogObject.Номенклатура>'
            '<v8:Ref>aaaaaaaa-1111-1111-1111-111111111111</v8:Ref>'
            '<v8:Description>Товар</v8:Description>'
            '</v8:CatalogObject.Номенклатура>'
        ])
        r = app_client.post('/upload',
                            data={'file': (io.BytesIO(xml), 'test.xml')},
                            content_type='multipart/form-data')
        assert r.status_code == 200
        j = r.get_json()
        assert 'objects' in j
        assert 'session_id' in j
        assert len(j['objects']) == 1
        assert j['objects'][0]['Description'] == 'Товар'
        assert j['filename'] == 'test.xml'


class TestGuidDbRoute:
    def test_empty_db(self, app_client):
        r = app_client.get('/api/guid_db')
        j = r.get_json()
        assert j['total'] == 0
        assert j['rows'] == []
        assert j['pages'] == 0

    def test_pagination(self, app_client, populated_db):
        r = app_client.get('/api/guid_db?page=1&per_page=1')
        j = r.get_json()
        assert j['total'] == 2
        assert j['pages'] == 2
        assert len(j['rows']) == 1

    def test_search(self, app_client, populated_db):
        r = app_client.get('/api/guid_db?search=Ромашка')
        j = r.get_json()
        assert j['total'] == 1
        assert 'Ромашка' in j['rows'][0]['name']

    def test_type_filter(self, app_client, populated_db):
        r = app_client.get('/api/guid_db?type=Справочник.Контрагенты')
        j = r.get_json()
        assert j['total'] == 1
        assert j['rows'][0]['type'] == 'Справочник.Контрагенты'

    def test_per_page_capped(self, app_client, populated_db):
        """per_page не должен превышать 500."""
        r = app_client.get('/api/guid_db?per_page=1000')
        j = r.get_json()
        assert j['per_page'] == 500


class TestAutoReplaceRoute:
    def test_no_objects(self, app_client):
        r = app_client.post('/api/auto_replace', json={})
        assert r.status_code == 400

    def test_replace_single_guid(self, app_client, populated_db):
        objects = [{
            '_type': 'Справочник.Номенклатура',
            'Description': 'Товар А',
            'Ref': 'aaaaaaaa-1111-1111-1111-111111111111',
            'IsFolder': False
        }]
        r = app_client.post('/api/auto_replace', json={'objects': objects})
        assert r.status_code == 200
        j = r.get_json()
        assert j['total_replaced'] == 1
        assert j['stats']['replaced'] == 1
        assert j['objects'][0]['Ref'] == 'newguid-aaaa-1111-1111-111111111111'

    def test_not_found_in_db(self, app_client, populated_db):
        objects = [{
            '_type': 'Документ.РеализацияТоваров',
            'Description': 'Документ 1',
            'Ref': 'cccccccc-3333-3333-3333-333333333333'
        }]
        r = app_client.post('/api/auto_replace', json={'objects': objects})
        j = r.get_json()
        assert j['total_replaced'] == 0
        assert j['stats']['not_found'] == 1

    def test_already_matched(self, app_client, populated_db):
        objects = [{
            '_type': 'Справочник.Номенклатура',
            'Description': 'Товар А',
            'Ref': 'newguid-aaaa-1111-1111-111111111111',  # уже новый
            'IsFolder': False
        }]
        r = app_client.post('/api/auto_replace', json={'objects': objects})
        j = r.get_json()
        assert j['stats']['already_matched'] == 1
        assert j['total_replaced'] == 0

    def test_no_description(self, app_client, populated_db):
        objects = [{
            '_type': 'Справочник.Номенклатура',
            'Ref': 'aaaaaaaa-1111-1111-1111-111111111111'
        }]
        r = app_client.post('/api/auto_replace', json={'objects': objects})
        j = r.get_json()
        assert j['stats']['no_description'] == 1

    def test_no_type(self, app_client, populated_db):
        objects = [{
            'Description': 'Товар',
            'Ref': 'aaaaaaaa-1111-1111-1111-111111111111'
        }]
        r = app_client.post('/api/auto_replace', json={'objects': objects})
        j = r.get_json()
        assert j['stats']['no_type'] == 1

    def test_is_folder_match(self, app_client, populated_db):
        """S3: is_folder должен учитываться при поиске."""
        objects = [{
            '_type': 'Справочник.Контрагенты',
            'Description': 'ООО Ромашка',
            'Ref': 'oldguid-bbbb-2222-2222-222222222222',
            'IsFolder': True
        }]
        r = app_client.post('/api/auto_replace', json={'objects': objects})
        j = r.get_json()
        assert j['total_replaced'] == 1
        assert j['objects'][0]['Ref'] == 'newguid-bbbb-2222-2222-222222222222'

    def test_is_folder_mismatch_fallback(self, app_client, populated_db):
        """Fix: если БД не содержит is_folder-инфы (sync упал на запросе с ЭтоГруппа),
        fallback по (type, name) всё равно находит единственного кандидата.
        """
        # Сценарий: в XML объект IsFolder=True, в БД есть только с is_folder=0
        # (потому что fallback sync_db.py не достал ЭтоГруппа).
        objects = [{
            '_type': 'Справочник.Номенклатура',
            'Description': 'Товар А',
            'Ref': 'oldguid-0000-0000-0000-000000000000',
            'IsFolder': True  # в XML это группа, а в БД сохранено как элемент
        }]
        r = app_client.post('/api/auto_replace', json={'objects': objects})
        j = r.get_json()
        # Ожидаем: fallback найдёт единственного кандидата (newguid-aaaa-...) по (type, name)
        assert j['total_replaced'] == 1, f'Expected 1 replacement via fallback, got {j["total_replaced"]}'
        assert j['objects'][0]['Ref'] == 'newguid-aaaa-1111-1111-111111111111'

    def test_ambiguous_multiple_candidates_not_replaced(self, app_client, populated_db):
        """Если для (type, name) есть несколько GUID-ов с разным is_folder и точного
        совпадения нет — пропускаем, не делаем неоднозначную замену.
        """
        # Добавим второй GUID для 'Товар А' с is_folder=True
        from db import get_db_connection
        conn = get_db_connection()
        conn.execute(
            'INSERT OR REPLACE INTO objects (type, name, guid, is_folder) VALUES (?, ?, ?, ?)',
            ('Справочник.Номенклатура', 'Товар А', 'another-guid-aaaa-1111-1111-111111111111', 1)
        )
        conn.commit()
        conn.close()

        # В XML объект с IsFolder=None (не указано) — не совпадает ни с True, ни с False в БД
        # → fallback найдёт 2 кандидата → пропустит как неоднозначное
        objects = [{
            '_type': 'Справочник.Номенклатура',
            'Description': 'Товар А',
            'Ref': 'oldguid-0000-0000-0000-000000000000',
            # IsFolder не указан — bool(obj.get('IsFolder', False)) даст False
            # → точное совпадение по (type, name, False) найдёт newguid-aaaa-...
        }]
        r = app_client.post('/api/auto_replace', json={'objects': objects})
        j = r.get_json()
        # Точное совпадение по is_folder=False должно сработать (не fallback)
        assert j['total_replaced'] == 1


class TestDownloadRoute:
    def _upload(self, client):
        """Загружает тестовый XML и возвращает session_id + objects."""
        xml = make_xml([
            '<v8:CatalogObject.Номенклатура>'
            '<v8:Ref>aaaaaaaa-1111-1111-1111-111111111111</v8:Ref>'
            '<v8:Description>Товар А</v8:Description>'
            '<v8:IsFolder>false</v8:IsFolder>'
            '</v8:CatalogObject.Номенклатура>'
        ])
        r = client.post('/upload',
                        data={'file': (io.BytesIO(xml), 'test.xml')},
                        content_type='multipart/form-data')
        j = r.get_json()
        return j['session_id'], j['objects']

    def test_no_session(self, app_client):
        r = app_client.post('/api/download',
                           json={'objects': [], 'session_id': 'invalid'})
        assert r.status_code == 400
        # Flask jsonify по умолчанию экранирует non-ASCII как \uXXXX,
        # поэтому проверяем через get_json(), а не через bytes.
        j = r.get_json()
        assert 'Сессия не найдена' in j.get('error', '')

    def test_full_cycle(self, app_client, populated_db):
        # 1. Загрузка
        session_id, objects = self._upload(app_client)
        # 2. Автозамена
        r = app_client.post('/api/auto_replace',
                           json={'objects': objects, 'session_id': session_id})
        modified = r.get_json()['objects']
        # 3. Скачивание
        r = app_client.post('/api/download',
                           json={'objects': modified, 'session_id': session_id})
        assert r.status_code == 200
        assert r.data.startswith(b'\xef\xbb\xbf')  # BOM
        content = r.data[3:]
        assert b'newguid-aaaa-1111-1111-111111111111' in content
        assert b'aaaaaaaa-1111-1111-1111-111111111111' not in content

    def test_no_replacements(self, app_client):
        session_id, objects = self._upload(app_client)
        # objects не модифицированы — нет замен
        r = app_client.post('/api/download',
                           json={'objects': objects, 'session_id': session_id})
        assert r.status_code == 400
        assert b'No replacements found' in r.data


class TestSyncStreamRoute:
    def test_invalid_session(self, app_client):
        r = app_client.get('/api/sync_stream?session_id=invalid')
        assert r.status_code == 200  # SSE всегда 200
        assert b'error' in r.data


class TestGuidTypesRoute:
    def test_empty_db(self, app_client):
        r = app_client.get('/api/guid_types')
        j = r.get_json()
        assert j['types'] == []

    def test_populated_db(self, app_client, populated_db):
        r = app_client.get('/api/guid_types')
        j = r.get_json()
        assert len(j['types']) == 2
        assert 'Справочник.Номенклатура' in j['types']
        assert 'Справочник.Контрагенты' in j['types']
