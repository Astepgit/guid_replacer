"""Тесты sync_db.py — мокирование HTTP-сервиса 1С."""
from unittest.mock import MagicMock, patch

import sync_db


class TestParseIsFolder:
    """Fix: расширенный парсинг ЭтоГруппа из разных форматов 1С."""

    def test_none(self):
        assert sync_db._parse_is_folder(None) is False

    def test_python_bool(self):
        assert sync_db._parse_is_folder(True) is True
        assert sync_db._parse_is_folder(False) is False

    def test_string_true_false(self):
        assert sync_db._parse_is_folder('true') is True
        assert sync_db._parse_is_folder('True') is True
        assert sync_db._parse_is_folder('TRUE') is True
        assert sync_db._parse_is_folder('false') is False
        assert sync_db._parse_is_folder('False') is False

    def test_russian_истина_ложь(self):
        assert sync_db._parse_is_folder('Истина') is True
        assert sync_db._parse_is_folder('истина') is True
        assert sync_db._parse_is_folder('Ложь') is False
        assert sync_db._parse_is_folder('ложь') is False

    def test_russian_yes_no(self):
        assert sync_db._parse_is_folder('Да') is True
        assert sync_db._parse_is_folder('да') is True
        assert sync_db._parse_is_folder('Нет') is False

    def test_integer(self):
        assert sync_db._parse_is_folder(1) is True
        assert sync_db._parse_is_folder(0) is False
        assert sync_db._parse_is_folder('1') is True

    def test_with_whitespace(self):
        assert sync_db._parse_is_folder('  true  ') is True
        assert sync_db._parse_is_folder(' Истина ') is True

    def test_unknown_value(self):
        assert sync_db._parse_is_folder('unknown') is False
        assert sync_db._parse_is_folder('') is False


class TestFetchIsFolderMap:
    """Fix: дозапрос ЭтоГруппа при fallback-варианте."""

    @patch('sync_db.execute_query')
    def test_returns_map_guid_to_bool(self, mock_execute):
        mock_execute.return_value = [
            ['guid-1', True],
            ['guid-2', False],
            ['guid-3', 'Истина'],
        ]
        result = sync_db._fetch_is_folder_map('Номенклатура', 'Справочник.Номенклатура')
        assert result == {'guid-1': True, 'guid-2': False, 'guid-3': True}

    @patch('sync_db.execute_query')
    def test_returns_none_on_error(self, mock_execute):
        mock_execute.return_value = None
        result = sync_db._fetch_is_folder_map('Номенклатура', 'Справочник.Номенклатура')
        assert result is None

    @patch('sync_db.execute_query')
    def test_skips_rows_without_guid(self, mock_execute):
        mock_execute.return_value = [
            ['guid-1', True],
            [None, False],   # без GUID
            ['guid-3', None],  # без ЭтоГруппа → False
        ]
        result = sync_db._fetch_is_folder_map('Номенклатура', 'Справочник.Номенклатура')
        assert 'guid-1' in result
        assert 'guid-3' in result
        assert result['guid-3'] is False
        assert result['guid-1'] is True


class TestExecuteQuery:
    @patch('sync_db.requests.post')
    def test_successful_query(self, mock_post):
        """Успешный запрос возвращает список строк."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'columns': ['Наименование', 'ГУИД', 'ЭтоГруппа'],
            'rows': [
                ['Товар А', 'guid-aaaa-1111', False],
                ['Товар Б', 'guid-bbbb-2222', True]
            ]
        }
        mock_post.return_value = mock_response

        result = sync_db.execute_query('SELECT 1')
        assert result == [['Товар А', 'guid-aaaa-1111', False], ['Товар Б', 'guid-bbbb-2222', True]]

    @patch('sync_db.requests.post')
    def test_http_error_returns_none(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = 'Internal Server Error'
        mock_post.return_value = mock_response

        result = sync_db.execute_query('SELECT 1')
        assert result is None

    @patch('sync_db.requests.post')
    def test_json_decode_error_returns_none(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = sync_db.json.JSONDecodeError('err', 'doc', 0)
        mock_response.text = 'not json'
        mock_post.return_value = mock_response

        result = sync_db.execute_query('SELECT 1')
        assert result is None

    @patch('sync_db.requests.post')
    def test_request_exception_returns_none(self, mock_post):
        import requests
        mock_post.side_effect = requests.exceptions.ConnectionError('refused')
        result = sync_db.execute_query('SELECT 1')
        assert result is None


class TestRunSync:
    def test_file_not_found(self, caplog):
        result = sync_db.run_sync('/nonexistent/file.xml')
        assert result['success'] is False
        assert 'не найден' in result['message']

    @patch('sync_db.execute_query')
    def test_successful_sync(self, mock_execute, isolate_env, sample_xml_path):
        """Полный цикл run_sync с моком запросов к 1С."""
        # Мокаем: для каждого типа возвращаем одну строку
        mock_execute.return_value = [
            ['Товар А', 'newguid-aaaa-1111', False],
        ]

        result = sync_db.run_sync(sample_xml_path, log_callback=lambda msg: None)
        assert result['success'] is True
        # В sample.xml 3 типа объектов → 3 типа запросов
        assert mock_execute.call_count >= 3

    @patch('sync_db.execute_query')
    def test_all_types_failed(self, mock_execute, isolate_env, sample_xml_path):
        """Все запросы упали — синхронизация завершается с ошибками, но не падает."""
        mock_execute.return_value = None

        result = sync_db.run_sync(sample_xml_path, log_callback=lambda msg: None)
        assert result['success'] is True  # атомарно заменяем даже пустую БД
        assert len(result['error_types']) > 0

    @patch('sync_db.execute_query')
    def test_query_bom_stripped(self, mock_execute, isolate_env, sample_xml_path):
        """S: BOM в начале запроса должен быть удалён."""
        mock_execute.return_value = []
        sync_db.run_sync(sample_xml_path, log_callback=lambda msg: None)
        # Проверяем, что запросы без BOM
        for call in mock_execute.call_args_list:
            args = call[0]
            query = args[0]
            assert not query.startswith('\ufeff'), 'BOM должен быть удалён'


class TestRunSyncWithFallback:
    """Fix: при fallback-варианте без ЭтоГруппа делаем дозапрос."""

    @patch('sync_db.execute_query')
    def test_fallback_triggers_is_folder_fetch(self, mock_execute, isolate_env, sample_xml_path):
        """Если первый запрос 'Наименование + ЭтоГруппа' падает, а второй 'Наименование'
        succeeds БЕЗ колонки ЭтоГруппа, sync должен сделать дополнительный запрос
        _fetch_is_folder_map и дополнить строки ЭтоГруппа.
        """
        # Mock execute_query to return None on first call (variant with ЭтоГруппа),
        # rows without ЭтоГруппа on second call (fallback), then the is_folder map.
        call_count = [0]

        def side_effect(query):
            call_count[0] += 1
            if 'ЭтоГруппа' in query and 'Наименование' in query:
                # Variant 1 'Наименование + ЭтоГруппа' fails
                return None
            elif 'Наименование' in query and 'ЭтоГруппа' not in query:
                # Variant 2 'Наименование' (fallback) succeeds, no ЭтоГруппа
                return [
                    ['Товар А', 'guid-aaaa'],
                    ['Товар Б', 'guid-bbbb'],
                ]
            elif query.strip().startswith('ВЫБРАТЬ') and 'ЭтоГруппа' in query and 'Наименование' not in query:
                # _fetch_is_folder_map query — returns (GUID, ЭтоГруппа)
                return [
                    ['guid-aaaa', True],
                    ['guid-bbbb', False],
                ]
            return None

        mock_execute.side_effect = side_effect
        result = sync_db.run_sync(sample_xml_path, log_callback=lambda msg: None)
        assert result['success'] is True
        # execute_query должен быть вызван минимум 3 раза (variant 1 + variant 2 + is_folder_fetch) для каталогов
        # plus queries for other types (Документ) — там 1 запрос на тип
        assert call_count[0] >= 4  # 2 for Номенклатура (variant 1 fail + variant 2 ok + is_folder fetch) + others

        # Проверяем, что в БД строки сохранены с правильным is_folder
        from db import get_db_connection
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT name, guid, is_folder FROM objects WHERE type LIKE '%Номенклатура%'"
        ).fetchall()
        conn.close()
        rows_dict = {r['name']: (r['guid'], r['is_folder']) for r in rows}
        assert 'Товар А' in rows_dict
        assert rows_dict['Товар А'] == ('guid-aaaa', 1)  # is_folder=1 because was True in is_folder_map
        assert 'Товар Б' in rows_dict
        assert rows_dict['Товар Б'] == ('guid-bbbb', 0)


class TestBuildQueries:
    def test_catalog_has_6_variants(self):
        queries = sync_db._build_queries('Номенклатура', 'Справочник.Номенклатура', True)
        assert len(queries) == 6
        # Все варианты имеют описательные имена
        names = [q[0] for q in queries]
        assert 'Наименование + ЭтоГруппа' in names
        assert 'Наименование' in names
        assert 'ПРЕДСТАВЛЕНИЕ + ЭтоГруппа' in names
        assert 'ПРЕДСТАВЛЕНИЕ' in names
        assert 'Код + ЭтоГруппа' in names
        assert 'Код' in names

    def test_non_catalog_has_3_variants(self):
        queries = sync_db._build_queries('РеализацияТоваров', 'Документ.РеализацияТоваров', False)
        assert len(queries) == 3
        # Нет вариантов с ЭтоГруппа
        for name, _ in queries:
            assert 'ЭтоГруппа' not in name
