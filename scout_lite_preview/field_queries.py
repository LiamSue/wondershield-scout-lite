"""Explicit source field, structural record and identifier lookup semantics only."""
from __future__ import annotations
import copy, unicodedata
from .sources import Fault, need, ident, canonical, jd, strict_json, identity
from .lexical import scalar_string, normalize, runs, overlap_count

POLICIES=('scout.id.raw/1','scout.id.nfkc_trim/1','scout.id.nfkc_trim_casefold/1')
TYPES=('any','string','number','boolean','null')
SQL_COLUMNS={'/organization':'org_name','/email':'contact_email','/external_ref':'external_ref','/description':'description','/document_type':'record_type'}

def req(ok,reason='FIELD_QUERY_INVALID'): need(ok,'INVALID_REQUEST',reason)
def name(v):return scalar_string(v) and ident(v)
def closed(v,keys):return type(v) is dict and set(v)==set(keys)
def path_valid(path):
    req(type(path) is list and len(path)<=16)
    for x in path:
        req(type(x) is dict and len(x)==1)
        if 'key' in x:req(scalar_string(x['key']) and len(x['key'])<=256)
        elif 'index' in x:req(type(x['index']) is int and x['index']>=0)
        else:req(x=={'each':'array'})

def selector_valid(s):
    req(type(s) is dict)
    if s.get('kind')=='csv':req(closed(s,('kind','header')) and scalar_string(s['header']) and 0<len(s['header'])<=256)
    elif s.get('kind')=='json':req(closed(s,('kind','path')));path_valid(s['path'])
    elif s.get('kind')=='sqlite':req(closed(s,('kind','column')) and s['column'] in SQL_COLUMNS.values())
    else:req(False)

def validate_config(config):
    """Validate before sorting. This never removes duplicate declarations."""
    try:req(len(canonical(config).encode('utf-8'))<=262144,'CONFIG_BYTE_LIMIT')
    except (ValueError,UnicodeError,TypeError):raise Fault('INVALID_REQUEST','CONFIG_INVALID')
    profiles=config.get('field_profiles',[]);req(type(profiles) is list and len(profiles)<=16,'PROFILE_CONFIG_INVALID')
    kinds={s['system']:s['kind'] for s in config['sources']};seen=set();out=[]
    for p in profiles:
        req(closed(p,('schema','id','bindings')) and p['schema']=='scout.fields/1' and name(p['id']),'PROFILE_CONFIG_INVALID')
        req(p['id'] not in seen,'PROFILE_ID_DUPLICATE');seen.add(p['id'])
        req(type(p['bindings']) is list and 1<=len(p['bindings'])<=32,'PROFILE_CONFIG_INVALID')
        bindings=[];count=0
        for b in p['bindings']:
            req(closed(b,('system','format','object_ids','record','fields')),'PROFILE_CONFIG_INVALID')
            req(name(b['system']) and b['system'] in kinds and b['format'] in ('csv','json','record'),'PROFILE_CONFIG_INVALID')
            req(kinds[b['system']]==('sqlite' if b['format']=='record' else 'document'),'PROFILE_CONFIG_INVALID')
            ids=b['object_ids'];req(ids=='*' or (type(ids) is list and 1<=len(ids)<=1000 and all(name(x) for x in ids)),'PROFILE_CONFIG_INVALID')
            if type(ids) is list:req(len(set(ids))==len(ids),'PROFILE_OBJECT_DUPLICATE')
            for prev in bindings:
                if (prev['system'],prev['format'])==(b['system'],b['format']):
                    req(not (ids=='*' or prev['object_ids']=='*' or set(ids)&set(prev['object_ids'])),'PROFILE_BINDING_CONFLICT')
            rec=b['record']
            if rec is not None:
                req(type(rec) is dict and rec.get('kind')==('sqlite' if b['format']=='record' else b['format']),'PROFILE_RECORD_INVALID')
                if b['format']=='json':req(closed(rec,('kind','path')),'PROFILE_RECORD_INVALID');path_valid(rec['path'])
                else:req(closed(rec,('kind',)),'PROFILE_RECORD_INVALID')
            req(type(b['fields']) is list and len(b['fields'])<=32,'PROFILE_SELECTOR_LIMIT');count+=len(b['fields']);req(count<=32,'PROFILE_SELECTOR_LIMIT')
            fields=[];fids=set();sels=set()
            for f in b['fields']:
                req(closed(f,('id','selector','identifiers')) and name(f['id']),'PROFILE_FIELD_INVALID');selector_valid(f['selector'])
                req(f['selector']['kind']==('sqlite' if b['format']=='record' else b['format']),'PROFILE_FIELD_INVALID')
                sel=canonical(f['selector']);req(f['id'] not in fids and sel not in sels,'PROFILE_FIELD_DUPLICATE');fids.add(f['id']);sels.add(sel)
                grants=f['identifiers'];req(type(grants) is list and len(grants)<=8,'PROFILE_GRANT_INVALID');gs=set()
                for g in grants:
                    req(closed(g,('namespace','policy')) and name(g['namespace']) and g['policy'] in POLICIES,'PROFILE_GRANT_INVALID')
                    key=(g['namespace'],g['policy']);req(key not in gs,'PROFILE_GRANT_DUPLICATE');gs.add(key)
                fields.append(dict(f,identifiers=sorted(copy.deepcopy(grants),key=lambda g:(g['namespace'],g['policy']))))
            bindings.append(dict(copy.deepcopy(b),object_ids=sorted(ids) if type(ids) is list else ids,fields=sorted(fields,key=lambda f:f['id'])))
        out.append(dict(p,bindings=sorted(bindings,key=canonical)))
    norm=copy.deepcopy(config);norm['field_profiles']=sorted(out,key=lambda p:p['id']);norm['sources']=sorted(norm['sources'],key=lambda s:s['system'])
    return norm

def binding_for(profile,m):
    return next((b for b in profile['bindings'] if b['system']==m['system'] and b['format']==m['format'] and (b['object_ids']=='*' or m['object_id'] in b['object_ids'])),None)

def target_valid(t,systems):
    req(type(t) is dict)
    if t.get('kind')=='all':req(closed(t,('kind',)));return {'kind':'all'}
    if t.get('kind')=='systems':
        req(closed(t,('kind','systems')) and type(t['systems']) is list and 1<=len(t['systems'])<=2)
        req(all(name(x) and x in systems for x in t['systems']) and len(set(t['systems']))==len(t['systems']))
        return dict(kind='systems',systems=sorted(t['systems']))
    req(t.get('kind')=='objects' and closed(t,('kind','objects')) and type(t['objects']) is list and 1<=len(t['objects'])<=32)
    for r in t['objects']:req(closed(r,('system','object_id')) and all(name(x) for x in r.values()) and r['system'] in systems)
    req(len({canonical(x) for x in t['objects']})==len(t['objects']))
    return dict(kind='objects',objects=sorted(copy.deepcopy(t['objects']),key=canonical))

def target_relevant(b,t):
    if t['kind']=='all':return True
    if t['kind']=='systems':return b['system'] in t['systems']
    return any(r['system']==b['system'] and (b['object_ids']=='*' or r['object_id'] in b['object_ids']) for r in t['objects'])

def normalize_identifier(value,policy):
    if policy=='scout.id.raw/1':return value
    value=unicodedata.normalize('NFKC',value).strip()
    return value.casefold() if policy=='scout.id.nfkc_trim_casefold/1' else value

def field_valid(f):
    req(type(f) is dict and f.get('value_type') in TYPES)
    if f.get('mode')=='declared':req(closed(f,('mode','id','value_type')) and name(f['id']))
    else:req(f.get('mode')=='source' and closed(f,('mode','selector','value_type')));selector_valid(f['selector'])

def compile_query(q,profiles,systems):
    req(type(q) is dict and {'schema','profile','combine','scope','clauses'}<=set(q)<= {'schema','profile','combine','scope','clauses','target'})
    req(q['schema']=='scout.query/4' and name(q['profile']) and q['profile'] in profiles and q['combine'] in ('and','or') and q['scope'] in ('unit','record','object'))
    t=target_valid(q.get('target',{'kind':'all'}),systems);p=profiles[q['profile']]
    clauses=q['clauses'];req(type(clauses) is list and 1<=len(clauses)<=8);unique={};osize=nsize=0
    for c in clauses:
        req(type(c) is dict and c.get('kind') in ('run','fragment','identifier'))
        kind=c['kind'];req(closed(c,('kind','field','value','namespace','policy') if kind=='identifier' else ('kind','field','value')))
        req(scalar_string(c['value']) and bool(c['value']));field_valid(c['field']);osize+=len(c['value']);req(osize<=256)
        if kind=='identifier':
            req(c['field']['value_type']=='string' and name(c['namespace']) and c['policy'] in POLICIES)
            matches=[]
            for b in p['bindings']:
                if not target_relevant(b,t):continue
                for f in b['fields']:
                    if (c['field']['mode']=='declared' and c['field']['id']==f['id']) or (c['field']['mode']=='source' and c['field']['selector']==f['selector']):matches.append(f)
            req(bool(matches),'IDENTIFIER_FIELD_NOT_DECLARED')
            req(all({'namespace':c['namespace'],'policy':c['policy']} in f['identifiers'] for f in matches),'IDENTIFIER_POLICY_NOT_DECLARED')
            normalized=normalize_identifier(c['value'],c['policy']);req(bool(normalized),'IDENTIFIER_EMPTY_NORMALIZED')
            key={k:c[k] for k in ('kind','field','namespace','policy')}
        else:
            normalized=normalize(c['value']);values,_=runs(c['value'],4096,'INVALID_REQUEST');req(values==[normalized]);key={k:c[k] for k in ('kind','field')}
        nsize+=len(normalized);req(nsize<=4096);key['normalized_value']=normalized;encoded=canonical(key)
        if encoded not in unique or c['value']<unique[encoded]['value']:unique[encoded]=dict(c,normalized=normalized)
    return dict(profile=p,target=t,scope=q['scope'],combine=q['combine'],clauses=[unique[k] for k in sorted(unique)])

def matches_path(pattern,actual):
    return len(pattern)==len(actual) and all(a==b or (a=={'each':'array'} and 'index' in b) for a,b in zip(pattern,actual))

def selector_matches(pattern,actual):
    if pattern['kind']!=actual['kind']:return False
    return matches_path(pattern['path'],actual['path']) if pattern['kind']=='json' else pattern==actual

def pointer(path):
    return ''.join('/'+(str(x['index']) if 'index' in x else x['key'].replace('~','~0').replace('/','~1')) for x in path)

def derive_view(m,raw,units,profile,source_hash):
    b=binding_for(profile,m)
    need(b is not None,'UNSUPPORTED','FIELD_PROFILE_UNDECLARED')
    paths={};nodes=[]
    if m['format']=='json':
        root=strict_json(raw.decode('utf-8'),number_exact=True)
        def walk(v,path):
            nodes.append((path,v));paths[pointer(path)]=path
            if type(v) is dict:
                for k,val in v.items():walk(val,path+[{'key':k}])
            elif type(v) is list:
                for i,val in enumerate(v):walk(val,path+[{'index':i}])
        walk(root,[])
    rec=b['record'];boundaries=[]
    if rec is not None and rec['kind']=='json':
        for path,v in nodes:
            if matches_path(rec['path'],path):
                need(type(v) in (dict,list),reason='RECORD_BOUNDARY_TYPE');boundaries.append(path)
                need(len(boundaries)<=2000,'LIMIT','RECORD_GROUP_LIMIT')
        for i,p in enumerate(boundaries):
            need(not any(p[:len(q)]==q or q[:len(p)]==p for q in boundaries[:i]),reason='RECORD_BOUNDARY_OVERLAP')
    out=[];records=set()
    for ordinal,u in enumerate(units):
        loc=u['locator'];record=None
        if m['format']=='csv':
            sel={'kind':'csv','header':loc['header']}
            if rec is not None:record={'kind':'csv','record':loc['record']}
        elif m['format']=='record':
            sel={'kind':'sqlite','column':SQL_COLUMNS[loc['pointer']]}
            if rec is not None:record={'kind':'sqlite','record_key':m['object_id']}
        else:
            actual=paths[loc['pointer']];sel={'kind':'json','path':actual}
            owners=[p for p in boundaries if actual[:len(p)]==p]
            if owners:record={'kind':'json','pointer':pointer(owners[0])}
        f=[f for f in b['fields'] if selector_matches(f['selector'],sel)]
        need(len(f)<=1,reason='FIELD_SELECTOR_OVERLAP')
        if record is not None:records.add(canonical(record));need(len(records)<=2000,'LIMIT','RECORD_GROUP_LIMIT')
        out.append(dict(ordinal=ordinal,source_selector=sel,value_type=u['value_type'],declared_field=f[0]['id'] if f else None,record_locator=record))
    return dict(identity=identity(m),source_sha256=source_hash,profile=profile['id'],profile_sha256=jd(profile),fields_version='scout.fields/1',grouping_version='scout.records/1',units=out)

def selected(field,u):
    if field['value_type']!='any' and field['value_type']!=u['value_type']:return False
    return field['id']==u['declared_field'] if field['mode']=='declared' else selector_matches(field['selector'],u['source_selector'])

def counts_for(unit,vu,plan,arm):
    values=None;out=[]
    for c in plan['clauses']:
        count=0
        if selected(c['field'],vu):
            if c['kind']=='identifier':count=int(bool(normalize_identifier(unit['text'],c['policy'])) and normalize_identifier(unit['text'],c['policy'])==c['normalized'])
            elif arm=='baseline':count=unit['text'].count(c['value'])
            else:
                if values is None:values,_=runs(unit['text'])
                count=values.count(c['normalized']) if c['kind']=='run' else sum(overlap_count(v,c['normalized']) for v in values)
        out.append(count)
    return out

def group_hits(units,view,plan,arm):
    if plan['scope']=='record':
        b=binding_for(plan['profile'],dict(view['identity'],format= {'csv':'csv','sqlite':'record','json':'json'}[view['units'][0]['source_selector']['kind']])) if view['units'] else None
        # Empty files still need an explicit record declaration; caller validates this too.
        if b is not None:need(b['record'] is not None,'UNSUPPORTED','RECORD_PROFILE_REQUIRED')
        need(not any(u['record_locator'] is None and any(selected(c['field'],u) for c in plan['clauses']) for u in view['units']),'UNSUPPORTED','RECORD_BOUNDARY_UNCOVERED')
    groups={}
    for vu in view['units']:
        u=units[vu['ordinal']];counts=counts_for(u,vu,plan,arm)
        if not any(counts):continue
        key=str(vu['ordinal']) if plan['scope']=='unit' else canonical(vu['record_locator']) if plan['scope']=='record' else ''
        g=groups.setdefault(key,dict(totals=[0]*len(counts),ordinals=[],predicates={},length=0,record=vu['record_locator'] if plan['scope']=='record' else None))
        g['totals']=[a+b for a,b in zip(g['totals'],counts)];g['ordinals'].append(vu['ordinal']);g['predicates'][vu['ordinal']]=[i for i,n in enumerate(counts) if n];g['length']+=len(u['text'])
    return [g for g in groups.values() if (all(g['totals']) if plan['combine']=='and' else any(g['totals']))]

def details(plan,groups,k):
    lookups=[]
    for i,c in enumerate(plan['clauses']):
        if c['kind']!='identifier':continue
        hits={}
        for g in groups:
            for e in g['evidence']:
                if i in e['matched_predicates']:hits[canonical([e['identity'],e['locator']])]=e['text']
        n=len(hits);raws=len(set(hits.values()))
        lookups.append(dict(predicate=i,namespace=c['namespace'],policy=c['policy'],normalized_query=c['normalized'],matched_units=n,distinct_raw_values=raws,multiple_matches=n>1,collision=raws>1,identity_asserted=False))
    return dict(profile=plan['profile']['id'],profile_sha256=jd(plan['profile']),target=plan['target'],total_matches=len(groups),returned_matches=min(k,len(groups)),truncated=len(groups)>k,identity_asserted=False,identifier_lookups=lookups)
