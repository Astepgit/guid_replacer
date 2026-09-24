"""Тесты sync_db.py — мокирование HTTP-сервиса 1С."""
from unittest.mock import MagicMock, patch

import sync_db


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
