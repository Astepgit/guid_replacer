#!/usr/bin/env python3

import json
import logging
import logging.handlers
import os
import threading
import time
import uuid

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context
from lxml import etree as ET
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from db import get_db_connection, init_db, load_guid_index
from parsing import get_replacement_map, parse_xml, replace_guids_in_xml
from sync_db import run_sync

load_dotenv()

# Настройка логирования
log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
log_file = os.environ.get('LOG_FILE', 'app.log')

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_CONTENT_LENGTH', 100 * 1024 * 1024))


def get_upload_folder():
    """Возвращает путь к папке загрузок, создавая её при необходимости.
    Читает env при каждом вызове, чтобы работало в тестах с monkeypatch.
    """
    upload_folder = os.environ.get('UPLOAD_FOLDER', 'uploads')
    os.makedirs(upload_folder, exist_ok=True)
    return upload_folder

# Настраиваем логирование после создания Flask-приложения
_file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
_stream_handler = logging.StreamHandler()
_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
_file_handler.setFormatter(_formatter)
_stream_handler.setFormatter(_formatter)

# Настраиваем root-логгер — Flask/werkzeug используют его
_root_logger = logging.getLogger()
_root_logger.setLevel(log_level)
_root_logger.addHandler(_file_handler)
_root_logger.addHandler(_stream_handler)

# Также настраиваем app.logger напрямую
app.logger.setLevel(log_level)
app.logger.addHandler(_file_handler)
app.logger.addHandler(_stream_handler)

logger = logging.getLogger(__name__)

# S2.1: инициализация БД на уровне модуля, а не при каждом запросе /
with app.app_context():
    init_db()

# Сессионное хранение: {session_id: {'xml_path': str, 'filename': str, 'created_at': float, 'replacements': dict}}
_sessions = {}
_sessions_lock = threading.Lock()
SESSION_TTL = int(os.environ.get('SESSION_TTL', 3600))  # 1 час в секундах

# Список временных файлов для очистки после отдачи ответа
_pending_temp_files = []
_pending_temp_files_lock = threading.Lock()


def _track_temp_file(path):
    """Регистрирует временный файл для удаления после отдачи ответа."""
    if path:
        with _pending_temp_files_lock:
            _pending_temp_files.append(path)


def _cleanup_tracked_temp_files():
    """Удаляет все временные файлы, накопленные в текущем запросе."""
    with _pending_temp_files_lock:
        files_to_remove = list(_pending_temp_files)
        _pending_temp_files.clear()
    for path in files_to_remove:
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.debug(f"Удалён временный файл: {path}")
        except OSError as e:
            logger.warning(f"Не удалось удалить временный файл {path}: {e}")


@app.after_request
def _cleanup_temp_files_after_request(response):
    """Очищает временные файлы (modified XML и т.д.) после отдачи ответа."""
    _cleanup_tracked_temp_files()
    return response


def cleanup_sessions():
    """Удаляет сессии старше SESSION_TTL и их файлы. Потокобезопасно."""
    now = time.time()
    with _sessions_lock:
        expired = [sid for sid, sdata in _sessions.items()
                   if now - sdata.get('created_at', 0) > SESSION_TTL]
        for sid in expired:
            sdata = _sessions.pop(sid, None)
            if sdata:
                xml_path = sdata.get('xml_path')
                if xml_path and os.path.exists(xml_path):
                    try:
                        os.remove(xml_path)
                        logger.info(f"Удалён файл истекшей сессии {sid}: {xml_path}")
                    except OSError as e:
                        logger.warning(f"Не удалось удалить файл {xml_path}: {e}")
    if expired:
        logger.info(f"Очищено {len(expired)} истекших сессий")


# Пространства имён для поиска (используются только для парсинга объектов)
NAMESPACES = {
    'v8': 'http://v8.1c.ru/8.1/data/enterprise/current-config',
    'core': 'http://v8.1c.ru/8.1/data/core',
    'xs': 'http://www.w3.org/2001/XMLSchema',
    'xsi': 'http://www.w3.org/2001/XMLSchema-instance',
    'V8Exch': 'http://www.1c.ru/V8/1CV8DtUD/'
}


# ---- Маршруты ----
@app.errorhandler(RequestEntityTooLarge)
def handle_request_entity_too_large(e):
    return jsonify({'error': 'Файл слишком большой. Максимальный размер: 100 МБ'}), 413


@app.route('/')
def index():
    # S2.1: init_db уже выполнен на уровне модуля; здесь просто отдаём страницу
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_file():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file part'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No selected file'}), 400
        if file:
            original_filename = file.filename or ''
            # Валидация расширения по оригинальному имени (до secure_filename,
            # который может вырезать не-ASCII символы и вернуть пустую строку)
            if not original_filename.lower().endswith('.xml'):
                return jsonify({'error': 'Разрешены только XML-файлы'}), 400
            filename = secure_filename(original_filename)
            # Если secure_filename вернул пустую строку, генерируем безопасное имя
            if not filename:
                filename = f"upload_{uuid.uuid4().hex}.xml"
            filepath = os.path.join(get_upload_folder(), filename)
            file.save(filepath)
            # Валидация содержимого — проверяем первый байт
            try:
                with open(filepath, 'rb') as f:
                    first_bytes = f.read(512)
                    if b'<?xml' not in first_bytes and b'<v8:' not in first_bytes:
                        os.remove(filepath)
                        return jsonify({'error': 'Файл не является валидным XML'}), 400
            except Exception:
                pass
            try:
                objects, tree = parse_xml(filepath)
                session_id = str(uuid.uuid4())
                with _sessions_lock:
                    _sessions[session_id] = {
                        'xml_path': filepath,
                        'filename': original_filename,
                        'created_at': time.time(),
                        'replacements': {},
                    }
                logger.info(f"Загрузка файла: session={session_id}, файл={original_filename}, объектов={len(objects)}")
                cleanup_sessions()
                return jsonify({'objects': objects, 'filename': original_filename, 'session_id': session_id})
            except ET.XMLSyntaxError as e:
                logger.error(f"Ошибка парсинга XML: {e}")
                return jsonify({'error': f'Ошибка парсинга XML: {str(e)}'}), 400
            except Exception as e:
                logger.error(f"Ошибка при обработке файла: {e}")
                return jsonify({'error': f'Ошибка при обработке файла: {str(e)}'}), 500
    except RequestEntityTooLarge:
        return jsonify({'error': 'Файл слишком большой. Максимальный размер: 100 МБ'}), 413
    except Exception as e:
        logger.error(f"Необработанная ошибка: {e}")
        return jsonify({'error': f'Внутренняя ошибка сервера: {str(e)}'}), 500


@app.route('/api/auto_replace', methods=['POST'])
def auto_replace_guids():
    try:
        data = request.get_json()
        if not data or 'objects' not in data:
            return jsonify({'error': 'No objects provided'}), 400
        objects = data['objects']
        replacements = {}
        modified_objects = []
        stats = {'replaced': 0, 'not_found': 0, 'already_matched': 0, 'no_description': 0, 'no_type': 0, 'no_guid': 0}

        # S3.1: загружаем весь индекс GUID одним SELECT, O(1) lookup per object
        # вместо N+1 запросов к SQLite.
        guid_index = load_guid_index()

        for obj in objects:
            obj_type = obj.get('_type')
            obj_name = obj.get('Description') or ''
            old_guid = obj.get('Ref')
            old_parent = obj.get('Parent')
            is_folder = bool(obj.get('IsFolder', False))
            if not obj_type:
                stats['no_type'] += 1
                modified_objects.append(obj)
                continue
            if not obj_name:
                stats['no_description'] += 1
                modified_objects.append(obj)
                continue
            if not old_guid:
                stats['no_guid'] += 1
                modified_objects.append(obj)
                continue
            new_guid = guid_index.get((obj_type, obj_name, is_folder))
            if not new_guid:
                stats['not_found'] += 1
                modified_objects.append(obj)
                continue
            if new_guid == old_guid:
                stats['already_matched'] += 1
                modified_objects.append(obj)
                continue
            obj['Ref'] = new_guid
            replacements[old_guid] = new_guid
            stats['replaced'] += 1
            if old_parent == old_guid:
                obj['Parent'] = None
                logger.info(f"Для объекта {obj_name} ({obj_type}) Parent == Ref, установлен Parent = NULL")
            # Обновляем Owner, если он ссылается на старый GUID
            old_owner = obj.get('Owner')
            if old_owner and old_owner == old_guid:
                obj['Owner'] = None
                logger.info(f"Для объекта {obj_name} ({obj_type}) Owner == Ref, установлен Owner = NULL")
            modified_objects.append(obj)
        logger.info(f"Автозамена: session={data.get('session_id', '?')}, заменено {stats['replaced']}, не найдено {stats['not_found']}, "
                    f"совпадало {stats['already_matched']}, без описания {stats['no_description']}")
        # S2.7: сохраняем replacements в сессии для восстановления после перезагрузки страницы
        session_id = data.get('session_id')
        if session_id:
            with _sessions_lock:
                sdata = _sessions.get(session_id)
                if sdata is not None:
                    sdata['replacements'] = replacements
        return jsonify({
            'objects': modified_objects,
            'replacements': replacements,
            'total_replaced': len(replacements),
            'stats': stats
        })
    except Exception as e:
        logger.error(f"Ошибка в auto_replace: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/download', methods=['POST'])
def download_modified_xml():
    try:
        data = request.get_json()
        if not data or 'objects' not in data:
            return jsonify({'error': 'No objects provided'}), 400
        session_id = data.get('session_id')
        with _sessions_lock:
            session_data = _sessions.get(session_id)
            if not session_data:
                return jsonify({'error': 'Сессия не найдена. Загрузите файл заново.'}), 400
            original_path = session_data['xml_path']
        if not original_path or not os.path.exists(original_path):
            return jsonify({'error': 'Original XML file not found'}), 400
        try:
            orig_objects, _ = parse_xml(original_path)
            replacements = get_replacement_map(orig_objects, data['objects'])
            if not replacements:
                return jsonify({'error': 'No replacements found'}), 400
            modified_path = replace_guids_in_xml(original_path, replacements)
            # S1.5: регистрируем временный файл для очистки после отдачи ответа
            _track_temp_file(modified_path)
            original_filename = session_data.get('filename', 'modified')
            name, ext = os.path.splitext(original_filename)
            download_name = f"{name}_modified{ext}"
            logger.info(f"Скачивание: session={session_id}, файл={download_name}, замен={len(replacements)}")
            return send_file(modified_path, as_attachment=True, download_name=download_name)
        except Exception as e:
            logger.error(f"Ошибка при скачивании: {e}")
            return jsonify({'error': str(e)}), 500
    except Exception as e:
        logger.error(f"Необработанная ошибка в download: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/guid_db')
def api_guid_db():
    try:
        search = request.args.get('search', '')
        type_filter = request.args.get('type', '')
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 100, type=int)
        per_page = min(per_page, 500)
        offset = (page - 1) * per_page
        conn = get_db_connection()
        query = "SELECT type, name, guid, is_folder FROM objects"
        params = []
        conditions = []
        if search:
            conditions.append("(name LIKE ? OR guid LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if type_filter:
            conditions.append("type = ?")
            params.append(type_filter)
        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        query_with_where = query + where_clause
        # S3.4: COUNT(*) вместо fetchall() — не загружает все строки в память
        count_query = f"SELECT COUNT(*) FROM ({query_with_where})"
        total = conn.execute(count_query, params).fetchone()[0]
        data_query = query_with_where + " ORDER BY type, name LIMIT ? OFFSET ?"
        params_with_pagination = params + [per_page, offset]
        rows = [dict(row) for row in conn.execute(data_query, params_with_pagination).fetchall()]
        conn.close()
        return jsonify({
            'rows': rows,
            'page': page,
            'per_page': per_page,
            'total': total,
            'pages': (total + per_page - 1) // per_page if per_page else 0
        })
    except Exception as e:
        logger.error(f"Ошибка в guid_db: {e}")
        return jsonify({'error': str(e)}), 500


def sync_db_stream(xml_path):
    """S2.4+S2.5: запускает run_sync в том же процессе (без subprocess),
    передаёт прогресс в SSE-поток. В начале отправляет retry-инструкцию,
    периодически — heartbeat для удержания соединения.
    """
    # S2.5: SSE-инструкция для авто-переподключения при обрыве
    yield "retry: 5000\n\n"

    # S2.5: heartbeat-поток в фоне
    heartbeat_stop = threading.Event()

    # Простейший способ — использовать очередь и writer-поток
    import queue
    sync_queue = queue.Queue()
    SENTINEL = object()

    def log_to_queue(msg):
        sync_queue.put(msg)

    def worker():
        try:
            run_sync(xml_path, log_callback=log_to_queue)
        except Exception as e:
            logger.exception("Ошибка в run_sync (in-process)")
            sync_queue.put(f"__ERROR__:{e}")
        finally:
            sync_queue.put(SENTINEL)

    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()

    total_types = 0
    last_heartbeat = time.time()

    try:
        while True:
            # Неблокирующий опрос очереди с heartbeat
            try:
                item = sync_queue.get(timeout=1)
            except queue.Empty:
                # S2.5: отправляем heartbeat каждые 15 секунд
                if time.time() - last_heartbeat > 15:
                    yield ": keepalive\n\n"
                    last_heartbeat = time.time()
                continue

            if item is SENTINEL:
                break

            if isinstance(item, str) and item.startswith("__ERROR__:"):
                err_msg = item[len("__ERROR__:"):]
                yield f"data: {json.dumps({'type': 'error', 'message': f'Синхронизация завершилась с ошибкой: {err_msg}'})}\n\n"
                break

            line = item.strip()
            if not line:
                continue

            if line.startswith('ВСЕГО_ТИПОВ:'):
                try:
                    total_types = int(line.split(':', 1)[1].strip())
                    yield f"data: {json.dumps({'type': 'total', 'total_types': total_types})}\n\n"
                except ValueError:
                    pass
            elif 'Синхронизация завершена' in line:
                yield f"data: {json.dumps({'type': 'done', 'message': line})}\n\n"
            elif 'Обрабатывается:' in line:
                parts = line.split('Обрабатывается:')
                if len(parts) > 1:
                    table = parts[1].strip()
                    yield f"data: {json.dumps({'type': 'progress', 'table': table, 'total_types': total_types, 'message': line})}\n\n"
            elif 'Получено' in line or 'Не удалось получить данные' in line:
                yield f"data: {json.dumps({'type': 'result', 'message': line})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"
    finally:
        heartbeat_stop.set()
        if worker_thread.is_alive():
            worker_thread.join(timeout=5)


@app.route('/api/sync_stream')
def sync_stream():
    session_id = request.args.get('session_id')
    with _sessions_lock:
        session_data = _sessions.get(session_id)
        if not session_data:
            return Response(
                f"data: {json.dumps({'type': 'error', 'message': 'Сессия не найдена. Загрузите файл заново.'})}\n\n",
                mimetype='text/event-stream'
            )
        xml_path = session_data['xml_path']
    if not xml_path or not os.path.exists(xml_path):
        return Response(
            f"data: {json.dumps({'type': 'error', 'message': 'XML файл не найден'})}\n\n",
            mimetype='text/event-stream'
        )
    return Response(
        stream_with_context(sync_db_stream(xml_path)),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',  # отключает буферизацию в nginx
        }
    )


@app.route('/api/guid_types')
def api_guid_types():
    try:
        conn = get_db_connection()
        types = [row[0] for row in conn.execute("SELECT DISTINCT type FROM objects ORDER BY type").fetchall()]
        conn.close()
        return jsonify({'types': types})
    except Exception as e:
        logger.error(f"Ошибка в guid_types: {e}")
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    host = os.environ.get('FLASK_HOST', '127.0.0.1')
    port = int(os.environ.get('FLASK_PORT', 5000))
    app.run(debug=debug, host=host, port=port)
