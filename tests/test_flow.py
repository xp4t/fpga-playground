"""End-to-end tool checks for the local FPGA workbench."""

import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server


class LocalResourceTests(unittest.TestCase):
    def test_basys_assets_are_in_this_repository(self):
        self.assertEqual(server.BOARDS["basys3"]["xdc"],
                         server.ROOT / "boards/Basys-3-Master.xdc")
        self.assertTrue(server.BOARDS["basys3"]["xdc"].is_file())
        self.assertTrue((server.ROOT / "web/basys3.jpg").is_file())

    def test_decoder_accepts_a_standalone_database(self):
        module_path = server.ROOT / "bitstream-decode/pipeline.py"
        spec = importlib.util.spec_from_file_location("local_pipeline", module_path)
        pipeline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pipeline)
        with tempfile.TemporaryDirectory() as directory:
            part = Path(directory) / "xc7a35tcpg236-1"
            part.mkdir()
            (part / "part.json").touch()
            (part / "package_pins.csv").touch()
            with mock.patch.dict(os.environ, {"PRJXRAY_DB_ROOT": directory}):
                self.assertEqual(pipeline.database_root(), Path(directory))


@unittest.skipUnless(all(shutil.which(name) for name in ("yosys", "iverilog", "vvp")),
                     "Yosys and Icarus Verilog are required")
class FlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.original_work = server.WORK
        server.WORK = Path(self.directory.name)
        self.openxc7_patch = mock.patch.object(server, "openxc7_paths", return_value=None)
        self.openxc7_patch.start()
        self.lab = server.Lab()

    def tearDown(self):
        self.openxc7_patch.stop()
        server.WORK = self.original_work
        self.directory.cleanup()

    def test_counter_synthesis_switches_step_and_reset(self):
        self.assertEqual(self.lab.synthesize(server.DEFAULT_CODE, "counter")["phase"], "synthesized")
        preview = self.lab.implement(1)
        self.assertEqual(preview["phase"], "preview" if not server.tool_path("vivado") else "implemented")
        self.assertEqual(preview["leds"], "0010")
        self.assertEqual(self.lab.control({"step": 1})["leds"], "0011")
        self.assertEqual(self.lab.control({"button": "btnC", "value": True})["leds"], "0000")

    def test_custom_combinational_design(self):
        source = "module lights(input wire [15:0] sw, output wire [15:0] led); assign led = sw ^ 16'hAAAA; endmodule"
        self.lab.synthesize(source, "lights")
        self.assertEqual(self.lab.implement(0x00FF)["leds"], "aa55")
        self.assertEqual(self.lab.control({"switches": 0x0F0F})["leds"], "a5a5")

    def test_detects_renamed_single_module(self):
        source = server.DEFAULT_CODE.replace("module counter", "module ha")
        result = self.lab.synthesize(source, "counter")
        self.assertEqual(result["phase"], "synthesized")
        self.assertEqual(result["top"], "ha")
        self.assertEqual(self.lab.implement(1)["leds"], "0010")

    def test_half_adder_ports_map_to_board(self):
        source = ("module ha(input wire a, input wire b, output wire sum, output wire carry); "
                  "assign sum=a^b; assign carry=a&b; endmodule")
        result = self.lab.synthesize(source, "counter")
        self.assertEqual(result["top"], "ha")
        self.assertEqual(result["buildTop"], "virtual_basys3_adapter")
        self.assertIn("SW0 → a", result["mappingSummary"])
        self.assertIn("LD1 ← carry", result["mappingSummary"])
        self.assertEqual(self.lab.implement(1)["leds"], "0001")
        self.assertEqual(self.lab.control({"switches": 3})["leds"], "0002")

    def test_multiple_modules_need_a_matching_top(self):
        source = ("module helper(input wire a, output wire b); assign b=a; endmodule\n"
                  "module ha(input wire [15:0] sw, output wire [15:0] led); assign led=sw; endmodule")
        with self.assertRaisesRegex(ValueError, "Choose one of: helper, ha"):
            self.lab.synthesize(source, "counter")

    def test_maps_partial_board_buses(self):
        source = "module short(input wire [7:0] sw, output wire [15:0] led); assign led = {8'b0, sw}; endmodule"
        self.assertEqual(self.lab.synthesize(source, "short")["buildTop"], "virtual_basys3_adapter")
        self.assertEqual(self.lab.implement(0x00FF)["leds"], "00ff")

    def test_rejects_more_than_sixteen_input_bits(self):
        source = "module wide(input wire [16:0] a, output wire y); assign y=a[0]; endmodule"
        with self.assertRaisesRegex(ValueError, "more than 16 switch input bits"):
            self.lab.synthesize(source, "wide")

    def test_board_selection_uses_arty_pins_and_four_bit_io(self):
        source = ("module ha(input wire a,b, output wire sum,carry); "
                  "assign sum=a^b; assign carry=a&b; endmodule")
        self.lab.synthesize(source, "ha")
        selected = self.lab.select_board("arty_a7_100t")
        self.assertEqual(selected["phase"], "ready")
        self.assertEqual(selected["switchCount"], 4)
        mapped = self.lab.synthesize(source, "ha")
        self.assertEqual(mapped["buildTop"], "virtual_board_adapter")
        xdc = (server.WORK / "board.xdc").read_text()
        self.assertIn("PACKAGE_PIN A8", xdc)
        self.assertIn("PACKAGE_PIN H5", xdc)
        self.assertEqual(self.lab.implement(3)["leds"], "0002")
        with self.assertRaisesRegex(ValueError, "4-bit"):
            self.lab.control({"switches": 16})

    def test_arty_button_bus_follows_button_controls(self):
        self.lab.select_board("arty_a7_100t")
        source = ("module arty(input wire [3:0] sw,btn, output wire [3:0] led); "
                  "assign led=sw^btn; endmodule")
        self.lab.synthesize(source, "arty")
        self.assertEqual(self.lab.implement(1)["leds"], "0001")
        self.assertEqual(self.lab.control({"button": "btnC", "value": True})["leds"], "0000")

    def test_nexys_constraints_use_100t_pin_names(self):
        self.lab.select_board("nexys_a7_100t")
        self.lab.synthesize(server.DEFAULT_CODE, "counter")
        xdc = (server.WORK / "board.xdc").read_text()
        self.assertIn("PACKAGE_PIN E3", xdc)
        self.assertIn("PACKAGE_PIN J15", xdc)
        self.assertIn("PACKAGE_PIN H17", xdc)
        self.assertIn("IOSTANDARD LVCMOS18 } [get_ports {sw[8]}]", xdc)

    def test_clock_divider_presets_and_one_hertz_preview(self):
        for hz, half_period in ((50_000_000, 1), (25_000_000, 2), (1, 50_000_000)):
            with self.subTest(hz=hz):
                result = self.lab.synthesize(server.DEFAULT_CODE, "counter", hz)
                self.assertEqual(result["clockActualHz"], hz)
                self.assertEqual(result["buildTop"], "virtual_clock_adapter")
                wrapper = (server.WORK / "clock_adapter.v").read_text()
                self.assertIn("`ifdef FPGA_PREVIEW", wrapper)
                self.assertIn(".clk(user_clk)", wrapper)
                if half_period > 1:
                    self.assertIn(f"'d{half_period - 1}", wrapper)
                self.assertIn("create_clock -period 10.000", (server.WORK / "board.xdc").read_text())
                self.assertEqual(self.lab.implement(1)["leds"], "0010")
        normal = self.lab.synthesize(server.DEFAULT_CODE, "counter", 100_000_000)
        self.assertEqual(normal["buildTop"], "counter")
        self.assertFalse((server.WORK / "clock_adapter.v").exists())

    def test_clock_frequency_validation(self):
        for hz in (0, -1, 100_000_001, 1.5, "1"):
            with self.subTest(hz=hz), self.assertRaisesRegex(ValueError, "Clock must be"):
                self.lab.synthesize(server.DEFAULT_CODE, "counter", hz)


@unittest.skipUnless(all(server.openxc7_paths(board) for board in server.BOARDS) and
                     all(shutil.which(name) for name in
                     ("yosys", "iverilog", "vvp")), "Open XC7 Basys3 toolchain is required")
class OpenXC7FlowTests(unittest.TestCase):
    def test_half_adder_reaches_real_bitstream_on_each_board(self):
        source = ("module ha(input wire a, b, output wire sum, carry); "
                  "assign sum=a^b; assign carry=a&b; endmodule")
        for board in server.BOARDS:
            with self.subTest(board=board), tempfile.TemporaryDirectory() as directory:
                original_work = server.WORK
                server.WORK = Path(directory)
                lab = server.Lab()
                try:
                    lab.select_board(board)
                    self.assertEqual(lab.synthesize(source, "counter")["backend"], "openxc7")
                    routed = lab.implement(3)
                    self.assertEqual(routed["phase"], "implemented")
                    self.assertEqual(routed["leds"], "0002")
                    self.assertTrue((server.WORK / "routed.json").is_file())
                    result = lab.generate()
                    self.assertEqual(result["phase"], "bitstream")
                    self.assertGreater((server.WORK / "design.bit").stat().st_size, 100_000)
                finally:
                    if lab.fabric:
                        lab.fabric.close()
                    server.WORK = original_work

    def test_one_hertz_clock_reaches_real_bitstream(self):
        with tempfile.TemporaryDirectory() as directory:
            original_work = server.WORK
            server.WORK = Path(directory)
            lab = server.Lab()
            try:
                self.assertEqual(lab.synthesize(server.DEFAULT_CODE, "counter", 1)["clockActualHz"], 1)
                self.assertEqual(lab.implement(1)["phase"], "implemented")
                generated = lab.generate()
                self.assertEqual(generated["phase"], "bitstream")
                self.assertIsNone(generated["fabricStatus"])
                self.assertGreater((server.WORK / "design.bit").stat().st_size, 100_000)
            finally:
                if lab.fabric:
                    lab.fabric.close()
                server.WORK = original_work


if __name__ == "__main__":
    unittest.main()
