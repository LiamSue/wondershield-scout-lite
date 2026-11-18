"""Scout Lite research API. Synthetic sources, local SQLite postings, no model."""
from __future__ import annotations
import argparse, collections, json, os, sqlite3, stat, sys, time
from pathlib import Path
from .parsers import parse
from .lexical import (DERIVATION, compile_query, postings, candidate_keys, score as lexical_score,
                     typed_counts, accepted, counts_score)
from .sources import (Fault, need, ident, canonical, jd, digest, strict_json, make_source,
                     identity, slot, signature, allowed, directory_fd)
from .task_logic import recognize, classify, entity
from . import field_queries as fq
from . import text_fields as tf
from . import observation_adapter as oa
SCHEMA='scout.lite/1'
OUTPUT_LIMIT=131072
EXACT_EVIDENCE_LIMIT=64  # request-wide; exceeding this yields no partial evidence
INDEX_LIMIT=8*1024*1024
XLSX_GAPS=('XLSX_DRAWINGS_NOT_INSPECTED','XLSX_CHARTS_NOT_INSPECTED','XLSX_CHARTSHEETS_NOT_PROCESSED','XLSX_FORMULAS_NOT_EVALUATED',
           'XLSX_FORMATS_AND_DATES_NOT_INTERPRETED','XLSX_OTHER_NONCELL_CONTENT_NOT_INSPECTED')

class Engine:
    def __init__(self,config):
        need(type(config) is dict and {'schema','tenant','principal','index','sources'}<=set(config)<={'schema','tenant','principal','index','sources','field_profiles','ppt_interpretation','word_interpretation','text_field_profile','synthetic_observations'},'INVALID_REQUEST','CONFIG_INVALID')
        need(config['schema']==SCHEMA and ident(config['tenant']) and ident(config['principal']),'INVALID_REQUEST','CONFIG_INVALID')
        specs=config['sources']
        need(type(specs) is list and len(specs)==2 and all(type(s) is dict and set(s)=={'system','kind','path'} for s in specs),'INVALID_REQUEST','SOURCE_CONFIG_INVALID')
        need({s['kind'] for s in specs}=={'document','sqlite'} and len({s['system'] for s in specs})==2,'INVALID_REQUEST','SOURCE_CONFIG_INVALID')
        for s in specs:
            need(ident(s['system']) and type(s['path']) is str and Path(s['path']).is_absolute(),'INVALID_REQUEST','SOURCE_CONFIG_INVALID')
        self.source_kinds={s['system']:s['kind'] for s in specs}
        config=fq.validate_config(config); specs=config['sources']
        self.observation_profile=oa.validate_config(config)
        config=dict(config,synthetic_observations=self.observation_profile)
        self.text_field_profile=tf.validate(config)
        config=dict(config,text_field_profile=self.text_field_profile)
        self.text_field_descriptor=tf.descriptor(self.text_field_profile)
        self.interpretation=config.get('ppt_interpretation','generic')
        need(self.interpretation=='generic','INVALID_REQUEST','INTERPRETATION_UNSUPPORTED_IN_DIAGNOSTIC')
        self.interpretation_descriptor={'profile':'internal-structured-no-office/1','format':'pptx','domain':'unavailable','status':'UNSUPPORTED','reason':'OFFICE_NOT_INCLUDED_IN_DIAGNOSTIC','rendered':False}
        self.word_interpretation=config.get('word_interpretation','generic')
        need(self.word_interpretation=='generic','INVALID_REQUEST','WORD_INTERPRETATION_UNSUPPORTED_IN_DIAGNOSTIC')
        self.word_descriptor={'profile':'internal-structured-no-office/1','format':'docx','domain':'unavailable','status':'UNSUPPORTED','reason':'OFFICE_NOT_INCLUDED_IN_DIAGNOSTIC','rendered':False}
        runtime_root=Path(__file__).resolve().parent
        runtime_names=('__init__.py','__main__.py','field_queries.py','lexical.py','observation_adapter.py','parsers.py','scout_lite.py','sources.py','task_contract.py','task_logic.py','text_fields.py','xlsx_cells.py')
        self.runtime_files_sha=jd({'capability':'internal-structured-and-xlsx-basic/1','files':{name:digest((runtime_root/name).read_bytes()) for name in runtime_names}})
        self.interpretation_descriptor=dict(self.interpretation_descriptor,runtime_files_sha256=self.runtime_files_sha)
        self.word_descriptor=dict(self.word_descriptor,runtime_files_sha256=self.runtime_files_sha)
        config=dict(config,ppt_interpretation=self.interpretation,word_interpretation=self.word_interpretation,text_field_profile=self.text_field_profile)
        self.profiles={p['id']:p for p in config['field_profiles']}
        self.config=config; self.tenant=config['tenant']; self.principal=config['principal']; self._test_hooks={}
        self.sources={s['system']:make_source(s) for s in specs}
        self.path=Path(config['index'])
        need(self.path.is_absolute() and '..' not in self.path.parts,'INVALID_REQUEST','INDEX_PATH_INVALID')
        for spec in specs:
            source_path=Path(spec['path'])
            need('..' not in source_path.parts,'INVALID_REQUEST','SOURCE_PATH_INVALID')
            need(not (spec['kind']=='document' and self.path.is_relative_to(source_path)) and self.path!=source_path,'DENIED','INDEX_SOURCE_ALIAS')
            if self.path.exists() and source_path.exists() and spec['kind']=='sqlite':
                a=self.path.lstat();b=source_path.lstat()
                need((a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino),'DENIED','INDEX_SOURCE_ALIAS')
        # The harness supplies an existing private directory. Never create parent directories or follow links.
        fd=directory_fd(self.path.parent)
        try:
            f=os.open(self.path.name,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=fd)
            try:
                st=os.fstat(f); need(stat.S_ISREG(st.st_mode) and st.st_nlink==1 and st.st_uid==os.getuid() and not stat.S_IMODE(st.st_mode)&0o077,'DENIED','INDEX_NOT_PRIVATE')
            finally: os.close(f)
        finally: os.close(fd)
        self.db=sqlite3.connect(str(self.path))
        self.db.row_factory=sqlite3.Row
        # Refuse incompatible derived state before any DDL/postings access or writes.
        binding=jd({'config':{k:config[k] for k in ('schema','tenant','principal','sources','field_profiles','ppt_interpretation','word_interpretation','text_field_profile','synthetic_observations')},'runtime_files_sha256':self.runtime_files_sha})
        derivation=canonical(DERIVATION)
        try:
            self.db.execute('PRAGMA trusted_schema=OFF')
            tables=self.db.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            fresh=not tables
            if not fresh:
                need(any(r['name']=='binding' and r['type']=='table' for r in tables),'INVALID_SOURCE','INDEX_DERIVATION_MISMATCH')
                cols={r[1] for r in self.db.execute('PRAGMA table_info(binding)')}
                need({'id','config_sha'}<=cols,'INVALID_SOURCE','INDEX_BINDING_INVALID')
                old=self.db.execute('SELECT id,config_sha FROM binding').fetchall()
                need(len(old)==1 and old[0]['id']==1,'INVALID_SOURCE','INDEX_BINDING_INVALID')
                need(cols=={'id','config_sha','derivation_json'},'INVALID_SOURCE','INDEX_DERIVATION_MISMATCH')
                value=self.db.execute('SELECT derivation_json FROM binding WHERE id=1').fetchone()[0]
                need(value==derivation,'INVALID_SOURCE','INDEX_DERIVATION_MISMATCH')
                need(old[0]['config_sha']==binding,'DENIED','INDEX_PRINCIPAL_OR_CONFIG_MISMATCH')
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA max_page_count='+str(INDEX_LIMIT//self.db.execute('PRAGMA page_size').fetchone()[0]))
            if fresh:
                self.db.executescript(''' 
                  CREATE TABLE binding (id INTEGER PRIMARY KEY CHECK(id=1), config_sha TEXT NOT NULL, derivation_json TEXT NOT NULL);
                  CREATE TABLE objects (slot TEXT PRIMARY KEY, system TEXT NOT NULL, signature TEXT NOT NULL, identity_json TEXT NOT NULL, source_sha TEXT, status TEXT NOT NULL, reason TEXT NOT NULL);
                  CREATE TABLE field_views (slot TEXT NOT NULL, profile TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, view_json TEXT, PRIMARY KEY(slot,profile));
                  CREATE TABLE units (slot TEXT NOT NULL, ordinal INTEGER NOT NULL, body TEXT NOT NULL, unit_json TEXT NOT NULL, PRIMARY KEY(slot,ordinal));
                  CREATE TABLE terms (slot TEXT NOT NULL, ordinal INTEGER NOT NULL, term TEXT NOT NULL, frequency INTEGER NOT NULL, PRIMARY KEY(slot,ordinal,term));
                  CREATE INDEX term_lookup ON terms(slot,term);
                ''')
                self.db.execute('INSERT INTO binding VALUES(1,?,?)',(binding,derivation));self.db.commit()
        except BaseException:
            self.db.close();raise
    def set_test_hook(self,stage,callback):
        """Trusted in-process synthetic-test seam, never a request/config capability."""
        need(stage=='before_final_recheck' and (callback is None or callable(callback)), 'INVALID_REQUEST','TEST_HOOK_INVALID')
        if callback is None:self._test_hooks.pop(stage,None)
        else:self._test_hooks[stage]=callback
    def close(self): self.db.close()
    def _snapshot(self):
        snaps={}; faults={}
        for system,source in self.sources.items():
            try: snaps[system]=source.snapshot()
            except Fault as f:faults[system]=f
            except (ValueError,UnicodeError,TypeError,KeyError):faults[system]=Fault('INVALID_SOURCE','SOURCE_SCHEMA_INVALID')
        return snaps,faults
    def _metas(self,snaps):
        return {slot(m):m for s in snaps.values() for m in s['metas'] if allowed(m,self.tenant,self.principal)}
    def _remove(self,key):
        for table in ('field_views','terms','units','objects'): self.db.execute('DELETE FROM '+table+' WHERE slot=?',(key,))
    def _capture_units(self,m):
        # Current object authorization and raw-source identity precede ANY sidecar read.
        if m['format'] in ('docx','pptx'):
            raise Fault('UNSUPPORTED','OFFICE_NOT_INCLUDED_IN_DIAGNOSTIC')
        kind=self.source_kinds.get(m['system'])
        if not ((kind=='document' and m['format'] in ('csv','json','xlsx')) or (kind=='sqlite' and m['format']=='record')):
            raise Fault('UNSUPPORTED','FORMAT_NOT_INCLUDED_IN_INTERNAL_CANDIDATE')
        raw=self.sources[m['system']].capture(m,self.tenant,self.principal)
        if oa.configured(self.observation_profile,m):
            units=oa.units(raw,m,self.observation_profile)
        else:
            units=parse(raw,m['format'],interpretation=self.interpretation,word_interpretation=self.word_interpretation,text_field_profile=self.text_field_profile)
        return raw,units
    def sync(self):
        """Trusted harness maintenance summary, not a remote administration endpoint."""
        started=time.perf_counter(); snaps,faults=self._snapshot(); metas=self._metas(snaps)
        before=self.path.stat().st_size; parsed=0; removed=0; reused=0; statuses=collections.Counter()
        try:
            self.db.execute('BEGIN')
            # Revoked/deleted/unavailable source records are removed, not offered from cache.
            live={k:m for k,m in metas.items() if not m['deleted']}
            for row in self.db.execute('SELECT slot FROM objects').fetchall():
                if row['slot'] not in live:self._remove(row['slot']);removed+=1
            for key,m in sorted(live.items()):
                sig=signature(m);old=self.db.execute('SELECT * FROM objects WHERE slot=?',(key,)).fetchone()
                if old and old['signature']==sig and old['status']=='OK' and not oa.configured(self.observation_profile,m):
                    reused+=1;statuses['PARTIAL' if m['format']=='xlsx' else 'OK']+=1;continue
                self.db.execute('SAVEPOINT object_update')
                try:
                    raw,units=self._capture_units(m); source_hash=digest(raw)
                    self._remove(key)
                    self.db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)',(key,m['system'],sig,canonical(identity(m)),source_hash,'OK','SUPPORTED_UNIT_SCOPE'))
                    for i,(u,grams) in enumerate(zip(units,postings(units))):
                        self.db.execute('INSERT INTO units VALUES(?,?,?,?)',(key,i,u['text'],canonical(u)))
                        for term,count in grams.items():
                            self.db.execute('INSERT INTO terms VALUES(?,?,?,?)',(key,i,term,count))
                    for profile in self.profiles.values():
                        try:
                            view=fq.derive_view(m,raw,units,profile,source_hash)
                            self.db.execute('INSERT INTO field_views VALUES(?,?,?,?,?)',(key,profile['id'],'OK','DECLARED_PROFILE_VIEW',canonical(view)))
                        except Fault as vf:
                            self.db.execute('INSERT INTO field_views VALUES(?,?,?,?,?)',(key,profile['id'],vf.status,vf.reason,None))
                    parsed+=1
                    statuses['PARTIAL' if m['format']=='xlsx' else 'OK']+=1
                except Fault as f:
                    self._remove(key)
                    self.db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)',(key,m['system'],sig,canonical(identity(m)),None,f.status,f.reason));statuses[f.status]+=1
                self.db.execute('RELEASE object_update')
            self.db.commit()
        except sqlite3.DatabaseError:
            self.db.rollback()
            return {'word_effective_interpretation':self.word_descriptor,'effective_interpretation':self.interpretation_descriptor,'schema':SCHEMA,'status':'LIMIT','reason':'INDEX_LIMIT_OR_WRITE_FAILED','parsed':0,'removed':0,'reused':0,'statuses':{},'source_errors':[],'wall_ms':(time.perf_counter()-started)*1000,'file_growth_bytes':self.path.stat().st_size-before}
        return {'word_effective_interpretation':self.word_descriptor,'effective_interpretation':self.interpretation_descriptor,'schema':SCHEMA,'status':'INCOMPLETE' if faults or any(k!='OK' for k in statuses) else 'OK','reason':'SYNC_CURRENT_AUTHORIZED_SCOPE','parsed':parsed,'removed':removed,'reused':reused,'statuses':dict(statuses),'source_errors':[{'system':s,'status':f.status,'reason':f.reason} for s,f in faults.items()],'wall_ms':(time.perf_counter()-started)*1000,'file_growth_bytes':self.path.stat().st_size-before}
    def _row(self,m):
        if m['deleted']:raise Fault('NOT_AVAILABLE','OBJECT_NOT_AVAILABLE')
        # Authorization was checked before this index lookup.
        row=self.db.execute('SELECT * FROM objects WHERE slot=?',(slot(m),)).fetchone()
        if row is None or row['signature']!=signature(m):raise Fault('STALE','INDEX_NOT_CURRENT')
        if row['status']!='OK':raise Fault(row['status'],row['reason'])
        return row
    def _verified(self,m):
        row=self._row(m);raw,fresh=self._capture_units(m)
        need(digest(raw)==row['source_sha'],'STALE','SOURCE_DIGEST_CHANGED')
        indexed=[strict_json(r[0]) for r in self.db.execute('SELECT unit_json FROM units WHERE slot=? ORDER BY ordinal',(slot(m),))]
        need(indexed==fresh,'STALE','INDEX_UNIT_PROVENANCE_MISMATCH')
        return fresh,row['source_sha']
    def _evidence(self,m,source_hash,u):
        return oa.annotate_evidence(dict(identity=identity(m),source_sha256=source_hash,unit_id=u['unit_id'],locator=u['locator'],text=u['text']),u)
    @staticmethod
    def _coverage():
        return dict(scope='authorized_current_objects',authorized_objects=0,indexed_current_objects=0,gap_count=0,gaps=[],complete=False,consistency='catalog_recheck_and_returned_units')
    def _response(self,op,arm,status='OK',reason='SUPPORTED_TASK'):
        out=dict(text_field_interpretation=dict(self.text_field_descriptor,runtime_files_sha256=self.runtime_files_sha),word_effective_interpretation=self.word_descriptor,effective_interpretation=self.interpretation_descriptor,schema=SCHEMA,protocol_revision='4.0',operation=op,arm=arm,status=status,reason=reason,results=[],coverage=self._coverage(),absence_proven=False,complete_scope=False)
        if self.observation_profile is not None:out['observation_interpretation']=oa.descriptor(self.observation_profile,self.runtime_files_sha)
        return out
    def _validate_request(self,r,arm):
        need(arm in ('candidate','baseline') and type(r) is dict and type(r.get('op')) is str,'INVALID_REQUEST','REQUEST_INVALID')
        op=r['op']
        keys={'recognize':{'op','object'},'classify':{'op','object'},'inspect':{'op','object'},'search':{'op','query','k'},'match':{'op','left','right_system'},'exact':{'op','objects','needle'}}
        need(op in keys,'INVALID_REQUEST','REQUEST_INVALID')
        optional={'evidence_mode'} | ({'all_evidence'} if op=='exact' else set())
        need(keys[op] <= set(r) <= keys[op] | optional,'INVALID_REQUEST','REQUEST_INVALID')
        if 'all_evidence' in r:need(type(r['all_evidence']) is bool,'INVALID_REQUEST','ALL_EVIDENCE_INVALID')
        if op=='inspect':need('evidence_mode' not in r,'INVALID_REQUEST','INSPECT_OBSERVATION_MODE_FORBIDDEN')
        if 'evidence_mode' in r:
            need(r['evidence_mode']==oa.MODE and self.observation_profile is not None,'INVALID_REQUEST','OBSERVATION_MODE_NOT_ADMITTED')
            need(not(op=='search' and type(r.get('query')) is dict and r['query'].get('schema')=='scout.query/4'),'INVALID_REQUEST','OBSERVATION_QUERY4_NOT_SUPPORTED')
        def ref(v):need(type(v) is dict and set(v)=={'system','object_id'} and all(ident(x) for x in v.values()) and v['system'] in self.sources,'INVALID_REQUEST','OBJECT_REFERENCE_INVALID')
        if op in ('recognize','classify','inspect'):ref(r['object'])
        elif op=='match':
            ref(r['left']);need(r['right_system'] in self.sources and r['right_system']!=r['left']['system'],'INVALID_REQUEST','MATCH_SYSTEM_INVALID')
        elif op=='search':
            need(type(r['k']) is int and 1<=r['k']<=20,'INVALID_REQUEST','QUERY_INVALID')
            self._query_plan(r['query'])
        else:
            need(type(r['objects']) is list and 1<=len(r['objects'])<=32 and type(r['needle']) is str and 0<len(r['needle'])<=256,'INVALID_REQUEST','EXACT_SCOPE_INVALID')
            for v in r['objects']:ref(v)
            need(len({canonical(v) for v in r['objects']})==len(r['objects']),'INVALID_REQUEST','DUPLICATE_SCOPE')
        return op
    def _query_plan(self,query):
        if type(query) is dict and query.get('schema')=='scout.query/4':
            return fq.compile_query(query,self.profiles,self.sources)
        return compile_query(query)
    def _verified_profile(self,m,profile):
        row=self._row(m); raw,fresh=self._capture_units(m)
        source_hash=digest(raw); need(source_hash==row['source_sha'],'STALE','SOURCE_DIGEST_CHANGED')
        indexed=[strict_json(r[0]) for r in self.db.execute('SELECT unit_json FROM units WHERE slot=? ORDER BY ordinal',(slot(m),))]
        need(indexed==fresh,'STALE','INDEX_UNIT_PROVENANCE_MISMATCH')
        view=fq.derive_view(m,raw,fresh,profile,source_hash)
        stored=self.db.execute('SELECT status,view_json FROM field_views WHERE slot=? AND profile=?',(slot(m),profile['id'])).fetchone()
        need(stored is not None and stored['status']=='OK' and strict_json(stored['view_json'])==view,'STALE','INDEX_FIELD_PROVENANCE_MISMATCH')
        return fresh,view,source_hash
    def _field_search(self,plan,scope,good,arm,gap):
        ranked=[]
        for key,m in scope.items():
            if key not in good:continue
            try:
                row=self.db.execute('SELECT * FROM field_views WHERE slot=? AND profile=?',(key,plan['profile']['id'])).fetchone()
                need(row is not None,'STALE','INDEX_PROFILE_VIEW_MISSING')
                need(row['status']=='OK',row['status'],row['reason'])
                view=strict_json(row['view_json'])
                if plan['scope']=='record':
                    b=fq.binding_for(plan['profile'],m)
                    need(b is not None and b['record'] is not None,'UNSUPPORTED','RECORD_PROFILE_REQUIRED')
                units=[strict_json(r[0]) for r in self.db.execute('SELECT unit_json FROM units WHERE slot=? ORDER BY ordinal',(key,))]
                hits=fq.group_hits(units,view,plan,arm)
                if not hits:continue
                units,view,source_hash=self._verified_profile(m,plan['profile'])
                hits=fq.group_hits(units,view,plan,arm)
                for g in hits:
                    score=counts_score(g['totals']); evidence=[]
                    for ordinal in g['ordinals']:
                        vu=view['units'][ordinal];e=self._evidence(m,source_hash,units[ordinal])
                        e['field_binding']={k:vu[k] for k in ('source_selector','value_type','declared_field','record_locator')}
                        e['field_binding'].update(profile=plan['profile']['id'],profile_sha256=view['profile_sha256'],fields_version='scout.fields/1')
                        e['matched_predicates']=g['predicates'][ordinal];evidence.append(e)
                    record=None
                    if plan['scope']=='record':
                        record=dict(identity=identity(m),source_sha256=source_hash,locator=g['record'],profile=plan['profile']['id'],profile_sha256=view['profile_sha256'],grouping_version='scout.records/1')
                    result=dict(score=score,evidence=evidence,record=record)
                    rank=(-score['matched_terms'],-score['term_occurrences'],g['length'],key)
                    if plan['scope']=='unit':rank+=(g['ordinals'][0],)
                    elif plan['scope']=='record':rank+=(canonical(g['record']),)
                    ranked.append((rank,result))
            except Fault as f:
                if f.status=='DENIED' and plan['target']['kind']=='objects':raise
                good.pop(key,None);gap(m['system'],f)
        return ranked
    def run(self,request,arm='candidate'):
        op=request.get('op','invalid') if type(request) is dict else 'invalid'
        if type(op) is not str or op not in ('recognize','classify','inspect','search','match','exact'):op='invalid'
        response=self._response(op,arm if arm in ('candidate','baseline') else 'candidate')
        try:
            self._validate_request(request,arm)
            response=self._run_valid(request,arm)
        except Fault as f:response.update(status=f.status,reason=f.reason,results=[],absence_proven=False,complete_scope=False)
        except (KeyError,TypeError,ValueError,sqlite3.DatabaseError):response.update(status='INVALID_REQUEST',reason='REQUEST_OR_INDEX_INVALID',results=[],absence_proven=False,complete_scope=False)
        # One egress invariant covers early, capture, post-index and final DENIEDs.
        if response['status']=='DENIED':
            response=self._response(op,response['arm'],'DENIED',response['reason'])
        if len(canonical(response).encode())>OUTPUT_LIMIT:
            response=self._response(op,arm,'LIMIT','WHOLE_RESPONSE_BYTE_LIMIT')
        if type(response.get('evidence_selection')) is dict:
            response['evidence_selection']['exhaustive']=(response['evidence_selection'].get('mode')=='bounded_all' and response.get('complete_scope') is True and response.get('coverage',{}).get('complete') is True and response.get('status') in ('MATCH','NO_MATCH'))
        return response
    def _run_valid(self,r,arm):
        op=r['op'];out=self._response(op,arm);v4=op=='search' and type(r['query']) is dict and r['query'].get('schema')=='scout.query/4'
        plan=self._query_plan(r['query']) if v4 else None
        snaps,source_faults=self._snapshot();metas=self._metas(snaps)
        by_ref={(m['system'],m['object_id']):m for m in metas.values()}
        systems=set(self.sources) if op in ('search','match') else {x['system'] for x in r['objects']} if op=='exact' else {r['object']['system']}
        if v4 and plan['target']['kind']=='systems':systems=set(plan['target']['systems'])
        if v4 and plan['target']['kind']=='objects':systems={x['system'] for x in plan['target']['objects']}
        gaps=[];good={};scope={};verified={};v4_ranked=[]
        def gap(system,f):gaps.append({'system':system,'status':f.status,'reason':f.reason})
        def resolve(ref):
            if ref['system'] in source_faults:raise source_faults[ref['system']]
            m=by_ref.get((ref['system'],ref['object_id']))
            if m is None:raise Fault('DENIED','OBJECT_ACCESS_NOT_GRANTED')
            return m
        if v4 and plan['target']['kind']=='objects':
            # Resolve every explicit member before any indexed body or source payload.
            for ref in plan['target']['objects']:
                try:m=resolve(ref);scope[slot(m)]=m
                except Fault as f:
                    if f.status=='DENIED':raise
                    gap(ref['system'],f)
        elif op in ('search','match'):
            scope={k:m for k,m in metas.items() if not m['deleted'] and m['system'] in systems}
            if op=='match':
                left=resolve(r['left']);scope[slot(left)]=left
            for s in systems:
                if s in source_faults:gap(s,source_faults[s])
        else:
            refs=r['objects'] if op=='exact' else [r['object']]
            for ref in refs:
                try:m=resolve(ref);scope[slot(m)]=m
                except Fault as f:
                    if f.status=='DENIED':raise
                    gap(ref['system'],f)
        observed={k:m for k,m in scope.items() if oa.configured(self.observation_profile,m)}
        unresolved_observed={k:m for k,m in oa.expected_scope(self.observation_profile,r,systems,self.tenant).items() if k not in scope}
        observed_mode=r.get('evidence_mode')==oa.MODE
        for key,m in scope.items():
            try:
                need(key not in observed or observed_mode,'UNSUPPORTED','OBSERVATION_MODE_REQUIRED')
                good[key]=self._row(m)
                if m['format']=='xlsx':
                    for reason in XLSX_GAPS:gap(m['system'],Fault('UNSUPPORTED',reason))
            except Fault as f:
                if key in observed and f.status=='DENIED':raise
                gap(m['system'],f)
        def verified_units(m):
            key=slot(m)
            if key not in verified:verified[key]=self._verified(m)
            units,source_hash=verified[key]
            return units,lambda u:self._evidence(m,source_hash,u)
        def verify_or_gap(m):
            try:return verified_units(m)
            except Fault as f:
                if oa.configured(self.observation_profile,m) and f.status=='DENIED':raise
                good.pop(slot(m),None);gap(m['system'],f);return None
        if op=='inspect':
            # Product extraction endpoint: no gold fields, search needles or semantic labels.
            m=resolve(r['object']);target_key=slot(m)
            need(target_key not in observed,'UNSUPPORTED','AUTOMATIC_RAW_OBSERVATION_UNAVAILABLE')
            v=verify_or_gap(m) if target_key in good else None
            if v:
                units,ev=v
                need(len(units)<=EXACT_EVIDENCE_LIMIT,'LIMIT','INSPECT_EVIDENCE_LIMIT')
                out['results']=[{'identity':identity(m),'evidence':[ev(u) for u in units]}]
                out['status']='PARSED';out['reason']='BOUNDED_NATIVE_UNITS'
                out['complete_scope']=True
                out['processing']={'method':'native_parser','processor_version':self.runtime_files_sha,'unit_limit':EXACT_EVIDENCE_LIMIT,'units_returned':len(units),'semantic_inference':False,'scope':'parser_units_not_all_visual_or_business_content','enumerated_total':None}
            elif gaps:
                out.update(status=gaps[0]['status'],reason=gaps[0]['reason'])
        elif op in ('recognize','classify'):
            m=resolve(r['object']);target_key=slot(m);v=verify_or_gap(m) if target_key in good else None
            if v:
                units,ev=v
                if op=='classify':
                    # Default to the existing checked record grouping. A source
                    # object is not itself proof that all its fields describe
                    # one record or subject.
                    if m['format'] not in ('csv','json','record'):
                        # A caller's single-object declaration is not evidence
                        # that separate text lines refer to one subject.
                        out.update(status='UNKNOWN',reason='STRUCTURED_RECORD_BOUNDARY_UNVERIFIED',results=[],text_field_notes=['CLASSIFICATION_BINDING_NOT_APPLIED'])
                        gap(m['system'],Fault('UNKNOWN','STRUCTURED_RECORD_BOUNDARY_UNVERIFIED'))
                    else:
                        profile=self.text_field_profile if self.text_field_profile is not None else {'schema':tf.SCHEMA,'bindings':[],'_legacy_classification_aliases':True}
                        out['status'],out['results'],notes=tf.checked_classify(units,m,profile,ev,arm)
                        out['text_field_notes']=notes
                        if 'TEXT_RECORD_NOT_DECLARED' in notes:
                            out['status']='UNKNOWN';out['reason']='RECORD_SCOPE_NOT_DECLARED'
                            gap(m['system'],Fault('UNKNOWN','TEXT_RECORD_NOT_DECLARED'))
                        elif m['format']=='json' and len(out['results'][0]['record_groups'])>1:
                            # Classify only independently checked records. An
                            # undeclared relation is not itself evidence of two
                            # competing, verified record candidates.
                            groups=out['results'][0]['record_groups']
                            positive_groups=sum(bool(group['labels']) for group in groups)
                            if any(group['status']=='AMBIGUOUS' for group in groups) or positive_groups>1:
                                out['status']='AMBIGUOUS'
                            elif positive_groups==1:
                                out['status']='PARTIAL'
                            else:
                                out['status']='UNKNOWN'
                            out['reason']='JSON_RECORD_RELATION_NOT_DECLARED'
                            out['text_field_notes'].append('JSON_RECORD_RELATION_NOT_DECLARED')
                            gap(m['system'],Fault('UNKNOWN','JSON_RECORD_RELATION_NOT_DECLARED'))
                        if self.text_field_profile is None:
                            out['text_field_interpretation']=dict(tf.descriptor(profile),runtime_files_sha256=self.runtime_files_sha,activation='classification_default')
                elif self.text_field_profile is not None:
                    out['results'],notes=tf.checked_recognize(units,m,self.text_field_profile,ev,arm)
                    out['text_field_notes']=notes
                else:out['results']=recognize(units,ev,arm)
            else:
                f=gaps[0];out.update(status=f['status'],reason=f['reason'])
        elif op=='search' and v4:
            v4_ranked=self._field_search(plan,scope,good,arm,gap)
            out['reason']='TYPED_QUERY_V4'
        elif op=='search':
            ranked=[];plan=compile_query(r['query'])
            for key,m in scope.items():
                if key not in good:continue
                if arm=='baseline':
                    ordinals=[row[0] for row in self.db.execute('SELECT ordinal FROM units WHERE slot=? ORDER BY ordinal',(key,))]
                else:
                    grams=candidate_keys(plan);marks=','.join('?' for _ in grams)
                    sql='SELECT DISTINCT ordinal FROM terms WHERE slot=? AND term IN ('+marks+') ORDER BY ordinal'
                    ordinals=[row[0] for row in self.db.execute(sql,(key,*grams))]
                if not plan['legacy']:
                    totals=[0]*len(plan['clauses']);witnesses={};matched_length=0
                for ordinal in ordinals:
                    row=self.db.execute('SELECT body,unit_json FROM units WHERE slot=? AND ordinal=?',(key,ordinal)).fetchone()
                    body=row['body']
                    if plan['legacy']:
                        if arm=='baseline':
                            if plan['raw'] not in body:continue
                            result_score={'matched_terms':1,'term_occurrences':body.count(plan['raw'])}
                            rank=(key,ordinal)
                        else:
                            result_score=lexical_score(body,plan['groups'])
                            if not result_score['matched_terms']:continue
                            rank=(-result_score['matched_terms'],-result_score['term_occurrences'],len(body),key,ordinal)
                        ranked.append((rank,m,[ordinal],result_score))
                    else:
                        counts=typed_counts(strict_json(row['unit_json']),plan,arm)
                        if not any(counts):continue
                        if plan['scope']=='unit':
                            if not accepted(counts,plan):continue
                            result_score=counts_score(counts)
                            rank=(-result_score['matched_terms'],-result_score['term_occurrences'],len(body),key,ordinal)
                            ranked.append((rank,m,[ordinal],result_score))
                        else:
                            matched_length+=len(body)
                            for i,count in enumerate(counts):
                                totals[i]+=count
                                if count and i not in witnesses:witnesses[i]=ordinal
                if not plan['legacy'] and plan['scope']=='object' and accepted(totals,plan):
                    result_score=counts_score(totals)
                    rank=(-result_score['matched_terms'],-result_score['term_occurrences'],matched_length,key)
                    ranked.append((rank,m,sorted(set(witnesses.values())),result_score))
            for _,m,ordinals,result_score in sorted(ranked,key=lambda x:x[0]):
                if slot(m) not in good:continue
                v=verify_or_gap(m)
                if v:
                    units,ev=v;out['results'].append({'score':result_score,'evidence':[ev(units[o]) for o in ordinals]})
                if len(out['results'])>=r['k']:break
            out['status']='OK' if out['results'] else 'NO_HIT'
            out['reason']='LEGACY_V2_RETRIEVAL' if plan['legacy'] else 'TYPED_QUERY_V3'
        elif op=='exact':
            all_evidence=r.get('all_evidence',False)
            evidence_count=0
            out['evidence_selection']={'mode':'bounded_all' if all_evidence else 'first_per_object','limit':EXACT_EVIDENCE_LIMIT if all_evidence else 1,'truncated':False,'exhaustive':all_evidence,'limit_scope':'request' if all_evidence else 'object'}
            # Every requested object is reread and reparsed, even when index produced NO hits.
            for key,m in sorted(scope.items()):
                if key not in good:continue
                v=verify_or_gap(m)
                if v:
                    units,ev=v;hits=[u for u in units if r['needle'] in u['text']]
                    if hits:
                        selected=hits if all_evidence else hits[:1]
                        evidence_count+=len(selected)
                        need(not all_evidence or evidence_count<=EXACT_EVIDENCE_LIMIT,'LIMIT','EXACT_EVIDENCE_LIMIT')
                        out['results'].append({'identity':identity(m),'evidence':[ev(hit) for hit in selected]})
            if not gaps:
                out['complete_scope']=True;out['status']='MATCH' if out['results'] else 'NO_MATCH';out['absence_proven']=not out['results']
        elif op=='match':
            entities={};evfns={}
            for key,m in scope.items():
                if key not in good:continue
                v=verify_or_gap(m)
                if v:
                    units,ev=v
                    # A caller's single-object declaration does not establish
                    # that unstructured fields belong to one actual subject.
                    if m['format'] not in ('csv','json','record'):
                        gap(m['system'],Fault('UNKNOWN','MATCH_RECORD_BOUNDARY_UNVERIFIED'))
                        continue
                    profile=self.text_field_profile if self.text_field_profile is not None else {'schema':tf.SCHEMA,'bindings':[],'_legacy_classification_aliases':True}
                    candidate,emit=tf.checked_entity(units,m,profile,ev)
                    if not any(candidate[f] for f in ('organization','email','external_ref')):
                        gap(m['system'],Fault('UNKNOWN','MATCH_ENTITY_KEY_NOT_AVAILABLE'))
                        continue
                    entities[key],evfns[key]=candidate,emit
                    if self.text_field_profile is None:
                        out['text_field_interpretation']=dict(tf.descriptor(profile),runtime_files_sha256=self.runtime_files_sha,activation='matching_default')
            lk=slot(left);a=entities.get(lk)
            if a is None:out['status']='INCOMPLETE'
            elif a['ambiguous']:out['status']='AMBIGUOUS';out['reason']='LEFT_ENTITY_SCOPE_AMBIGUOUS'
            else:
                left_counts=collections.Counter(ref for k,e in entities.items() if scope[k]['system']==left['system'] for ref in e['ref_values'])
                right_counts=collections.Counter(ref for k,e in entities.items() if scope[k]['system']==r['right_system'] for ref in e['ref_values'])
                uncertain_right=any(e['ambiguous'] for k,e in entities.items() if scope[k]['system']==r['right_system'])
                for key,b in sorted(entities.items()):
                    m=scope[key]
                    if m['system']!=r['right_system']:continue
                    # checked_entity marks multiple source records or conflicts
                    # ambiguous; never emit its flattened cross-record units.
                    if b['ambiguous']:continue
                    basis=[];decision=None
                    if arm=='baseline':
                        if a['raw_names'] & b['raw_names']:basis=['organization'];decision='REVIEW_CANDIDATE'
                    else:
                        if a['external_ref'] and a['external_ref']==b['external_ref']:
                            basis=['external_ref'];decision='MATCH_CANDIDATE'
                            conflicts=any(a[f] and b[f] and a[f]!=b[f] for f in ('email','organization'))
                            if conflicts or b['ambiguous']:decision='AMBIGUOUS';basis.append('conflict')
                            if left_counts[a['external_ref']]!=1 or right_counts[b['external_ref']]!=1:decision='AMBIGUOUS';basis.append('duplicate')
                        elif (a['email'] and a['email']==b['email']) or (a['organization'] and a['organization']==b['organization']):
                            decision='REVIEW_CANDIDATE';basis=[f for f in ('email','organization') if a[f] and a[f]==b[f]]
                            if b['ambiguous'] or (a['external_ref'] and b['external_ref'] and a['external_ref']!=b['external_ref']):decision='AMBIGUOUS';basis.append('conflict')
                        if gaps and decision=='MATCH_CANDIDATE':decision='REVIEW_CANDIDATE'
                    if decision:out['results'].append({'decision':decision,'identity':identity(m),'basis':basis,'evidence':[evfns[lk](u) for u in a['units']]+[evfns[key](u) for u in b['units']]})
                decisions={x['decision'] for x in out['results']}
                out['status']='AMBIGUOUS' if 'AMBIGUOUS' in decisions else 'MATCH_CANDIDATE' if 'MATCH_CANDIDATE' in decisions else 'REVIEW_CANDIDATE' if decisions else 'NO_CANDIDATE'
                if uncertain_right:
                    out['status']='AMBIGUOUS';out['reason']='RIGHT_ENTITY_SCOPE_AMBIGUOUS'
                    for result in out['results']:
                        if result['decision']=='MATCH_CANDIDATE':
                            result['decision']='REVIEW_CANDIDATE';result['basis'].append('incomplete_right_scope')
        # Final current-authority recheck; never expose a result from an affected source.
        hook=self._test_hooks.get('before_final_recheck')
        if hook is not None:hook()
        observation_bad={}
        if observed_mode:
            for key,m in observed.items():
                if key not in good:continue
                try:self._verified(m)
                except Fault as f:
                    if f.status=='DENIED':raise
                    good.pop(key,None);observation_bad[key]=f;gap(m['system'],f)
            if observation_bad:
                out['results']=[x for x in out['results'] if all(slot(e['identity']) not in observation_bad for e in x.get('evidence',[]))]
        final,final_faults=self._snapshot();bad=set()
        for s in systems:
            if s in final_faults or s not in snaps or s not in final or snaps[s]['token']!=final[s]['token']:
                bad.add(s)
                if s not in source_faults:gap(s,final_faults.get(s,Fault('STALE','AUTHORIZATION_CATALOG_CHANGED')))
        if bad:
            scope={k:m for k,m in scope.items() if m['system'] not in bad};good={k:v for k,v in good.items() if k in scope}
            if op=='search':out['results']=[x for x in out['results'] if all(e['identity']['system'] not in bad for e in x['evidence'])]
            else:out['results']=[]
        if v4:
            # Exclusion precedes aggregate counts, ranking and top-k; safe sources refill.
            survivors=[(rank,result) for rank,result in v4_ranked if all(e['identity']['system'] not in bad for e in result['evidence'])]
            groups=[result for rank,result in sorted(survivors,key=lambda x:x[0])]
            out['query_details']=fq.details(plan,groups,r['k'])
            out['results']=groups[:r['k']]
            out['status']='OK' if out['results'] else 'NO_HIT'
        if gaps:
            if op=='exact' and any(g['status']=='DENIED' for g in gaps):
                return self._response(op,arm,'DENIED','OBJECT_ACCESS_NOT_GRANTED')
            if op in ('search','match','exact','inspect'):out['status']='INCOMPLETE';out['reason']='AUTHORIZED_SCOPE_HAS_GAPS'
            elif bad:out['status']='STALE';out['reason']='FINAL_AUTHORIZATION_NOT_CURRENT'
            elif op in ('recognize','classify') and target_key in observation_bad:
                # Final source/artifact failure invalidates the explicit target's whole pack.
                f=observation_bad[target_key];out.update(status=f.status,reason=f.reason,results=[])
            if op=='exact':out['results']=[]
            if op=='inspect':
                safe_xlsx=(m['format']=='xlsx' and target_key in verified and target_key in good and not bad
                           and all(g['status']=='UNSUPPORTED' and g['reason'] in XLSX_GAPS for g in gaps))
                if not safe_xlsx:out['results']=[]
            if op in ('recognize','classify') and m['format']=='xlsx':
                out.update(status='INCOMPLETE',reason='XLSX_CLASSIFICATION_NOT_SUPPORTED',results=[])
            if op=='match':
                incomplete_right=any(g['system']==r['right_system'] for g in gaps)
                incomplete_other=any(g['system']!=r['right_system'] for g in gaps)
                for x in out['results']:
                    if x['decision']=='MATCH_CANDIDATE':x['decision']='REVIEW_CANDIDATE'
                    if x['decision']=='REVIEW_CANDIDATE':
                        if incomplete_right and 'incomplete_right_scope' not in x['basis']:
                            x['basis'].append('incomplete_right_scope')
                        if incomplete_other and 'incomplete_authorized_scope' not in x['basis']:
                            x['basis'].append('incomplete_authorized_scope')
            out['absence_proven']=False;out['complete_scope']=False
        out['coverage'].update(authorized_objects=len(scope),indexed_current_objects=len(good),gap_count=len(gaps),gaps=gaps,complete=not gaps)
        return oa.overlay(out,scope,good,observed,unresolved_observed,bool(gaps),self.observation_profile is not None,observed_mode,self.observation_profile,self.runtime_files_sha)

def _strict_classify_exit(response, sync_summary=None, *, sync_requested=False):
    # Shape and declared scope only, never an authorization or safety verdict.
    if type(response) is not dict or response.get('schema')!='scout.lite/1' or response.get('operation')!='classify':return 3
    if response.get('status')!='OK' or response.get('reason') not in ('SUPPORTED_TASK','DECLARED_RECORD_SCOPE_CHECKED'):return 3
    coverage=response.get('coverage');results=response.get('results')
    if type(coverage) is not dict or coverage.get('scope')!='authorized_current_objects' or coverage.get('complete') is not True:return 3
    if type(coverage.get('authorized_objects')) is not int or coverage['authorized_objects']<1:return 3
    if type(coverage.get('indexed_current_objects')) is not int or coverage['indexed_current_objects']!=coverage['authorized_objects']:return 3
    if type(coverage.get('gap_count')) is not int or coverage['gap_count']!=0 or coverage.get('gaps')!=[]:return 3
    if type(results) is not list or len(results)!=1 or type(results[0]) is not dict:return 3
    if type(results[0].get('labels')) is not list or not results[0]['labels'] or any(type(x) is not str for x in results[0]['labels']):return 3
    if type(results[0].get('evidence')) is not list or not results[0]['evidence'] or any(type(x) is not dict for x in results[0]['evidence']):return 3
    if sync_requested and (type(sync_summary) is not dict or sync_summary.get('status')!='OK'):return 3
    return 0

def _sync_incomplete_notice(sync_summary):
    if type(sync_summary) is dict and sync_summary.get('status')=='OK':return None
    status=sync_summary.get('status') if type(sync_summary) is dict else None
    reason=sync_summary.get('reason') if type(sync_summary) is dict else None
    return {'event':'SCOUT_SYNC_NOT_COMPLETE',
            'status':status if type(status) is str and status in ('OK','INCOMPLETE','LIMIT') else 'UNKNOWN',
            'reason':reason if type(reason) is str and reason in ('SYNC_CURRENT_AUTHORIZED_SCOPE','INDEX_LIMIT_OR_WRITE_FAILED') else 'UNKNOWN'}

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--request',required=True);p.add_argument('--sync',action='store_true');p.add_argument('--arm',choices=('candidate','baseline'),default='candidate');p.add_argument('--strict-classify-exit',action='store_true',help='Opt in: exit 3 for non-classify, incomplete sync or non-complete classification; exit 0 is not a safety or authorization decision.');a=p.parse_args()
    engine=None
    try:
        from .sources import safe_read
        def input_json(path):
            p=Path(path).absolute();return strict_json(safe_read(p.parent,p.name).decode('utf-8'))
        config=input_json(a.config);request=input_json(a.request);engine=Engine(config)
        sync_summary=engine.sync() if a.sync else None
        response=engine.run(request,a.arm)
        print(canonical(response))
        if a.sync:
            notice=_sync_incomplete_notice(sync_summary)
            if notice is not None:print(canonical(notice),file=sys.stderr)
        if a.strict_classify_exit:
            code=_strict_classify_exit(response,sync_summary,sync_requested=a.sync)
            if code:print('SCOUT_STRICT_CLASSIFY_NOT_COMPLETE',file=sys.stderr)
            return code
    except Fault as f:
        print(canonical(dict(word_effective_interpretation=engine.word_descriptor if engine else {'profile':'UNRESOLVED_CONFIG_OR_ADMISSION','rendered':False},effective_interpretation=engine.interpretation_descriptor if engine else {'profile':'UNRESOLVED_CONFIG_OR_ADMISSION','rendered':False},schema=SCHEMA,protocol_revision='4.0',operation='invalid',arm=a.arm,status=f.status,reason=f.reason,results=[],coverage=Engine._coverage(),absence_proven=False,complete_scope=False)))
        return 2
    finally:
        if engine:engine.close()
    return 0
if __name__=='__main__':raise SystemExit(main())
