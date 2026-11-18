"""Explicit bounded run/fragment/structured-value retrieval, no language model."""
from __future__ import annotations
import collections, unicodedata
from .parsers import field_type
from .sources import need, jd

DERIVATION={'protocol_revision':'4.0','lexical':'scout.lexical.fields/4',
            'unicode_database':unicodedata.unidata_version,'units':'scout.units/1','fields':'scout.fields/1',
            'record_grouping':'scout.records/1','identifier_policies':'scout.identifiers/1'}
MAX_NORMALIZED=262144
MAX_QUERY_NORMALIZED=4096

def scalar_string(value):
    return type(value) is str and not any(0xD800<=ord(c)<=0xDFFF for c in value)

def normalize(text):
    return unicodedata.normalize('NFKC',text).casefold()

def runs(text, limit=MAX_NORMALIZED, status='LIMIT'):
    normalized=normalize(text)
    need(len(normalized)<=limit,status,'LEXICAL_NORMALIZATION_LIMIT')
    out=[];start=None;has_alnum=False
    for i,ch in enumerate(normalized):
        alnum=ch.isalnum()
        if alnum or unicodedata.category(ch).startswith('M'):
            if start is None:start=i
            has_alnum=has_alnum or alnum
        else:
            if start is not None and has_alnum:out.append(normalized[start:i])
            start=None;has_alnum=False
    if start is not None and has_alnum:out.append(normalized[start:])
    return out,len(normalized)

def query_groups(text):
    need(scalar_string(text),'INVALID_REQUEST','QUERY_INVALID')
    values,_=runs(text,MAX_QUERY_NORMALIZED,'INVALID_REQUEST')
    groups=sorted(set(values))
    need(0<len(groups)<=8,'INVALID_REQUEST','QUERY_INVALID')
    return groups

def compile_query(query):
    """Strict request compiler. Never guess intent or discard separators."""
    if type(query) is str:
        need(0<len(query)<=256,'INVALID_REQUEST','QUERY_INVALID')
        return {'legacy':True,'groups':query_groups(query),'raw':query}
    need(type(query) is dict and set(query)=={'schema','combine','scope','clauses'},'INVALID_REQUEST','QUERY_INVALID')
    need(query['schema']=='scout.query/3' and query['combine'] in ('and','or') and query['scope'] in ('unit','object'),'INVALID_REQUEST','QUERY_INVALID')
    clauses=query['clauses']
    need(type(clauses) is list and 1<=len(clauses)<=8,'INVALID_REQUEST','QUERY_INVALID')
    unique={};original_size=0;normalized_size=0
    for clause in clauses:
        need(type(clause) is dict and clause.get('kind') in ('run','fragment','identifier'),'INVALID_REQUEST','QUERY_INVALID')
        kind=clause['kind']
        keys={'kind','field','policy','value'} if kind=='identifier' else {'kind','value'}
        need(set(clause)==keys and scalar_string(clause['value']) and bool(clause['value']),'INVALID_REQUEST','QUERY_INVALID')
        value=clause['value'];original_size+=len(value)
        need(original_size<=256,'INVALID_REQUEST','QUERY_INVALID')
        if kind=='identifier':
            need(clause['field'] in ('external_ref','invoice_number') and clause['policy']=='raw_exact','INVALID_REQUEST','QUERY_INVALID')
            normalized=value;key=(kind,clause['field'],clause['policy'],value)
        else:
            normalized=normalize(value);values,n=runs(value,MAX_QUERY_NORMALIZED,'INVALID_REQUEST')
            need(values==[normalized],'INVALID_REQUEST','QUERY_INVALID')
            key=(kind,normalized)
        normalized_size+=len(normalized)
        need(normalized_size<=MAX_QUERY_NORMALIZED,'INVALID_REQUEST','QUERY_INVALID')
        compiled=dict(clause,normalized=normalized)
        # Stable representative for the deliberately literal typed comparator.
        if key not in unique or value<unique[key]['value']:unique[key]=compiled
    return {'legacy':False,'combine':query['combine'],'scope':query['scope'],
            'clauses':[unique[k] for k in sorted(unique)]}

def run_key(value):return 'r:'+jd(value)
def identifier_key(field,value):return 'i:'+jd([field,value])
def eligible_identifier(unit):
    field=field_type(unit['field']) if type(unit.get('field')) is str else None
    return field if unit.get('value_type')=='string' and field in ('external_ref','invoice_number') else None

def postings(units):
    """O(N) occurrences: legacy grams plus digest-bound whole values."""
    total=0
    for u in units:
        values,n=runs(u['text']);total+=n
        need(total<=MAX_NORMALIZED,'LIMIT','LEXICAL_NORMALIZATION_LIMIT')
        counts=collections.Counter()
        for value in values:
            counts.update('1:'+ch for ch in value)
            counts.update('2:'+value[i:i+2] for i in range(len(value)-1))
            counts[run_key(value)]+=1
        field=eligible_identifier(u)
        if field is not None:counts[identifier_key(field,u['text'])]+=1
        yield counts

def candidate_gram(group):return ('1:'+group) if len(group)==1 else ('2:'+group[:2])
def candidate_keys(plan):
    if plan['legacy']:return sorted({candidate_gram(g) for g in plan['groups']})
    keys=[]
    for c in plan['clauses']:
        if c['kind']=='run':keys.append(run_key(c['normalized']))
        elif c['kind']=='fragment':keys.append(candidate_gram(c['normalized']))
        else:keys.append(identifier_key(c['field'],c['value']))
    return sorted(set(keys))

def overlap_count(value,group):
    count=0;start=0
    while True:
        pos=value.find(group,start)
        if pos<0:return count
        count+=1;start=pos+1

def score(text,groups):
    values,_=runs(text)
    counts=[sum(overlap_count(value,g) for value in values) for g in groups]
    return {'matched_terms':sum(x>0 for x in counts),'term_occurrences':sum(counts)}

def typed_counts(unit,plan,arm):
    values,_=runs(unit['text']) if arm=='candidate' else ([],0)
    counts=[];field=eligible_identifier(unit)
    for c in plan['clauses']:
        if c['kind']=='identifier':count=int(field==c['field'] and unit['text']==c['value'])
        elif arm=='baseline':count=unit['text'].count(c['value'])
        elif c['kind']=='run':count=values.count(c['normalized'])
        else:count=sum(overlap_count(value,c['normalized']) for value in values)
        counts.append(count)
    return counts

def accepted(counts,plan):
    return all(counts) if plan['combine']=='and' else any(counts)

def counts_score(counts):
    return {'matched_terms':sum(n>0 for n in counts),'term_occurrences':sum(counts)}
