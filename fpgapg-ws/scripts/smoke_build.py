#!/usr/bin/env python3
"""Operator-only real tool smoke test in a disposable workspace."""
import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', choices=['all', *server.BOARDS], default='basys3')
    parser.add_argument('--require-fabric', action='store_true')
    args = parser.parse_args()
    boards = server.BOARDS if args.board == 'all' else [args.board]
    source = ('module ha(input wire a,b, output wire sum,carry); '
              'assign sum=a^b; assign carry=a&b; endmodule')
    server.WORK.mkdir(parents=True, exist_ok=True)
    for board in boards:
        with tempfile.TemporaryDirectory(prefix='smoke-', dir=server.WORK) as folder:
            lab = server.Lab(Path(folder))
            try:
                lab.select_board(board)
                assert lab.synthesize(source, 'ha')['backend'] == 'openxc7'
                assert lab.implement(3)['phase'] == 'implemented'
                result = lab.generate()
                assert result['phase'] == 'bitstream'
                assert (lab.work / 'design.bit').stat().st_size > 100_000
                if args.require_fabric:
                    assert result['fabricStatus'] == 'ready', result['fabricError']
                    lab.fabric.control({'running': False})
                    for switches, expected in ((0, 0), (1, 1), (2, 1), (3, 2)):
                        lab.control({'switches': switches})
                        actual = sum(value << i for i, value in enumerate(lab.fabric.snapshot()['leds']))
                        assert actual == expected, (board, switches, actual, expected)
                print(f'{board}: real bitstream build' + (' and decoded truth table' if args.require_fabric else '') + ' passed', flush=True)
            finally:
                lab.close()
