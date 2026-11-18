"""Explicit single-record literal labels; pure derivation, no I/O or inference."""
from __future__ import annotations
import re, copy
from .sources import Fault, need, ident, jd, canonical
from .parsers import normal_field, field_type
from . import task_logic as tasks
SCHEMA='scout.text-fields/1'
WIRE='scout.text-fields.wire/2'
ALIASES={
 'organization':('organization','organization_name','org_name','company_name','组织','组织名称','公司名称'),
 'email':('email','contact_email','邮箱','联系邮箱'),'external_ref':('external_ref','外部编号'),
 'invoice_number':('invoice_number','invoice_id','发票编号'),'amount':('amount','total_amount','金额'),
 'currency':('currency','币种'),'document_type':('document_type','record_type','文档类型'),'description':('description','描述')}
LOOKUP={normal_field(a):k for k,vs in ALIASES.items() for a in vs}
def validate(config):
    p=config.get('text_field_profile')
    if p is None:return None
    req=lambda c:need(c,'INVALID_REQUEST','TEXT_FIELD_PROFILE_INVALID')
    req(type(p) is dict and set(p)=={'schema','bindings'} and p['schema']==SCHEMA)
    req(type(p['bindings']) is list and len(p['bindings'])<=32)
    kinds={s['system']:s['kind'] for s in config['sources']};seen=set();bindings=[]
    for b in p['bindings']:
        req(type(b) is dict and set(b)=={'system','format','object_ids','record'})
        req(type(b['system']) is str and b['system'] in kinds and kinds[b['system']]=='document' and type(b['format']) is str and b['format'] in ('txt','docx','pptx','xlsx','png','pdf') and b['record']=='single_object')
        ids=b['object_ids'];req(type(ids) is list and 1<=len(ids)<=1000 and all(ident(x) for x in ids) and len(set(ids))==len(ids))
        for id in ids:
            if b['format'] in ('png','pdf'):
                obs=config.get('synthetic_observations')
                req(obs is not None and any(e['source_identity']['tenant']==config['tenant'] and e['source_identity']['system']==b['system'] and e['source_identity']['object_id']==id for e in obs['entries']))
                req((b['format']=='pdf')==(obs['profile']=='scout.synthetic-pdfkit-stored-text/1'))
            key=(b['system'],b['format'],id);req(key not in seen);seen.add(key)
        bindings.append(dict(b,object_ids=sorted(ids)))
    return {'schema':SCHEMA,'bindings':sorted(bindings,key=canonical)}
def descriptor(profile):
    return {'schema':SCHEMA,'wire_revision':WIRE,'enabled':profile is not None,'profile_sha256':jd(profile),'value_semantics':'source_observation_not_verified_fact','query_v4_derived_fields':False}
def _record(u,fmt):
    loc=u['locator']
    if fmt=='csv':return {'kind':'csv','record':loc['record']}
    if fmt=='json':return {'kind':'json_direct_parent','pointer':loc['pointer'].rsplit('/',1)[0]}
    return {'kind':'sqlite_single_record'}
def derive(units,m,profile):
    """Returns task-only units; underlying indexed units/evidence remain original."""
    groups={};notes=[]
    def put(u,key):
        encoded=canonical(key);g=groups.setdefault(encoded,{'record_locator':key,'units':[],'conflicts':[]})
        g['units'].append(u)
    if m['format'] in ('csv','json','record'):
        for original in units:
            key=_record(original,m['format'])
            f=(LOOKUP.get(normal_field(original['field']))
               if original['field'] is not None and
               (not profile.get('_legacy_classification_aliases') or field_type(original['field']) is not None)
               else None)
            u=dict(original,field=f or original['field'],_source_unit=original,_record_binding={'schema':SCHEMA,'wire_revision':WIRE,'profile_sha256':jd(profile),'record_locator':key})
            put(u,key)
    elif any(b['system']==m['system'] and b['format']==m['format'] and m['object_id'] in b['object_ids'] for b in profile['bindings']):
        key={'kind':'declared_single_object','object_id':m['object_id']}
        groups[canonical(key)]={'record_locator':key,'units':[],'conflicts':[]}
        count=0
        for original in units:
            offset=0
            for line in re.findall(r'[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$',original['text']):
                body=line.rstrip('\r\n');match=re.fullmatch(r'([ \t]*)([^:\uff1a\r\n]+?)([ \t]*)[:\uff1a]([ \t]*)(.*?)([ \t]*)',body)
                if match:
                    field=LOOKUP.get(normal_field(match.group(2)))
                    if field and match.group(5):
                        start=offset+match.start(5);end=offset+match.end(5)
                        bind={'schema':SCHEMA,'wire_revision':WIRE,'profile_sha256':jd(profile),'canonical_field':field,'record_locator':key,'source_unit_id':original['unit_id'],'label_start':offset+match.start(2),'label_end':offset+match.end(2),'value_start':start,'value_end':end,'label':match.group(2),'value':original['text'][start:end]}
                        u=dict(original,text=original['text'][start:end],field=field,value_type='string',_source_unit=original,_field_derivation=bind)
                        put(u,key);count+=1;need(count<=2000,'LIMIT','DERIVED_FIELD_LIMIT')
                offset+=len(line)
    else:notes.append('TEXT_RECORD_NOT_DECLARED')
    for g in groups.values():
        counts={}
        for u in g['units']:
            f=field_type(u['field']) if u['field'] is not None else None
            if f:counts[f]=counts.get(f,0)+1
        g['conflicts']=sorted(k for k,n in counts.items() if n>1)
    return list(groups.values()),notes

def _trusted_evidence(base,u):
    e=base(u.get('_source_unit',u))
    if '_field_derivation' in u:e=dict(e,field_derivation=u['_field_derivation'])
    if '_record_binding' in u:e=dict(e,record_binding=u['_record_binding'])
    return e

def evidence(base,u):
    return _trusted_evidence(base,u)

def _render_recognition(units,groups,base,arm,projector=None):
    projector=evidence if projector is None else projector
    native=[u for g in groups for u in g['units'] if '_record_binding' in u]
    result=tasks.recognize(native,lambda u:projector(base,u),arm) if native else tasks.recognize(units,base,arm)
    if arm=='baseline':return result
    for g in groups:
        for u in g['units']:
            if '_field_derivation' not in u:continue
            b=u['_field_derivation'];result.append(dict(kind='derived_field',label=tasks.LABELS[b['canonical_field']],canonical_field=b['canonical_field'],value=u['text'],start=b['value_start'],end=b['value_end'],record_locator=g['record_locator'],field_status='AMBIGUOUS' if b['canonical_field'] in g['conflicts'] else 'OBSERVED',evidence=[projector(base,u)]))
    return result

def _render_classification(groups,base,arm,projector=None):
    projector=evidence if projector is None else projector
    labels=set();records=[];all_evidence=[];field_labels=[];ambiguous=False
    for g in groups:
        status,rs=tasks.classify(g['units'],lambda u:projector(base,u),arm)
        record=rs[0];record=dict(record,record_locator=g['record_locator'],conflicting_fields=g['conflicts'])
        if g['conflicts']:record['labels']=[];status='AMBIGUOUS';ambiguous=True
        labels.update(record['labels']);record['status']=status
        ev=record.pop('evidence');record['evidence_indices']=list(range(len(all_evidence),len(all_evidence)+len(ev)))
        records.append(record);all_evidence.extend(ev);field_labels.extend(record['field_labels'])
    status='AMBIGUOUS' if ambiguous or len(labels)>1 else 'OK' if labels else 'UNCLASSIFIED'
    result={'labels':sorted(labels),'field_labels':field_labels,'evidence':all_evidence,'record_groups':records}
    return status,[result]

def _render_entity(groups):
    units=[u for g in groups for u in g['units']];out=tasks.entity(units)
    out['ambiguous']=out['ambiguous'] or len(groups)>1 or any(g['conflicts'] for g in groups)
    return out


def _validate_classification(result,status):
    """Check the self-contained evidence references before returning task output."""
    check=lambda ok:need(ok,'INVALID_SOURCE','TEXT_FIELD_PROJECTION_INVALID')
    check(type(result) is dict and set(result)=={'labels','field_labels','evidence','record_groups'})
    ev=result['evidence'];groups=result['record_groups'];check(type(ev) is list and type(groups) is list)
    indices=[];labels=set();fields=[];conflict=False
    for g in groups:
        check(type(g) is dict and set(g)=={'record_locator','conflicting_fields','status','labels','field_labels','evidence_indices'})
        ids=g['evidence_indices'];check(type(ids) is list and all(type(i) is int and 0<=i<len(ev) for i in ids))
        check(len(ids)==len(g['field_labels']) and len(ids)==len(set(ids)) and ids==sorted(ids))
        for i,f in zip(ids,g['field_labels']):
            check(ev[i]['unit_id']==f['unit_id'])
            b=ev[i].get('field_derivation',ev[i].get('record_binding'))
            check(type(b) is dict and b['schema']==SCHEMA and b['wire_revision']==WIRE and b['record_locator']==g['record_locator'])
        check(type(g['conflicting_fields']) is list and g['labels']==sorted(set(g['labels'])))
        if g['conflicting_fields']:check(g['status']=='AMBIGUOUS' and not g['labels']);conflict=True
        else:check(g['status']==('UNCLASSIFIED' if not g['labels'] else 'AMBIGUOUS' if len(g['labels'])>1 else 'OK'))
        indices.extend(ids);labels.update(g['labels']);fields.extend(g['field_labels'])
    check(indices==list(range(len(ev))))
    check(result['labels']==sorted(labels) and result['field_labels']==fields)
    check(status==('AMBIGUOUS' if conflict or len(labels)>1 else 'OK' if labels else 'UNCLASSIFIED'))



def _same(actual,expected):
    try:need(canonical(actual)==canonical(expected),'INVALID_SOURCE','TEXT_FIELD_PROJECTION_INVALID')
    except Fault:raise
    except (TypeError,ValueError,UnicodeError):raise Fault('INVALID_SOURCE','TEXT_FIELD_PROJECTION_INVALID') from None

def _context(units,m,profile,base,arm='candidate'):
    need(type(units) is list and type(m) is dict and type(profile) is dict and callable(base) and arm in ('candidate','baseline'),'INVALID_SOURCE','TEXT_FIELD_TRUSTED_CONTEXT_REQUIRED')
    # No producer/output references can mutate the trusted observation snapshot.
    return (copy.deepcopy(units),copy.deepcopy(m),copy.deepcopy(profile),base,arm)

def _fresh(ctx):
    units,m,profile,base,arm=ctx
    fresh=copy.deepcopy(units)
    groups,notes=derive(fresh,copy.deepcopy(m),copy.deepcopy(profile))
    return fresh,groups,notes,base,arm

def validate_classification(result,status,*,units=None,m=None,profile=None,base=None,arm='candidate'):
    ctx=_context(units,m,profile,base,arm)
    try:_validate_classification(result,status)
    except Fault:raise
    except (KeyError,TypeError,ValueError,AttributeError):raise Fault('INVALID_SOURCE','TEXT_FIELD_PROJECTION_INVALID') from None
    fresh,groups,notes,base,arm=_fresh(ctx)
    expected_status,expected=_render_classification(groups,base,arm,projector=_trusted_evidence)
    _same({'status':status,'result':result},{'status':expected_status,'result':expected[0]})

def validate_recognition(result,*,units=None,m=None,profile=None,base=None,arm='candidate'):
    ctx=_context(units,m,profile,base,arm)
    fresh,groups,notes,base,arm=_fresh(ctx)
    _same(result,_render_recognition(fresh,groups,base,arm,projector=_trusted_evidence))

def _validate_entity(result,ctx):
    fresh,groups,notes,base,arm=_fresh(ctx);expected=_render_entity(groups)
    need(type(result) is dict and set(result)==set(expected),'INVALID_SOURCE','TEXT_FIELD_PROJECTION_INVALID')
    for key in ('ref_values','raw_names'):
        need(type(result[key]) is set and all(type(x) is str for x in result[key]) and result[key]==expected[key],'INVALID_SOURCE','TEXT_FIELD_PROJECTION_INVALID')
    _same({k:v for k,v in result.items() if k not in ('ref_values','raw_names')},{k:v for k,v in expected.items() if k not in ('ref_values','raw_names')})

def validate_entity(result,*,units=None,m=None,profile=None,base=None,arm='candidate'):
    _validate_entity(result,_context(units,m,profile,base,arm))

def checked_recognize(units,m,profile,base,arm):
    ctx=_context(units,m,profile,base,arm);fresh,groups,notes,base,arm=_fresh(ctx)
    result=_render_recognition(fresh,groups,base,arm)
    validate_recognition(result,units=ctx[0],m=ctx[1],profile=ctx[2],base=base,arm=arm)
    return result,notes

def checked_classify(units,m,profile,base,arm):
    ctx=_context(units,m,profile,base,arm);fresh,groups,notes,base,arm=_fresh(ctx)
    status,result=_render_classification(groups,base,arm)
    validate_classification(result[0],status,units=ctx[0],m=ctx[1],profile=ctx[2],base=base,arm=arm)
    return status,result,notes

def checked_entity(units,m,profile,base):
    ctx=_context(units,m,profile,base);fresh,groups,notes,base,arm=_fresh(ctx)
    result=_render_entity(groups);_validate_entity(result,ctx)
    # Every emitted matching witness is checked against trusted rederived units,
    # not a field_derivation/record_binding supplied by the result being checked.
    trusted_fresh,trusted_groups,_,_,_=_fresh(ctx)
    permitted={canonical(u):copy.deepcopy(u) for g in trusted_groups for u in g['units']}
    def emit(u):
        key=canonical(u);need(key in permitted,'INVALID_SOURCE','TEXT_FIELD_PROJECTION_INVALID')
        projected=evidence(base,u)
        expected=_trusted_evidence(base,copy.deepcopy(permitted[key]))
        _same(projected,expected)
        return projected
    return result,emit
