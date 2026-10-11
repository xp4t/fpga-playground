#!/usr/bin/env python3
"""Refresh deployable source in fpgapg-ws; keep its cloud README/configs intact."""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'fpgapg-ws'
FILES = ['server.py', 'build_runner.py', 'requirements.txt', 'requirements-decoder.txt',
         'Dockerfile', 'vercel.json', '.gitignore', '.dockerignore', 'LICENSE',
         'artya7.png', 'nexysa7.avif']
DIRECTORIES = ['api', 'boards', 'bitstream-decode', 'sim-core', 'web', 'tests', 'docs', '.github']


def main():
    DEST.mkdir(exist_ok=True)
    paths = [ROOT / name for name in FILES]
    for name in DIRECTORIES:
        paths.extend(path for path in (ROOT / name).rglob('*') if path.is_file()
                     and '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo'))
    paths.extend(ROOT / 'scripts' / name for name in
                 ('install_openxc7.sh', 'install_decoder.sh', 'prepare_decoder_cache.py'))
    for source in paths:
        destination = DEST / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    print(f'Refreshed {len(paths)} source files in {DEST}')


if __name__ == '__main__':
    main()
