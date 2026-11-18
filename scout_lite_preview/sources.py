"""Small synthetic read-only connectors. No production authentication or writes."""
from __future__ import annotations
import errno, hashlib, json, os, re, sqlite3, stat, time
from decimal import Decimal
from pathlib import Path, PurePosixPath
from urllib.parse import quote

MAX_BYTES = 262144
MAX_OBJECTS = 1000

class Fault(Exception):
    def __init__(self, status, reason):
        super().__init__(reason); self.status, self.reason = status, reason

def need(ok, status='INVALID_SOURCE', reason='SCHEMA_INVALID'):
    if not ok: raise Fault(status, reason)

def digest(data): return hashlib.sha256(data).hexdigest()
def canonical(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
def jd(value): return digest(canonical(value).encode())

def strict_json(text, number_exact=False):
    def obj(pairs):
        out = {}
        for k, v in pairs:
            need(k not in out, reason='DUPLICATE_JSON_KEY'); out[k] = v
        return out
    def invalid(_): raise Fault('INVALID_SOURCE', 'NONFINITE_JSON')
    try: return json.loads(text, object_pairs_hook=obj, parse_constant=invalid, parse_float=Decimal if number_exact else float)
    except (ValueError, UnicodeError, RecursionError) as exc: raise Fault('INVALID_SOURCE', 'JSON_INVALID') from exc

def ident(s): return type(s) is str and 0 < len(s) <= 80 and not any(ord(c) < 32 for c in s)

def directory_fd(path):
    p = Path(path)
    need(p.is_absolute() and '..' not in p.parts, 'DENIED', 'PATH_NOT_CONFINED')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in p.parts[1:]:
            nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = nxt
        return fd
    except BaseException:
        os.close(fd); raise

def safe_read(root, relative, max_bytes=MAX_BYTES):
    """Descriptor-relative no-follow traversal; regular file, bound bytes, stable capture."""
    parts = PurePosixPath(relative).parts
    need(type(relative) is str and relative and not relative.startswith('/') and
         all(p not in ('.', '..', '') for p in relative.split('/')),
         'DENIED', 'PATH_NOT_CONFINED')
    fd = -1; parent = -1
    try:
        parent = directory_fd(root)
        for component in parts[:-1]:
            nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent); parent = nxt
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode), 'DENIED', 'NOT_REGULAR_FILE')
        need(before.st_size <= max_bytes, 'LIMIT', 'SOURCE_BYTE_LIMIT')
        chunks = []; total = 0
        while True:
            chunk = os.read(fd, min(65536, max_bytes + 1 - total))
            if not chunk: break
            chunks.append(chunk); total += len(chunk)
            need(total <= max_bytes, 'LIMIT', 'SOURCE_BYTE_LIMIT')
        after = os.fstat(fd)
        key = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        need(key(before) == key(after) and total == after.st_size, 'STALE', 'SOURCE_CHANGED_DURING_READ')
        return b''.join(chunks)
    finally:
        if fd >= 0: os.close(fd)
        if parent >= 0: os.close(parent)

def retry(call):
    for attempt in range(2):
        try: return call()
        except OSError as exc:
            transient = exc.errno in (errno.EINTR, errno.EAGAIN, errno.EBUSY)
            if transient and attempt == 0: time.sleep(.005); continue
            if exc.errno in (errno.EACCES, errno.EPERM, errno.ELOOP, errno.ENOTDIR):
                raise Fault('DENIED', 'SOURCE_PATH_OR_ACCESS_DENIED') from exc
            raise Fault('SOURCE_UNAVAILABLE', 'TRANSIENT_EXHAUSTED' if transient else 'SOURCE_READ_UNAVAILABLE') from exc
        except sqlite3.OperationalError as exc:
            transient = 'locked' in str(exc).lower() or 'busy' in str(exc).lower()
            if transient and attempt == 0: time.sleep(.005); continue
            raise Fault('SOURCE_UNAVAILABLE', 'TRANSIENT_EXHAUSTED' if transient else 'SQLITE_READ_UNAVAILABLE') from exc
        except sqlite3.DatabaseError as exc:
            raise Fault('INVALID_SOURCE', 'SQLITE_INVALID') from exc

def meta_validate(m):
    need(type(m) is dict)
    for k in ('tenant', 'object_id', 'version', 'owner'): need(ident(m.get(k)))
    need(type(m.get('acl')) is list and len(m['acl']) <= 100 and all(ident(x) for x in m['acl']))
    need(len(set(m['acl'])) == len(m['acl']) and type(m.get('deleted')) is bool)
    return m

def identity(m): return {k: m[k] for k in ('system', 'tenant', 'object_id', 'version', 'owner')}
def slot(m): return canonical([m['system'], m['tenant'], m['object_id']])
def signature(m):
    return jd({k: m.get(k) for k in ('system','tenant','object_id','version','owner','acl','deleted','format','path','sha256')})
def allowed(m, tenant, principal): return m['tenant'] == tenant and principal in m['acl']

class DocumentSource:
    def __init__(self, spec): self.system, self.path = spec['system'], Path(spec['path'])
    def snapshot(self):
        raw = retry(lambda: safe_read(self.path, 'manifest.json'))
        v = strict_json(raw.decode('utf-8'))
        need(type(v) is dict and v.get('schema') == 'document-source/1')
        need(type(v.get('epoch')) is int and v['epoch'] > 0)
        objects = v.get('objects'); need(type(objects) is list and len(objects) <= MAX_OBJECTS, 'LIMIT', 'CATALOG_OBJECT_LIMIT')
        metas = []; seen = set()
        for src in objects:
            m = dict(meta_validate(src)); key = (m['tenant'], m['object_id'])
            need(key not in seen, reason='DUPLICATE_SOURCE_IDENTITY'); seen.add(key)
            need(type(m.get('format')) is str and len(m['format']) <= 16)
            need(type(m.get('path')) is str and 0 < len(m['path']) <= 1024)
            need(type(m.get('sha256')) is str and re.fullmatch('[0-9a-f]{64}', m['sha256']))
            m.update(system=self.system, epoch=v['epoch']); m['acl'] = sorted(m['acl']); metas.append(m)
        return {'metas': metas, 'token': digest(raw), 'epoch': v['epoch']}
    def capture(self, m, tenant, principal):
        # Trusted caller scope is independent of the requested metadata snapshot.
        need(m['system']==self.system and m['tenant']==tenant, 'DENIED', 'OBJECT_ACCESS_NOT_GRANTED')
        current=self.snapshot()
        fresh=next((v for v in current['metas'] if v['tenant']==tenant and v['object_id']==m['object_id']),None)
        need(fresh is not None, 'NOT_AVAILABLE', 'OBJECT_NOT_AVAILABLE')
        need(allowed(fresh,tenant,principal), 'DENIED', 'OBJECT_ACCESS_NOT_GRANTED')
        need(not fresh['deleted'], 'NOT_AVAILABLE', 'OBJECT_NOT_AVAILABLE')
        need(current['epoch']==m['epoch'] and signature(fresh)==signature(m), 'STALE', 'ROW_OR_AUTHORIZATION_CHANGED')
        # Manifest and file are not one transaction. Final recheck still applies.
        raw = retry(lambda: safe_read(self.path, fresh['path']))
        need(digest(raw) == m['sha256'], 'STALE', 'SOURCE_DIGEST_CHANGED')
        return raw

class RelationalSource:
    FIELDS = {'tenant','record_key','rev','owner','readers_json','deleted','org_name','contact_email','external_ref','description','record_type'}
    def __init__(self, spec): self.system, self.path = spec['system'], Path(spec['path'])
    def _connect(self):
        # No read/write URI, no extensions, no sidecar writes. Require a closed, committed synthetic producer DB.
        fd = directory_fd(self.path.parent); c = None
        try:
            f = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                s = os.fstat(f); need(stat.S_ISREG(s.st_mode), 'DENIED', 'NOT_REGULAR_FILE')
                need(s.st_size <= MAX_BYTES, 'LIMIT', 'SOURCE_BYTE_LIMIT')
            finally: os.close(f)
            for suffix in ('-wal', '-journal'):
                try: side = os.stat(self.path.name + suffix, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError: continue
                need(side.st_size == 0, 'SOURCE_UNAVAILABLE', 'UNCOMMITTED_SQLITE_SOURCE')
            c = sqlite3.connect('file:' + quote(str(self.path), safe='/') + '?mode=ro', uri=True, timeout=0)
            c.execute('PRAGMA query_only=ON'); c.execute('PRAGMA trusted_schema=OFF')
            current = os.stat(self.path.name, dir_fd=fd, follow_symlinks=False)
            need(stat.S_ISREG(current.st_mode) and (s.st_dev,s.st_ino)==(current.st_dev,current.st_ino), 'STALE', 'SQLITE_PATH_CHANGED')
            need(c.execute('PRAGMA user_version').fetchone()[0] == 1, reason='SQLITE_SCHEMA_VERSION')
            for table in ('records','source_state'):
                definition=c.execute('SELECT type,sql FROM sqlite_master WHERE name=?',(table,)).fetchone()
                need(definition is not None and definition[0]=='table' and type(definition[1]) is str and re.match(r'^CREATE\s+TABLE\b',definition[1],re.I), reason='SQLITE_REAL_TABLE_REQUIRED')
            columns={r[1]:r for r in c.execute('PRAGMA table_info(records)')}
            need(set(columns)==self.FIELDS and all(r[2].upper()==('INTEGER' if k=='deleted' else 'TEXT') for k,r in columns.items()), reason='SQLITE_SCHEMA_CHANGED')
            need(columns['tenant'][5]==1 and columns['record_key'][5]==2 and all(columns[k][3]==1 for k in ('tenant','record_key','rev','owner','readers_json','deleted')),reason='SQLITE_KEY_SCHEMA')
            state_columns=list(c.execute('PRAGMA table_info(source_state)'))
            need(len(state_columns)==1 and state_columns[0][1]=='epoch' and state_columns[0][2].upper()=='INTEGER',reason='SQLITE_STATE_SCHEMA')
            return c
        except BaseException:
            if c is not None: c.close()
            raise
        finally: os.close(fd)
    def _read(self, fn):
        def action():
            c = self._connect()
            try:
                c.execute('BEGIN')
                state = c.execute('SELECT epoch FROM source_state LIMIT 2').fetchall()
                need(len(state)==1 and type(state[0][0]) is int and state[0][0]>0, reason='SQLITE_EPOCH_INVALID')
                return fn(c, state[0][0])
            finally: c.close()
        return retry(action)
    def snapshot(self):
        def action(c, epoch):
            rows = c.execute('SELECT tenant,record_key,rev,owner,readers_json,deleted FROM records ORDER BY tenant,record_key LIMIT ?', (MAX_OBJECTS+1,)).fetchall()
            need(len(rows)<=MAX_OBJECTS, 'LIMIT', 'CATALOG_OBJECT_LIMIT')
            metas=[]
            for t, key, rev, owner, readers, deleted in rows:
                need(type(readers) is str and type(deleted) is int and deleted in (0,1))
                m=meta_validate(dict(tenant=t,object_id=key,version=rev,owner=owner,acl=strict_json(readers),deleted=bool(deleted)))
                m.update(system=self.system,epoch=epoch,format='record'); m['acl']=sorted(m['acl']); metas.append(m)
            return {'metas':metas,'token':jd([epoch,metas]),'epoch':epoch}
        return self._read(action)
    def capture(self, m, tenant, principal):
        need(m['system']==self.system and m['tenant']==tenant, 'DENIED', 'OBJECT_ACCESS_NOT_GRANTED')
        def action(c, epoch):
            # No payload-column projection before this transaction's current ACL check.
            # SQLite page reads are not a physical-byte isolation mechanism.
            meta=c.execute('SELECT tenant,record_key,rev,owner,readers_json,deleted FROM records WHERE tenant=? AND record_key=?', (tenant,m['object_id'])).fetchone()
            need(meta is not None, 'NOT_AVAILABLE', 'OBJECT_NOT_AVAILABLE')
            t,key,rev,owner,readers,deleted=meta
            need(type(readers) is str and type(deleted) is int and deleted in (0,1))
            fresh=meta_validate(dict(tenant=t,object_id=key,version=rev,owner=owner,acl=strict_json(readers),deleted=bool(deleted)))
            fresh.update(system=self.system,epoch=epoch,format='record');fresh['acl']=sorted(fresh['acl'])
            need(allowed(fresh,tenant,principal), 'DENIED', 'OBJECT_ACCESS_NOT_GRANTED')
            need(not fresh['deleted'], 'NOT_AVAILABLE', 'OBJECT_NOT_AVAILABLE')
            need(epoch==m['epoch'], 'STALE', 'SOURCE_EPOCH_CHANGED')
            need(signature(fresh)==signature(m), 'STALE', 'ROW_OR_AUTHORIZATION_CHANGED')
            # _read holds the SAME consistent read transaction through both SELECTs.
            row=c.execute('SELECT org_name,contact_email,external_ref,description,record_type FROM records WHERE tenant=? AND record_key=?', (tenant,m['object_id'])).fetchone()
            need(row is not None, 'NOT_AVAILABLE', 'OBJECT_NOT_AVAILABLE')
            names=('organization','email','external_ref','description','document_type')
            need(all(v is None or type(v) is str for v in row), reason='SQLITE_VALUE_TYPE')
            raw=canonical(dict(zip(names,row))).encode()
            need(len(raw)<=MAX_BYTES,'LIMIT','SOURCE_BYTE_LIMIT')
            return raw
        return self._read(action)

def make_source(spec): return (DocumentSource if spec['kind']=='document' else RelationalSource)(spec)
