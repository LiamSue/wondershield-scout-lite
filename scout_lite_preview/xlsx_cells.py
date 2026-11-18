"""Independent, bounded, read-only subset of OOXML spreadsheet cell storage.

This module does not render, evaluate formulas, follow external references, or
interpret styles/dates. XML parts and values remain tied to the captured bytes.
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from xml.etree import ElementTree

from .sources import Fault, need

S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
P = '{http://schemas.openxmlformats.org/package/2006/relationships}'
C = '{http://schemas.openxmlformats.org/package/2006/content-types}'
REL_BASE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
MAX_ENTRIES = 256
MAX_EXPANDED = 4 * 1024 * 1024
MAX_PART = 1024 * 1024
MAX_NODES = 20000
MAX_DEPTH = 32
MAX_SHEETS = 16
MAX_SHARED = 10000
MAX_CELLS = 2000
CELL = re.compile(r'([A-Z]{1,3})([1-9][0-9]{0,6})\Z')
INDEX = re.compile(r'(0|[1-9][0-9]{0,7})\Z')


def _fail(reason, status='UNSUPPORTED'):
    raise Fault(status, reason)


def _xml(raw):
    try:
        text = raw.decode('utf-8-sig')
        need(re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', text, re.IGNORECASE) is None,
             'UNSUPPORTED', 'XLSX_XML_DECLARATION_UNSUPPORTED')
        root = ElementTree.fromstring(text)
    except (UnicodeError, ElementTree.ParseError) as exc:
        raise Fault('INVALID_SOURCE', 'XLSX_XML_INVALID_OR_NON_UTF8') from exc
    paths = {id(root): [[root.tag, 1]]}
    stack = [(root, 1)]
    count = 0
    while stack:
        element, depth = stack.pop()
        count += 1
        need(count <= MAX_NODES and depth <= MAX_DEPTH, 'LIMIT', 'XLSX_XML_COMPLEXITY_LIMIT')
        indices = {}
        children = list(element)
        for child in children:
            indices[child.tag] = indices.get(child.tag, 0) + 1
            paths[id(child)] = paths[id(element)] + [[child.tag, indices[child.tag]]]
        stack.extend((child, depth + 1) for child in reversed(children))
    return root, paths


def _part(archive, names, name):
    need(name in names, 'UNSUPPORTED', 'XLSX_REQUIRED_PART_MISSING')
    info = names[name]
    need(not info.is_dir() and info.file_size <= MAX_PART, 'LIMIT', 'XLSX_PART_LIMIT')
    raw = archive.read(info)
    need(len(raw) == info.file_size, 'INVALID_SOURCE', 'XLSX_PART_SIZE_MISMATCH')
    root, paths = _xml(raw)
    return root, paths, hashlib.sha256(raw).hexdigest()


def _target(target):
    need(type(target) is str and target and '\\' not in target and '?' not in target and '#' not in target and '%' not in target and ':' not in target,
         'UNSUPPORTED', 'XLSX_RELATIONSHIP_TARGET_UNSUPPORTED')
    if target.startswith('/'):
        target = target[1:]
    else:
        target = 'xl/' + target
    parts = target.split('/')
    need(all(part not in ('', '.', '..') for part in parts) and parts[0] == 'xl',
         'UNSUPPORTED', 'XLSX_RELATIONSHIP_TARGET_UNSUPPORTED')
    path = str(PurePosixPath(target))
    need(path.startswith('xl/'), 'UNSUPPORTED', 'XLSX_RELATIONSHIP_TARGET_UNSUPPORTED')
    return path


def _text_leaves(container):
    leaves = []
    for child in container:
        if child.tag == S + 't':
            leaves.append(child)
        elif child.tag == S + 'r':
            leaves.extend(grandchild for grandchild in child if grandchild.tag == S + 't')
        elif child.tag not in (S + 'rPh', S + 'phoneticPr'):
            _fail('XLSX_STRING_STRUCTURE_UNSUPPORTED')
    return leaves


def _binding(member, member_sha, paths, leaf):
    text = leaf.text or ''
    return {'leaf_member': member, 'leaf_member_sha256': member_sha,
            'leaf_expanded_path': paths[id(leaf)], 'payload_state': 'nonempty' if text else 'empty',
            'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}


def _coordinate(value):
    match = CELL.fullmatch(value or '')
    need(match is not None, 'UNSUPPORTED', 'XLSX_CELL_COORDINATE_UNVERIFIED')
    column = 0
    for char in match.group(1):
        column = column * 26 + ord(char) - 64
    need(column <= 16384 and int(match.group(2)) <= 1048576,
         'UNSUPPORTED', 'XLSX_CELL_COORDINATE_UNVERIFIED')
    return int(match.group(2))


def parse_cells(raw, add):
    """Call add(text, locator, None, value_type) for each verified stored cell."""
    need(type(raw) is bytes and len(raw) <= 262144, 'LIMIT', 'XLSX_SOURCE_BYTE_LIMIT')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            need(len(infos) <= MAX_ENTRIES, 'LIMIT', 'XLSX_ENTRY_LIMIT')
            names = {}
            expanded = 0
            for info in infos:
                name = info.filename
                parts = name.rstrip('/').split('/')
                need(name and not name.startswith('/') and '\\' not in name and all(p not in ('', '.', '..') for p in parts),
                     'UNSUPPORTED', 'XLSX_ARCHIVE_NAME_UNSUPPORTED')
                need(name not in names and not (info.flag_bits & 1), 'UNSUPPORTED', 'XLSX_ARCHIVE_MEMBER_UNSUPPORTED')
                need(info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                     'UNSUPPORTED', 'XLSX_COMPRESSION_UNSUPPORTED')
                names[name] = info
                expanded += info.file_size
            need(expanded <= MAX_EXPANDED, 'LIMIT', 'XLSX_EXPANDED_LIMIT')
            content_types, _, _ = _part(archive, names, '[Content_Types].xml')
            root_rels, _, _ = _part(archive, names, '_rels/.rels')
            need(content_types.tag == C + 'Types' and root_rels.tag == P + 'Relationships',
                 'INVALID_SOURCE', 'XLSX_PACKAGE_STRUCTURE_INVALID')
            workbook_types = [item.get('ContentType') for item in content_types
                              if item.tag == C + 'Override' and item.get('PartName') == '/xl/workbook.xml']
            need(workbook_types == ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'],
                 'UNSUPPORTED', 'XLSX_WORKBOOK_CONTENT_TYPE_UNSUPPORTED')
            office_rels = [item for item in root_rels if item.tag == P + 'Relationship'
                           and item.get('Type') == REL_BASE + 'officeDocument']
            need(len(office_rels) == 1 and office_rels[0].get('TargetMode') in (None, 'Internal')
                 and office_rels[0].get('Target', '').lstrip('/') == 'xl/workbook.xml',
                 'UNSUPPORTED', 'XLSX_WORKBOOK_RELATIONSHIP_UNSUPPORTED')
            workbook, _, _ = _part(archive, names, 'xl/workbook.xml')
            rels, _, _ = _part(archive, names, 'xl/_rels/workbook.xml.rels')
            need(workbook.tag == S + 'workbook' and rels.tag == P + 'Relationships',
                 'UNSUPPORTED', 'XLSX_NAMESPACE_UNSUPPORTED')
            relationships = {}
            all_relationship_ids = set()
            for rel in rels:
                if rel.tag != P + 'Relationship':
                    continue
                rel_id, typ = rel.get('Id'), rel.get('Type')
                need(rel_id and rel_id not in all_relationship_ids, 'INVALID_SOURCE', 'XLSX_RELATIONSHIP_INVALID')
                all_relationship_ids.add(rel_id)
                if typ in (REL_BASE + 'worksheet', REL_BASE + 'sharedStrings', REL_BASE + 'chartsheet'):
                    need(rel.get('TargetMode') in (None, 'Internal'), 'UNSUPPORTED', 'XLSX_EXTERNAL_RELATIONSHIP_UNSUPPORTED')
                    relationships[rel_id] = (typ, _target(rel.get('Target')))
            sheet_list = workbook.find(S + 'sheets')
            need(sheet_list is not None, 'INVALID_SOURCE', 'XLSX_SHEETS_MISSING')
            sheets = [child for child in sheet_list if child.tag == S + 'sheet']
            need(0 < len(sheets) <= MAX_SHEETS, 'LIMIT', 'XLSX_SHEET_LIMIT')
            shared_rel = [target for typ, target in relationships.values() if typ == REL_BASE + 'sharedStrings']
            need(len(shared_rel) <= 1, 'INVALID_SOURCE', 'XLSX_SHARED_RELATIONSHIP_AMBIGUOUS')
            shared = []
            if shared_rel:
                root, paths, part_sha = _part(archive, names, shared_rel[0])
                need(root.tag == S + 'sst', 'INVALID_SOURCE', 'XLSX_SHARED_STRINGS_INVALID')
                shared = [child for child in root if child.tag == S + 'si']
                need(len(shared) <= MAX_SHARED, 'LIMIT', 'XLSX_SHARED_STRINGS_LIMIT')
                shared = [(shared_rel[0], part_sha, paths, _text_leaves(entry)) for entry in shared]
            seen_sheets, seen_paths = set(), set()
            cells = 0
            for sheet in sheets:
                sheet_name, rid = sheet.get('name'), sheet.get(R + 'id')
                need(type(sheet_name) is str and 0 < len(sheet_name) <= 31 and sheet_name not in seen_sheets,
                     'INVALID_SOURCE', 'XLSX_SHEET_NAME_INVALID')
                seen_sheets.add(sheet_name)
                rel = relationships.get(rid)
                need(rel is not None and rel[0] in (REL_BASE + 'worksheet', REL_BASE + 'chartsheet') and rel[1] not in seen_paths,
                     'UNSUPPORTED', 'XLSX_SHEET_RELATIONSHIP_UNVERIFIED')
                member = rel[1]
                seen_paths.add(member)
                if rel[0] == REL_BASE + 'chartsheet':
                    need(member in names and not names[member].is_dir(), 'UNSUPPORTED', 'XLSX_CHARTSHEET_PART_MISSING')
                    continue
                root, paths, part_sha = _part(archive, names, member)
                need(root.tag == S + 'worksheet', 'INVALID_SOURCE', 'XLSX_WORKSHEET_INVALID')
                data = root.find(S + 'sheetData')
                need(data is not None, 'INVALID_SOURCE', 'XLSX_SHEET_DATA_MISSING')
                seen_cells = set()
                for row in data:
                    if row.tag != S + 'row':
                        continue
                    declared_row = row.get('r')
                    if declared_row is not None:
                        need(declared_row.isascii() and declared_row.isdigit() and int(declared_row) > 0,
                             'INVALID_SOURCE', 'XLSX_ROW_REFERENCE_INVALID')
                    for cell in row:
                        if cell.tag != S + 'c':
                            continue
                        cells += 1
                        need(cells <= MAX_CELLS, 'LIMIT', 'XLSX_CELL_LIMIT')
                        address = cell.get('r')
                        row_index = _coordinate(address)
                        need(address not in seen_cells and (declared_row is None or int(declared_row) == row_index),
                             'INVALID_SOURCE', 'XLSX_CELL_REFERENCE_CONFLICT')
                        seen_cells.add(address)
                        if any(child.tag == S + 'f' for child in cell):
                            continue
                        typ = cell.get('t', 'n')
                        vals = [child for child in cell if child.tag == S + 'v']
                        inline = [child for child in cell if child.tag == S + 'is']
                        need(len(vals) <= 1 and len(inline) <= 1, 'INVALID_SOURCE', 'XLSX_CELL_VALUE_AMBIGUOUS')
                        if not vals and not inline:
                            continue
                        locator = {'kind':'office', 'contract_version':'scout.xlsx-basic-cell/1',
                                   'unit_kind':'spreadsheet_cell', 'owner_member':member, 'part':member,
                                   'unit_container_expanded_path':paths[id(cell)], 'sheet_name':sheet_name, 'cell':address,
                                   'derivation':{'operation':'ordered_native_unit_leaves','separator':''}}
                        value_type = 'string'
                        if typ == 'inlineStr':
                            need(len(inline) == 1 and not vals, 'INVALID_SOURCE', 'XLSX_INLINE_STRING_INVALID')
                            leaves = _text_leaves(inline[0])
                            texts = [leaf.text or '' for leaf in leaves]
                            bindings = [_binding(member, part_sha, paths, leaf) for leaf in leaves]
                            locator['cell_type'] = 'inline_string'
                        elif typ == 's':
                            need(len(vals) == 1 and not inline and INDEX.fullmatch(vals[0].text or '') is not None,
                                 'INVALID_SOURCE', 'XLSX_SHARED_INDEX_INVALID')
                            index = int(vals[0].text)
                            need(index < len(shared), 'INVALID_SOURCE', 'XLSX_SHARED_INDEX_INVALID')
                            leaf_member, leaf_sha, leaf_paths, leaves = shared[index]
                            texts = [leaf.text or '' for leaf in leaves]
                            bindings = [_binding(leaf_member, leaf_sha, leaf_paths, leaf) for leaf in leaves]
                            locator.update(cell_type='shared_string', shared_string_index=index)
                        elif typ in ('n', 'b', 'str', 'd'):
                            need(len(vals) == 1 and not inline and vals[0].text is not None,
                                 'INVALID_SOURCE', 'XLSX_CELL_VALUE_INVALID')
                            value = vals[0].text
                            if typ == 'n':
                                try:
                                    number = Decimal(value)
                                except InvalidOperation as exc:
                                    raise Fault('INVALID_SOURCE', 'XLSX_NUMBER_INVALID') from exc
                                need(number.is_finite(), 'INVALID_SOURCE', 'XLSX_NUMBER_INVALID')
                                value_type = 'number'
                                locator.update(cell_type='number', numeric_interpretation='RAW_STYLE_AND_DATE_SYSTEM_NOT_EVALUATED')
                            elif typ == 'b':
                                need(value in ('0', '1'), 'INVALID_SOURCE', 'XLSX_BOOLEAN_INVALID')
                                value_type = 'boolean'
                                locator.update(cell_type='boolean', logical_boolean=value == '1')
                            elif typ == 'd':
                                locator.update(cell_type='date_raw', date_interpretation='NOT_EVALUATED')
                            else:
                                locator['cell_type'] = 'string'
                            texts = [value]
                            bindings = [_binding(member, part_sha, paths, vals[0])]
                        else:
                            _fail('XLSX_CELL_TYPE_UNSUPPORTED')
                        locator['leaf_bindings'] = bindings
                        add(''.join(texts), locator, None, value_type)
    except Fault:
        raise
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError, ValueError, OverflowError) as exc:
        raise Fault('INVALID_SOURCE', 'XLSX_ARCHIVE_INVALID') from exc
