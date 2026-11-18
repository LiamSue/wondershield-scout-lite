"""Prepare a disposable config for the bundled synthetic example."""
import hashlib
import json
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent
PINS = {
    'docs/book.xlsx': '138ce8029840b4258103b2f72ebb5d0a9ff9cc054733d69f29dc0591df05744d',
    'docs/manifest.json': '2b9187814282081457faf5b16e6242d373ccba815431add866bb58f95650e209',
    'rows.sqlite': '7123e173faa4a25bbb3960ebc1d52b5c4226dddc8c91a849b379a71dc7fb6efe',
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


if len(sys.argv) != 2:
    raise SystemExit('Usage: prepare_demo.py NEW_DIRECTORY')
for rel, expected in PINS.items():
    path = EXAMPLES / rel
    if path.is_symlink() or not path.is_file() or sha(path) != expected:
        raise SystemExit('Synthetic example identity mismatch: ' + rel)
requested = Path(sys.argv[1]).expanduser()
parent = requested.parent.resolve(strict=True)
out = parent / requested.name
out.mkdir(mode=0o700, exist_ok=False)
config = {
    'schema': 'scout.lite/1', 'tenant': 't', 'principal': 'p',
    'index': str(out / 'index.sqlite'),
    'sources': [
        {'system': 'files', 'kind': 'document', 'path': str((EXAMPLES / 'docs').resolve())},
        {'system': 'rows', 'kind': 'sqlite', 'path': str((EXAMPLES / 'rows.sqlite').resolve())},
    ],
}
request = {'op': 'inspect', 'object': {'system': 'files', 'object_id': 'book'}}
for name, data in [('config.json', config), ('request.json', request)]:
    with (out / name).open('x', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
print('Prepared synthetic example:', out)
