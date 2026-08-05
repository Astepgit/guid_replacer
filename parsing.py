#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Общий модуль парсинга XML-выгрузок 1С.
Используется app.py и parser.py для извлечения объектов метаданных.
"""

from lxml import etree as ET
import logging
import os
import re
import tempfile

logger = logging.getLogger(__name__)

NAMESPACES = {
    'v8': 'http://v8.1c.ru/8.1/data/enterprise/current-config',
    'core': 'http://v8.1c.ru/8.1/data/core',
    'xs': 'http://www.w3.org/2001/XMLSchema',
    'xsi': 'http://www.w3.org/2001/XMLSchema-instance',
    'V8Exch': 'http://www.1c.ru/V8/1CV8DtUD/'
}

GUID_PATTERN = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'


def get_object_type(tag):
    """Извлекает читаемый тип объекта из XML-тега."""
    if 'CatalogObject.' in tag:
        category = 'Справочник'
        prefix = 'CatalogObject.'
    elif 'DocumentObject.' in tag:
        category = 'Документ'
        prefix = 'DocumentObject.'
    elif 'ChartOfCharacteristicTypesObject.' in tag:
        category = 'ПланВидовХарактеристик'
        prefix = 'ChartOfCharacteristicTypesObject.'
    elif 'InformationRegisterObject.' in tag:
        category = 'РегистрСведений'
        prefix = 'InformationRegisterObject.'
    elif 'AccumulationRegisterObject.' in tag:
        category = 'РегистрНакопления'
        prefix = 'AccumulationRegisterObject.'
    else:
        return tag
    if tag.startswith(prefix):
        obj_name = tag[len(prefix):]
    else:
        parts = tag.split('.')
        obj_name = parts[-1] if parts else tag
    return f"{category}.{obj_name}"


def extract_object(elem):
    """Извлекает атрибуты объекта из XML-элемента."""
    obj = {}
    ref_elem = elem.find('.//v8:Ref', NAMESPACES)
    if ref_elem is not None:
        obj['Ref'] = ref_elem.text
    desc_elem = elem.find('.//v8:Description', NAMESPACES)
    if desc_elem is not None:
        obj['Description'] = desc_elem.text
    code_elem = elem.find('.//v8:Code', NAMESPACES)
    if code_elem is not None:
        obj['Code'] = code_elem.text
    folder_elem = elem.find('.//v8:IsFolder', NAMESPACES)
    if folder_elem is not None:
        obj['IsFolder'] = folder_elem.text == 'true'
    parent_elem = elem.find('.//v8:Parent', NAMESPACES)
    if parent_elem is not None:
        parent_ref = parent_elem.text
        obj['Parent'] = parent_ref if parent_ref and parent_ref != '00000000-0000-0000-0000-000000000000' else None
    owner_elem = elem.find('.//v8:Owner', NAMESPACES)
    if owner_elem is not None:
        owner_ref = owner_elem.text
        obj['Owner'] = owner_ref if owner_ref and owner_ref != '00000000-0000-0000-0000-000000000000' else None
    for child in elem:
        tag = child.tag
        if '}' in tag:
            tag = tag.split('}', 1)[1]
        if tag in ['Ref', 'Description', 'Code', 'IsFolder', 'Parent', 'Owner']:
            continue
        if 'xsi:type' in child.attrib and child.attrib.get('{http://www.w3.org/2001/XMLSchema-instance}type', '').startswith('v8:CatalogRef.'):
            ref_text = child.text
            if ref_text and ref_text != '00000000-0000-0000-0000-000000000000':
                obj[tag] = ref_text
        elif child.text:
            obj[tag] = child.text
    obj['_tag'] = elem.tag
    obj['_type'] = get_object_type(elem.tag)
    return obj


def parse_xml(file_path):
    """Парсит XML-файл и возвращает объекты и дерево."""
    parser = ET.XMLParser(remove_blank_text=False, resolve_entities=False)
    tree = ET.parse(file_path, parser)
    root = tree.getroot()
    objects = []
    for elem in root.iter():
        tag = elem.tag
        if '}' in tag:
            tag = tag.split('}', 1)[1]
        if '.' in tag and ('Object.' in tag or tag.endswith('Object')):
            objects.append(extract_object(elem))
    return objects, tree


def find_all_guids(text):
    """Находит все GUID в тексте."""
    return set(re.findall(GUID_PATTERN, text, re.IGNORECASE))


def replace_guids_in_xml(original_xml_path, replacements):
    """
    XML-aware замена GUID: обходит дерево lxml и заменяет GUID только в элементах-ссылках
    (v8:Ref, v8:Parent, v8:Owner и любых элементах с xsi:type, содержащим 'Ref').
    Полностью сохраняет исходную структуру, пробелы и форматирование.
    """
    parser = ET.XMLParser(remove_blank_text=False, resolve_entities=False)
    tree = ET.parse(original_xml_path, parser)
    root = tree.getroot()

    xsi_type_attr = '{http://www.w3.org/2001/XMLSchema-instance}type'
    replaced_count = 0

    for elem in root.iter():
        tag = elem.tag
        if '}' in tag:
            local_tag = tag.split('}', 1)[1]
        else:
            local_tag = tag

        # Определяем, является ли элемент ссылочным:
        # 1) Тег Ref, Parent, Owner
        # 2) Любой элемент с xsi:type, содержащим 'Ref' (например, v8:CatalogRef.xxx)
        is_ref_element = (
            local_tag in ('Ref', 'Parent', 'Owner') or
            (xsi_type_attr in elem.attrib and 'Ref' in elem.attrib[xsi_type_attr])
        )

        if is_ref_element and elem.text and elem.text.strip() in replacements:
            old_guid = elem.text.strip()
            elem.text = replacements[old_guid]
            replaced_count += 1

    logger.info(f"XML-aware замена: заменено {replaced_count} GUID в элементах-ссылках")

    temp_dir = tempfile.mkdtemp()
    output_path = os.path.join(temp_dir, 'modified.xml')
    tree.write(output_path, encoding='utf-8', xml_declaration=True)

    # Добавляем BOM для совместимости с 1С
    with open(output_path, 'rb') as f:
        content = f.read()
    if not content.startswith(b'\xef\xbb\xbf'):
        content = b'\xef\xbb\xbf' + content
    with open(output_path, 'wb') as f:
        f.write(content)

    return output_path


def get_replacement_map(original_objects, modified_objects):
    """Строит карту замен GUID по парам объектов."""
    replacements = {}
    for orig, mod in zip(original_objects, modified_objects):
        old_guid = orig.get('Ref')
        new_guid = mod.get('Ref')
        if old_guid and new_guid and old_guid != new_guid:
            replacements[old_guid] = new_guid
    return replacements
