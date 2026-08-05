#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask, request, jsonify, render_template, send_file, Response, stream_with_context
from werkzeug.exceptions import RequestEntityTooLarge
from lxml import etree as ET
import json
import os
import sqlite3
import subprocess
import time
import logging
import uuid
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from parsing import parse_xml, replace_guids_in_xml, get_replacement_map

load_dotenv()

# Настройка логирования
log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(level=log_level, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_CONTENT_LENGTH', 100 * 1024 * 1024))

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Сессионное хранение: {session_id: {'xml_path': str, 'filename': str, 'created_at': float}}
_sessions = {}
SESSION_TTL = int(os.environ.get('SESSION_TTL', 3600))  # 1 час в секундах


def cleanup_sessions():
    """Удаляет сессии старше SESSION_TTL и их файлы."""
    now = time.time()
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

DB_PATH = os.environ.get('DB_PATH', 'guid_cache.db')

# ---- Работа с базой данных ----
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS objects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            name TEXT,
            guid TEXT,
            is_folder INTEGER DEFAULT 0,
            UNIQUE(type, name, is_folder)
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_type_name ON objects(type, name)')
    conn.commit()
    conn.close()

def get_guid_by_type_and_name(type_name, name, is_folder=False):
    conn = get_db_connection()
    cursor = conn.execute(
        "SELECT guid FROM objects WHERE type = ? AND name = ? AND is_folder = ? LIMIT 1",
        (type_name, name, 1 if is_folder else 0)
    )
    row = cursor.fetchone()
    conn.close()
    return row['guid'] if row else None

def sync_db_stream(xml_file):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, 'sync_db.py')
    if not os.path.exists(script_path):
        yield f"data: {json.dumps({'error': 'sync_db.py не найден'})}\n\n"
        return
    env = os.environ.copy()
    process = subprocess.Popen(
        ['python3', script_path, xml_file],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env
    )
    total_types = 0
    try:
        for line in iter(process.stdout.readline, ''):
            line = line.strip()
            if line:
                if line.startswith('ВСЕГО_ТИПОВ:'):
                    total_types = int(line.split(':', 1)[1].strip())
                    yield f"data: {json.dumps({'type': 'total', 'total_types': total_types})}\n\n"
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
        process.wait(timeout=600)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        yield f"data: {json.dumps({'type': 'error', 'message': 'Синхронизация превысила лимит 600 секунд'})}\n\n"
    if process.returncode != 0:
        yield f"data: {json.dumps({'type': 'error', 'message': f'Синхронизация завершилась с ошибкой (код {process.returncode})'})}\n\n"

# ---- Маршруты ----
@app.errorhandler(RequestEntityTooLarge)
def handle_request_entity_too_large(e):
    return jsonify({'error': 'Файл слишком большой. Максимальный размер: 100 МБ'}), 413

@app.route('/')
def index():
    init_db()
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
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
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
                _sessions[session_id] = {
                    'xml_path': filepath,
                    'filename': original_filename,
                    'created_at': time.time(),
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
        for obj in objects:
            obj_type = obj.get('_type')
            obj_name = obj.get('Description') or ''
            old_guid = obj.get('Ref')
            old_parent = obj.get('Parent')
            is_folder = obj.get('IsFolder', False)
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
            new_guid = get_guid_by_type_and_name(obj_type, obj_name, is_folder)
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
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY type, name"
        cursor = conn.execute(query, params)
        total = len(cursor.fetchall())
        query += " LIMIT ? OFFSET ?"
        params.extend([per_page, offset])
        cursor = conn.execute(query, params)
        rows = [dict(row) for row in cursor.fetchall()]
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

@app.route('/api/sync_stream')
def sync_stream():
    session_id = request.args.get('session_id')
    session_data = _sessions.get(session_id)
    if not session_data:
        return Response(
            f"data: {json.dumps({'error': 'Сессия не найдена. Загрузите файл заново.'})}\n\n",
            mimetype='text/event-stream'
        )
    xml_path = session_data['xml_path']
    if not xml_path or not os.path.exists(xml_path):
        return Response(
            f"data: {json.dumps({'error': 'XML файл не найден'})}\n\n",
            mimetype='text/event-stream'
        )
    return Response(
        stream_with_context(sync_db_stream(xml_path)),
        mimetype='text/event-stream'
    )

@app.route('/api/guid_types')
def api_guid_types():
    try:
        conn = get_db_connection()
        cursor = conn.execute("SELECT DISTINCT type FROM objects ORDER BY type")
        types = [row[0] for row in cursor.fetchall()]
        conn.close()
        return jsonify({'types': types})
    except Exception as e:
        logger.error(f"Ошибка в guid_types: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=5000)
