"""Install one pinned local wheel into a new venv using an existing pip."""
import argparse
import hashlib
import runpy
import sys
import venv
from pathlib import Path


def guard(event, args):
    if event.startswith(('socket.', 'subprocess.')) or event in ('os.system', 'os.fork', 'os.kill', 'os.posix_spawn', 'os.exec'):
        raise RuntimeError('PROCESS_OR_NETWORK_BLOCKED')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('wheel', type=Path)
    parser.add_argument('environment', type=Path)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args()
    if sys.version_info < (3, 11) or not sys.dont_write_bytecode:
        raise SystemExit('Python 3.11+ and -B are required')
    if args.wheel.is_symlink() or not args.wheel.is_file():
        raise SystemExit('Local wheel missing or symbolic link')
    wheel = args.wheel.resolve(strict=True)
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != args.sha256:
        raise SystemExit('Wheel SHA-256 mismatch')
    if args.environment.exists() or args.environment.is_symlink():
        raise SystemExit('Environment already exists')
    env = args.environment.parent.resolve(strict=True) / args.environment.name
    sys.addaudithook(guard)
    venv.EnvBuilder(with_pip=False, symlinks=True, system_site_packages=False).create(str(env))
    site = env / 'lib' / ('python%d.%d' % sys.version_info[:2]) / 'site-packages'
    if not site.is_dir() or any(site.iterdir()):
        raise SystemExit('Fresh environment site-packages is not empty')
    sys.argv = ['pip', '--isolated', 'install', '--no-index', '--no-deps', '--no-compile', '--no-cache-dir',
                '--disable-pip-version-check', '--target', str(site), str(wheel)]
    try:
        runpy.run_module('pip', run_name='__main__')
    except SystemExit as exc:
        if exc.code not in (0, None):
            raise


if __name__ == '__main__':
    main()
