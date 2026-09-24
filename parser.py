#!/usr/bin/env python3

"""
Парсер структуры объектов из XML-выгрузки 1С.
Использует общий модуль parsing.py.
"""

import json
import sys

from parsing import parse_xml


def main():
    if len(sys.argv) < 2:
        print("Usage: python parser.py <path_to_xml_file> [output_json_file]")
        sys.exit(1)

    xml_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else 'objects.json'

    objects = parse_xml(xml_file)[0]

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(objects, f, ensure_ascii=False, indent=2)

    print(f"Found {len(objects)} objects. Structure saved to {output_file}")


if __name__ == '__main__':
    main()
