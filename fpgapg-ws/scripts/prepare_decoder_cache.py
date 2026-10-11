#!/usr/bin/env python3
"""Operator-only database preparation; never invoked for submitted HDL."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bitstream-decode'))
from pipeline import database_root

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--part', choices=['xc7a35tcpg236-1', 'xc7a100tcsg324-1'], required=True)
    args = parser.parse_args()
    root = ROOT / '.tools/decoder'
    root.mkdir(parents=True, exist_ok=True)
    target = root / f'{args.part}.db'
    if target.is_file():
        parser.error(f'{target} already exists; move it aside before regenerating')
    temporary = root / f'{args.part}.building.db'
    temporary.unlink(missing_ok=True)
    try:
        subprocess.run([sys.executable, '-m', 'fasm2bels.database.create_channels',
                        '--db-root', str(database_root(args.part)), '--part', args.part,
                        '--connection-database-output', str(temporary)], check=True)
        temporary.chmod(0o444)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    print(target)
