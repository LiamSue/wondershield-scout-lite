"""Build the pinned local Scout Lite wheel without network or subprocesses."""
import argparse
import ast
import base64
import csv
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


def guard(event,args):
    if event.startswith(('socket.','subprocess.')) or event in ('os.system','os.fork','os.kill','os.posix_spawn','os.exec'):
        raise RuntimeError('PROCESS_OR_NETWORK_BLOCKED')


sys.addaudithook(guard)


def sha(data):return hashlib.sha256(data).hexdigest()


def checked(path,expected):
    if path.is_symlink() or not path.is_file():raise RuntimeError('INPUT_INVALID:'+path.name)
    data=path.read_bytes()
    if sha(data)!=expected:raise RuntimeError('INPUT_HASH_MISMATCH:'+path.name)
    return data


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=Path('dist'));args=p.parse_args()
    if not sys.dont_write_bytecode:raise RuntimeError('PYTHON_B_REQUIRED')
    manifest=json.loads((ROOT/'RELEASE_INPUTS.json').read_text())
    if manifest['schema']!='scout.release-inputs/1':raise RuntimeError('INPUT_SCHEMA_INVALID')
    dist=manifest['distribution'];version=manifest['version'];slug=dist.replace('-','_');info=slug+'-'+version+'.dist-info'
    if dist not in ('wondershield-scout-lite','wondershield-scout-lite-structured'):
        raise RuntimeError('DISTRIBUTION_INVALID')
    files=manifest['runtime_files'];names=set(files)
    pkg=ROOT/'scout_lite_preview'
    if {x.name for x in pkg.iterdir()}!=names or len(names) not in (11,12):raise RuntimeError('RUNTIME_SET_INVALID')
    entries={}
    for name,digest in sorted(files.items()):
        data=checked(pkg/name,digest)
        if any(x in data for x in (b'lo_source_model',b'native_coverage',b'openpyxl')):raise RuntimeError('RESTRICTED_REFERENCE:'+name)
        tree=ast.parse(data.decode('utf-8'),filename=name)
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):roots=[a.name.split('.')[0] for a in node.names]
            elif isinstance(node,ast.ImportFrom) and node.module:roots=[node.module.split('.')[0]]
            else:continue
            if any(x in {'socket','subprocess','signal','requests','http','httpx','ollama','openpyxl'} for x in roots):
                raise RuntimeError('RESTRICTED_IMPORT:'+name)
        entries['scout_lite_preview/'+name]=data
    init=ast.parse(entries['scout_lite_preview/__init__.py'].decode())
    actual=next((ast.literal_eval(n.value) for n in init.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='__version__' for t in n.targets)),None)
    if actual!=version:raise RuntimeError('VERSION_MISMATCH')
    notices=manifest['notice_files']
    if set(notices)!={'LICENSE','NOTICE','THIRD_PARTY_NOTICES.md','LICENSES/openpyxl-MIT.txt'}:raise RuntimeError('NOTICE_SET_INVALID')
    for name,digest in sorted(notices.items()):entries[info+'/licenses/'+name]=checked(ROOT/name,digest)
    meta=('Metadata-Version: 2.4\nName: '+dist+'\nVersion: '+version+
          '\nSummary: Local bounded structured-data developer preview\n'
          'Maintainer: Shanghai Suewonder Data Technology Co., Ltd.\nRequires-Python: >=3.11\n'
          'License-Expression: Apache-2.0\n'+''.join('License-File: '+n+'\n' for n in sorted(notices))+
          '\nDeveloper preview; inspect coverage and third-party notices.\n').encode()
    entries[info+'/METADATA']=meta
    entries[info+'/WHEEL']=b'Wheel-Version: 1.0\nGenerator: scout-local-stdlib-builder/1\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
    s=io.StringIO(newline='');writer=csv.writer(s,lineterminator='\n')
    for name,data in sorted(entries.items()):
        writer.writerow([name,'sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode(),len(data)])
    writer.writerow([info+'/RECORD','','']);entries[info+'/RECORD']=s.getvalue().encode()
    out=args.output
    if out.exists() or out.is_symlink():raise RuntimeError('OUTPUT_ALREADY_EXISTS')
    out.parent.resolve(strict=True);out.mkdir(mode=0o700)
    wheel=out/(slug+'-'+version+'-py3-none-any.whl')
    with wheel.open('xb') as f:
        with zipfile.ZipFile(f,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
            for name,data in sorted(entries.items()):
                zi=zipfile.ZipInfo(name,(2026,1,1,0,0,0));zi.create_system=3;zi.external_attr=0o100644<<16
                zi.compress_type=zipfile.ZIP_DEFLATED;z.writestr(zi,data)
    print(json.dumps({'wheel':str(wheel),'sha256':sha(wheel.read_bytes()),'members':len(entries),
                      'distribution':dist,'version':version}))


if __name__=='__main__':main()
