"""Pytest fixtures: тестовое окружение, мок 1С, синтетические XML."""
import os
import shutil
import sys

import pytest

# Делаем проект импортируемым
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture(autouse=True)
def isolate_env(tmp_path, monkeypatch):
    """Изолирует каждый тест в свой tmp_path, чтобы не конфликтовать с реальной БД."""
    # Уникальная БД для теста
    db_path = str(tmp_path / 'test_guid_cache.db')
    upload_dir = str(tmp_path / 'uploads')
    os.makedirs(upload_dir, exist_ok=True)

    monkeypatch.setenv('DB_PATH', db_path)
    monkeypatch.setenv('UPLOAD_FOLDER', upload_dir)
    monkeypatch.setenv('FLASK_DEBUG', 'false')
    monkeypatch.setenv('LOG_LEVEL', 'WARNING')
    monkeypatch.setenv('MCP_QUERY_URL', 'http://mock-1c.local/hs/mcp-1c/query')
    monkeypatch.setenv('MCP_USER', 'test_user')
    monkeypatch.setenv('MCP_PASSWORD', 'test_pass')

    # Перезагружаем db.DB_PATH, потому что он вычисляется при импорте
    import db
    monkeypatch.setattr(db, 'DB_PATH', db_path)

    yield tmp_path

    # cleanup
    if os.path.exists(db_path):
        os.remove(db_path)
    shutil.rmtree(upload_dir, ignore_errors=True)


@pytest.fixture
def app_client(isolate_env):
    """Flask test client с инициализированной БД."""
    # Импортируем после установки env
    from app import app, init_db
    init_db()
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def sample_xml_path():
    """Путь к фикстуре sample.xml."""
    return os.path.join(PROJECT_ROOT, 'tests', 'fixtures', 'sample.xml')


@pytest.fixture
def populated_db(isolate_env):
    """БД с тестовыми GUID, как будто после синхронизации с 1С."""
    from app import init_db
    from db import get_db_connection
    init_db()
    conn = get_db_connection()
    conn.execute(
        'INSERT OR REPLACE INTO objects (type, name, guid, is_folder) VALUES (?, ?, ?, ?)',
        ('Справочник.Номенклатура', 'Товар А', 'newguid-aaaa-1111-1111-111111111111', 0)
    )
    conn.execute(
        'INSERT OR REPLACE INTO objects (type, name, guid, is_folder) VALUES (?, ?, ?, ?)',
        ('Справочник.Контрагенты', 'ООО Ромашка', 'newguid-bbbb-2222-2222-222222222222', 1)
    )
    # Документ.РеализацияТоваров намеренно не добавлен — должен попасть в not_found
    conn.commit()
    conn.close()
