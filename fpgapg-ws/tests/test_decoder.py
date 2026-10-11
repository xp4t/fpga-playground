"""Synthetic physical-pad netlists verify board-specific fabric wiring."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import server

sys.path.insert(0, str(server.ROOT / 'sim-core'))
sys.path.insert(0, str(server.ROOT / 'bitstream-decode'))
from runtime import BoardRuntime
import pipeline


class DecoderTests(unittest.TestCase):
    def test_pipeline_uses_selected_part_and_readonly_cache_for_each_stage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / 'part.db'
            cache.touch()
            with mock.patch.object(pipeline, 'run_stage') as stage:
                pipeline.convert(root / 'design.bit', root / 'decoded', part='xc7a100tcsg324-1',
                                 connection_database=cache, db_root=root, yosys_bin='/usr/bin/yosys')
            self.assertIn('xc7a100tcsg324-1', stage.call_args_list[0].args[1])
            bels = stage.call_args_list[1].args[1]
            self.assertEqual(bels[bels.index('--part') + 1], 'xc7a100tcsg324-1')
            self.assertEqual(bels[bels.index('--connection_database') + 1], str(cache))
            self.assertEqual(stage.call_args_list[2].args[1][0], '/usr/bin/yosys')

    def test_synchronous_fabric_load_widths_button_aliases_and_leds_for_all_boards(self):
        for board, config in server.BOARDS.items():
            with self.subTest(board=board), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                part = root / config['part']
                part.mkdir()
                pins = ['sw[0]', 'btn[0]' if board == 'arty_a7_100t' else 'btnC', 'led[0]']
                xdc = root / 'board.xdc'
                xdc.write_text('\n'.join(f'set_property PACKAGE_PIN P{i} [get_ports {{{name}}}]' for i, name in enumerate(pins)))
                (part / 'package_pins.csv').write_text('site,pin\nIOB_X0Y0,P0\nIOB_X0Y1,P1\nIOB_X0Y2,P2\n')
                def converter(capture, output):
                    (output / 'design.decode.json').write_text(json.dumps({
                        'unknown_bits': 0, 'missing_tile_types': [], 'complete': True}))
                    ports = {'IOB_X0Y0_IPAD': {'direction': 'input', 'bits': [2]},
                             'IOB_X0Y1_IPAD': {'direction': 'input', 'bits': [3]},
                             'IOB_X0Y2_OPAD': {'direction': 'output', 'bits': [2]}}
                    netlist = output / 'netlist.json'
                    netlist.write_text(json.dumps({'modules': {'top': {'ports': ports, 'cells': {}}}}))
                    return netlist
                runtime = BoardRuntime(root / 'programs', part=config['part'], master_xdc=xdc,
                                       switch_count=config['switches'], led_count=config['leds'],
                                       buttons=config['buttons'], db_root=root, converter=converter,
                                       button_ports={'btnC': 'btn[0]'} if board == 'arty_a7_100t' else None)
                try:
                    runtime.programmed(b'synthetic configuration', synchronous=True)
                    self.assertEqual(runtime.snapshot()['status'], 'ready', runtime.snapshot()['error'])
                    runtime.control({'switches': 1, 'button': 'btnC', 'value': True, 'running': False})
                    snapshot = runtime.snapshot()
                    self.assertEqual(snapshot['part'], config['part'])
                    self.assertEqual(len(snapshot['leds']), config['leds'])
                    self.assertEqual(len(snapshot['switches']), config['switches'])
                    self.assertEqual(snapshot['leds'][0], 1)
                    self.assertEqual(runtime.board.simulator.read('IOB_X0Y1_IPAD'), 1)
                    with self.assertRaises(ValueError):
                        runtime.control({'switches': 1 << config['switches']})
                finally:
                    runtime.close(wait=True)
