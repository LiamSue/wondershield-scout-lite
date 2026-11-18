"""Finite deterministic task functions. No source I/O, grants or guessed semantics."""
from __future__ import annotations
import datetime, re, unicodedata
from decimal import Decimal, InvalidOperation
from .parsers import field_type

LABELS={'organization':'ORGANIZATION_NAME','email':'CONTACT_EMAIL','external_ref':'EXTERNAL_REFERENCE','invoice_number':'INVOICE_NUMBER','amount':'AMOUNT','currency':'CURRENCY','document_type':'DOCUMENT_TYPE','description':'DESCRIPTION'}
EMAIL=re.compile(r'(?<![A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-])[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+(?![A-Za-z0-9-])')
DATE=re.compile(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)')

def fields(units):
    out={}
    for u in units:
        if u['field'] is not None:
            canonical=field_type(u['field'])
            if canonical and u['value_type'] not in ('null','boolean') and u['text'].strip():
                out.setdefault(canonical,[]).append(u)
    return out

def recognize(units,evidence,arm):
    out=[]
    for u in units:
        if u['field'] is not None:
            f=field_type(u['field'])
            out.append(dict(kind='field',label=LABELS.get(f,'OTHER_FIELD'),value=u['text'],canonical_field=f,start=None,end=None,evidence=[evidence(u)]))
        if arm=='baseline': continue
        for label,pattern in (('EMAIL',EMAIL),('ISO_DATE',DATE)):
            for m in pattern.finditer(u['text']):
                if label=='ISO_DATE':
                    try: datetime.date.fromisoformat(m.group())
                    except ValueError: continue
                out.append(dict(kind='span',label=label,value=m.group(),canonical_field=None,start=m.start(),end=m.end(),evidence=[evidence(u)]))
    return out

def email_valid(value): return EMAIL.fullmatch(value.strip()) is not None

def amount_valid(u):
    try: return u['value_type'] not in ('null','boolean') and Decimal(u['text']).is_finite()
    except (ValueError,InvalidOperation): return False

def classify(units,evidence,arm):
    f=fields(units); labels=[]
    if arm=='candidate':
        if f.get('organization') and any(email_valid(u['text']) for u in f.get('email',[])): labels.append('contact_record')
        if f.get('invoice_number') and any(amount_valid(u) for u in f.get('amount',[])) and any(re.fullmatch('[A-Z]{3}',u['text']) for u in f.get('currency',[])): labels.append('invoice_record')
        if any(unicodedata.normalize('NFKC',u['text']).casefold().strip()=='policy' for u in f.get('document_type',[])): labels.append('policy_document')
    selected=[u for u in units if u['field'] is not None]
    result={'labels':sorted(labels),'field_labels':[{'unit_id':u['unit_id'],'label':LABELS.get(field_type(u['field']),'OTHER_FIELD')} for u in selected], 'evidence':[evidence(u) for u in selected]}
    return ('UNCLASSIFIED' if not labels else 'AMBIGUOUS' if len(labels)>1 else 'OK'),[result]

def normalize_name(s): return ' '.join(unicodedata.normalize('NFKC',s).casefold().split())
def normalize_ref(s): return unicodedata.normalize('NFKC',s).strip()
def normalize_email(s):
    s=s.strip()
    if not email_valid(s): return ''
    local,domain=s.rsplit('@',1); return local+'@'+domain.lower()

def entity(units):
    f=fields(units); out={}; ambiguous=False
    for k,norm in (('organization',normalize_name),('email',normalize_email),('external_ref',normalize_ref)):
        vals={norm(u['text']) for u in f.get(k,[])}-{''}
        if len(vals)>1: ambiguous=True
        out[k]=next(iter(vals)) if len(vals)==1 else ''
    out['ref_values']={normalize_ref(u['text']) for u in f.get('external_ref',[])}-{''}
    out['ambiguous']=ambiguous
    out['units']=[u for k in ('organization','email','external_ref') for u in f.get(k,[])]
    out['raw_names']={u['text'] for u in f.get('organization',[])}
    return out
