"""Bounded exact text units; no semantic model and no source-path access."""
from __future__ import annotations
import csv, io, re, unicodedata
from decimal import Decimal
from .sources import Fault, need, strict_json, canonical, jd, digest
from .xlsx_cells import parse_cells

MAX_UNITS=2000
MAX_TEXT=65536
ALIASES={
 'organization':('organization','organization_name','org_name','company_name'),
 'email':('email','contact_email'), 'external_ref':('external_ref',),
 'invoice_number':('invoice_number','invoice_id'), 'amount':('amount','total_amount'),
 'currency':('currency',), 'document_type':('document_type','record_type'),
 'description':('description',),
}
LOOKUP={a:k for k,vals in ALIASES.items() for a in vals}
def normal_field(s): return '_'.join(unicodedata.normalize('NFKC',s).casefold().split())
def field_type(s): return LOOKUP.get(normal_field(s))
def tokens(s): return re.findall(r'[^\W_]+',unicodedata.normalize('NFKC',s).casefold(),re.UNICODE)

def parse(raw, fmt, *, interpretation='generic',word_interpretation='generic',text_field_profile=None):
    units=[]; size=0
    def add(text,locator,field=None,value_type='string'):
        nonlocal size
        need(type(text) is str)
        size+=len(text.encode()); need(size<=MAX_TEXT and len(units)<MAX_UNITS,'LIMIT','EXTRACTED_UNIT_LIMIT')
        u={'unit_id':'u'+str(len(units)), 'text':text, 'locator':locator,'field':field,'value_type':value_type}
        units.append(u)
    try:
        if fmt=='csv':
            text=raw.decode('utf-8-sig'); reader=csv.reader(io.StringIO(text,newline=''),strict=True)
            try: headers=next(reader)
            except StopIteration: raise Fault('INVALID_SOURCE','CSV_HEADER_MISSING')
            need(headers and all(h.strip() for h in headers) and len(set(headers))==len(headers),reason='CSV_HEADER_INVALID')
            need(len({normal_field(h) for h in headers})==len(headers),reason='CSV_HEADER_ALIAS_COLLISION')
            for rowno,row in enumerate(reader,1):
                need(len(row)==len(headers),reason='CSV_WIDTH_INVALID')
                for col,(name,value) in enumerate(zip(headers,row)):
                    add(value,{'kind':'csv','record':rowno,'column':col,'header':name},name)
        elif fmt in ('json','record'):
            root=strict_json(raw.decode('utf-8'),number_exact=True)
            def walk(v,path,field,depth):
                need(depth<=16,'LIMIT','JSON_DEPTH_LIMIT')
                if type(v) is dict:
                    for k,val in v.items(): walk(val,path+'/'+k.replace('~','~0').replace('/','~1'),k,depth+1)
                elif type(v) is list:
                    for n,val in enumerate(v): walk(val,path+'/'+str(n),field,depth+1)
                else:
                    typ='null' if v is None else 'boolean' if type(v) is bool else 'number' if type(v) in (int,float,Decimal) else 'string'
                    # Re-encode logical values exactly; the locator is a JSON pointer, never a raw byte span.
                    val=v if type(v) is str else str(v) if type(v) is Decimal else canonical(v)
                    if type(v) is Decimal: need(v.is_finite(),reason='NONFINITE_JSON')
                    add(val,{'kind':'sqlite_field' if fmt=='record' else 'json','pointer':path,'scalar_type':typ},field,typ)
            walk(root,'',None,0)
        elif fmt=='xlsx':
            parse_cells(raw,add)
        elif fmt in ('docx','pptx'):
            raise Fault('UNSUPPORTED','OFFICE_NOT_INCLUDED_IN_DIAGNOSTIC')
        else: raise Fault('UNSUPPORTED','FORMAT_NOT_SUPPORTED')
    except Fault: raise
    except (ValueError,UnicodeError,csv.Error,RecursionError) as exc: raise Fault('INVALID_SOURCE','PARSE_INVALID') from exc
    except Exception as exc:
        if fmt in ('docx','pptx'): raise Fault('UNSUPPORTED','OFFICE_PROFILE_REJECTED') from exc
        raise
    return units
