"""Тесты модуля parsing.py."""
import os

import pytest

from parsing import (
    CATEGORY_MAP,
    extract_object,
    get_object_type,
    get_replacement_map,
    parse_xml,
    replace_guids_in_xml,
)

# ============ get_object_type ============

class TestGetObjectType:
    def test_catalog(self):
        assert get_object_type('{ns}CatalogObject.Номенклатура') == 'Справочник.Номенклатура'

    def test_document(self):
        assert get_object_type('{ns}DocumentObject.РеализацияТоваров') == 'Документ.РеализацияТоваров'

    def test_chart_of_character_types(self):
        assert get_object_type('{ns}ChartOfCharacteristicTypesObject.ВидыНоменклатуры') == \
            'ПланВидовХарактеристик.ВидыНоменклатуры'

    def test_information_register(self):
        assert get_object_type('{ns}InformationRegisterObject.Цены') == \
            'РегистрСведений.Цены'

    def test_accumulation_register(self):
        assert get_object_type('{ns}AccumulationRegisterObject.Остатки') == \
            'РегистрНакопления.Остатки'

    def test_unknown_tag_returns_as_is(self):
        assert get_object_type('{ns}SomeOtherTag') == '{ns}SomeOtherTag'

    def test_tag_without_namespace(self):
        assert get_object_type('CatalogObject.Склады') == 'Справочник.Склады'


# ============ CATEGORY_MAP ============

class TestCategoryMap:
    def test_catalog_mapping(self):
        assert CATEGORY_MAP['Справочник'] == 'Справочник'

    def test_register_mapping(self):
        assert CATEGORY_MAP['Регистр сведений'] == 'РегистрСведений'
        assert CATEGORY_MAP['РегистрСведений'] == 'РегистрСведений'

    def test_business_process_mapping(self):
        assert CATEGORY_MAP['Бизнес-процесс'] == 'БизнесПроцесс'
        assert CATEGORY_MAP['БизнесПроцесс'] == 'БизнесПроцесс'

    def test_unknown_category_passthrough(self):
        # Если категории нет в мапе, sync_db.py использует исходное имя
        assert CATEGORY_MAP.get('НесуществующаяКатегория', 'НесуществующаяКатегория') == \
            'НесуществующаяКатегория'


# ============ extract_object ============

class TestExtractObject:
    def _make_elem(self, xml_str):
        from lxml import etree as ET
        parser = ET.XMLParser(remove_blank_text=False, resolve_entities=False)
        tree = ET.fromstring(xml_str, parser)
        return tree

    def test_full_object_extraction(self):
        xml = '''<v8:CatalogObject.Номенклатура xmlns:v8="http://v8.1c.ru/8.1/data/enterprise/current-config">
            <v8:Ref>aaaaaaaa-1111-1111-1111-111111111111</v8:Ref>
            <v8:Description>Товар А</v8:Description>
            <v8:Code>00001</v8:Code>
            <v8:IsFolder>false</v8:IsFolder>
            <v8:Parent>00000000-0000-0000-0000-000000000000</v8:Parent>
            <v8:Owner>00000000-0000-0000-0000-000000000000</v8:Owner>
        </v8:CatalogObject.Номенклатура>'''
        elem = self._make_elem(xml)
        obj = extract_object(elem)

        assert obj['Ref'] == 'aaaaaaaa-1111-1111-1111-111111111111'
        assert obj['Description'] == 'Товар А'
        assert obj['Code'] == '00001'
        assert obj['IsFolder'] is False
        # Parent и Owner с нулевым GUID должны быть None
        assert obj['Parent'] is None
        assert obj['Owner'] is None
        assert obj['_type'] == 'Справочник.Номенклатура'

    def test_folder_object(self):
        xml = '''<v8:CatalogObject.Склады xmlns:v8="http://v8.1c.ru/8.1/data/enterprise/current-config">
            <v8:Ref>bbbbbbbb-2222-2222-2222-222222222222</v8:Ref>
            <v8:Description>Группа складов</v8:Description>
            <v8:IsFolder>true</v8:IsFolder>
        </v8:CatalogObject.Склады>'''
        elem = self._make_elem(xml)
        obj = extract_object(elem)
        assert obj['IsFolder'] is True

    def test_real_parent(self):
        xml = '''<v8:CatalogObject.Склады xmlns:v8="http://v8.1c.ru/8.1/data/enterprise/current-config">
            <v8:Ref>bbbbbbbb-2222-2222-2222-222222222222</v8:Ref>
            <v8:Parent>cccccccc-3333-3333-3333-333333333333</v8:Parent>
        </v8:CatalogObject.Склады>'''
        elem = self._make_elem(xml)
        obj = extract_object(elem)
        assert obj['Parent'] == 'cccccccc-3333-3333-3333-333333333333'


# ============ parse_xml ============

class TestParseXml:
    def test_parse_sample_xml(self, sample_xml_path):
        objects, tree = parse_xml(sample_xml_path)
        assert len(objects) == 3
        types = [o['_type'] for o in objects]
        assert 'Справочник.Номенклатура' in types
        assert 'Справочник.Контрагенты' in types
        assert 'Документ.РеализацияТоваров' in types

    def test_parse_returns_tree(self, sample_xml_path):
        objects, tree = parse_xml(sample_xml_path)
        from lxml import etree as ET
        assert isinstance(tree, ET._ElementTree) or hasattr(tree, 'getroot')


# ============ get_replacement_map ============

class TestGetReplacementMap:
    def test_basic_replacement(self):
        orig = [{'Ref': 'old-1'}, {'Ref': 'old-2'}]
        mod = [{'Ref': 'new-1'}, {'Ref': 'new-2'}]
        result = get_replacement_map(orig, mod)
        assert result == {'old-1': 'new-1', 'old-2': 'new-2'}

    def test_mismatched_lengths_raises(self):
        with pytest.raises(ValueError, match='Длины массивов не совпадают'):
            get_replacement_map([{'Ref': 'a'}], [{'Ref': 'b'}, {'Ref': 'c'}])

    def test_empty_guids_skipped(self):
        orig = [{'Ref': 'a'}, {'Ref': None}]
        mod = [{'Ref': 'b'}, {'Ref': 'c'}]
        result = get_replacement_map(orig, mod)
        assert result == {'a': 'b'}

    def test_unchanged_guids_skipped(self):
        orig = [{'Ref': 'same'}]
        mod = [{'Ref': 'same'}]
        result = get_replacement_map(orig, mod)
        assert result == {}

    def test_empty_inputs(self):
        assert get_replacement_map([], []) == {}


# ============ replace_guids_in_xml ============

class TestReplaceGuidsInXml:
    def test_basic_replacement(self, sample_xml_path, tmp_path):
        replacements = {
            'aaaaaaaa-1111-1111-1111-111111111111': 'zzzzzzzz-9999-9999-9999-999999999999'
        }
        out_path = replace_guids_in_xml(sample_xml_path, replacements)
        try:
            with open(out_path, 'rb') as f:
                content = f.read()
            # BOM
            assert content.startswith(b'\xef\xbb\xbf')
            text = content[3:].decode('utf-8')
            # New GUID присутствует
            assert 'zzzzzzzz-9999-9999-9999-999999999999' in text
            # Old GUID отсутствует
            assert 'aaaaaaaa-1111-1111-1111-111111111111' not in text
        finally:
            if os.path.exists(out_path):
                os.remove(out_path)
                # Удаляем темп-каталог, если пуст
                import shutil
                shutil.rmtree(os.path.dirname(out_path), ignore_errors=True)

    def test_preserves_other_guids(self, sample_xml_path):
        """GUID, которых нет в replacements, не должны меняться."""
        replacements = {
            'aaaaaaaa-1111-1111-1111-111111111111': 'zzzzzzzz-9999-9999-9999-999999999999'
        }
        out_path = replace_guids_in_xml(sample_xml_path, replacements)
        try:
            with open(out_path, 'rb') as f:
                content = f.read()[3:].decode('utf-8')
            # bbb... и ccc... не должны измениться
            assert 'bbbbbbbb-2222-2222-2222-222222222222' in content
            assert 'cccccccc-3333-3333-3333-333333333333' in content
        finally:
            if os.path.exists(out_path):
                os.remove(out_path)
                import shutil
                shutil.rmtree(os.path.dirname(out_path), ignore_errors=True)

    def test_empty_replacements(self, sample_xml_path):
        """Пустой replacements — файл не меняется."""
        out_path = replace_guids_in_xml(sample_xml_path, {})
        try:
            with open(out_path, 'rb') as f:
                content = f.read()
            # Все GUID на месте
            assert b'aaaaaaaa-1111-1111-1111-111111111111' in content
        finally:
            if os.path.exists(out_path):
                os.remove(out_path)
                import shutil
                shutil.rmtree(os.path.dirname(out_path), ignore_errors=True)
