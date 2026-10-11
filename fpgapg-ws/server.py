#!/usr/bin/env python3
"""Local browser workbench for Basys3 RTL, simulation, and Artix-7 builds."""

import argparse
from contextlib import contextmanager, nullcontext
import hashlib
from http.cookies import SimpleCookie, CookieError
import secrets
import queue
import hmac
from collections import deque
import tempfile
import time
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
WORK = ROOT / ".work"
MAX_SOURCE = 100_000
MAX_REQUEST = MAX_SOURCE * 2 + 10_000
MAX_PREVIEW_CYCLES = 4096
BOARD_CLOCK_HZ = 100_000_000
BUTTONS = ("btnC", "btnU", "btnD", "btnL", "btnR")
BOARDS = {
    "basys3": {"name": "Basys 3", "part": "xc7a35tcpg236-1", "device": "xc7a35tcpg236",
               "switches": 16, "leds": 16, "buttons": BUTTONS,
               "xdc": ROOT / "boards/Basys-3-Master.xdc"},
    "nexys_a7_100t": {"name": "Nexys A7 100T", "part": "xc7a100tcsg324-1",
                        "device": "xc7a100tcsg324", "switches": 16, "leds": 16,
                        "buttons": BUTTONS, "xdc": ROOT / "boards/Nexys-A7-100T-Master.xdc"},
    "arty_a7_100t": {"name": "Arty A7 100T", "part": "xc7a100tcsg324-1",
                       "device": "xc7a100tcsg324", "switches": 4, "leds": 4,
                       "buttons": BUTTONS[:4], "xdc": ROOT / "boards/Arty-A7-100-Master.xdc"},
}
DEFAULT_CODE = """`timescale 1ns/1ps
module counter (
    input wire clk,
    input wire btnC,
    input wire [15:0] sw,
    output wire [15:0] led
);
    reg [7:0] count = 0;
    always @(posedge clk) begin
        if (btnC) count <= 0;
        else if (sw[0]) count <= count + 1'b1;
    end
    assign led = {8'b0, count};
endmodule
"""


def tool_path(name):
    if name == "vivado":
        configured = os.environ.get("VIVADO_BIN")
        if configured and Path(configured).is_file():
            return configured
    if name in ("nextpnr-xilinx", "fasm2frames", "xc7frames2bit"):
        roots = [Path(os.environ["OPENXC7_DIR"])] if os.environ.get("OPENXC7_DIR") else [
            ROOT / ".tools/openxc7", Path.home() / ".apio/packages/openxc7"]
        for root in roots:
            candidate = root / "bin" / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return shutil.which(name)


def openxc7_paths(board="basys3"):
    """Return the installed open XC7 tools and data for the selected board."""
    config = BOARDS[board]
    roots = [Path(os.environ["OPENXC7_DIR"])] if os.environ.get("OPENXC7_DIR") else [
        ROOT / ".tools/openxc7", Path.home() / ".apio/packages/openxc7"]
    for root in roots:
        chipdb = root / "chipdb" / f"{config['device']}.bin"
        family = root / "share/nextpnr/external/prjxray-db/artix7"
        part_file = family / config["part"] / "part.yaml"
        binaries = {name: root / "bin" / name for name in
                    ("nextpnr-xilinx", "fasm2frames", "xc7frames2bit")}
        if chipdb.is_file() and part_file.is_file() and all(
                path.is_file() and os.access(path, os.X_OK) for path in binaries.values()):
            return {"root": root, "chipdb": chipdb, "family": family,
                    "part_file": part_file, **binaries}
    return None


# Shared runner applies limits to the entire tool process group, including descendants.
from build_runner import run, run_to_file, BUILD_TIMEOUT, workspace_size, DISK_LIMIT


def tcl_path(path):
    return "{" + str(path.resolve()).replace("\\", "/") + "}"


def resolve_top(code, requested):
    # The browser may still hold the previous design's top name after an edit.
    uncommented = re.sub(r"/\*.*?\*/|//[^\n]*", "", code, flags=re.DOTALL)
    modules = list(dict.fromkeys(re.findall(
        r"\bmodule\s+(?:(?:automatic|static)\s+)?([A-Za-z_][A-Za-z_0-9]*)\b",
        uncommented)))
    if len(modules) == 1:
        return modules[0]
    if requested in modules:
        return requested
    if modules:
        raise ValueError("Top module {!r} not found. Choose one of: {}".format(
            requested, ", ".join(modules)))
    if not isinstance(requested, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", requested):
        raise ValueError("Enter a valid top module name")
    return requested


def xdc_port(board, port):
    if board == "basys3":
        return port
    if board == "nexys_a7_100t":
        if port == "clk":
            return "CLK100MHZ"
        if port in BUTTONS:
            return port.upper()
        if port == "dp":
            return "DP"
        if port.startswith("seg["):
            return "C" + "ABCDEFG"[int(port[4:-1])]
        if port.startswith(("sw[", "led[", "an[")):
            return port.upper()
    if board == "arty_a7_100t":
        if port == "clk":
            return "CLK100MHZ"
        if port in BUTTONS[:4]:
            return f"btn[{BUTTONS.index(port)}]"
        if port.startswith(("sw[", "led[", "btn[")):
            return port
    raise ValueError(f"No {BOARDS[board]['name']} pin mapping for {port}")


def make_constraints(ports, board="basys3", *, work):
    config = BOARDS[board]
    lines = config["xdc"].read_text().splitlines()
    desired = set()
    for name, info in ports.items():
        if name in ("clk", *BUTTONS, "dp"):
            if name in BUTTONS and name not in config["buttons"]:
                raise ValueError(f"{config['name']} has no {name} button")
            if name == "dp" and board == "arty_a7_100t":
                raise ValueError(f"{config['name']} has no seven-segment display")
            if len(info["bits"]) != 1:
                raise ValueError(f"{name} must be a single bit")
            desired.add(name)
        elif name in ("sw", "led", "seg", "an"):
            limit = {"sw": config["switches"], "led": config["leds"],
                     "seg": 0 if board == "arty_a7_100t" else 7,
                     "an": 0 if board == "arty_a7_100t" else
                     (8 if board == "nexys_a7_100t" else 4)}[name]
            if len(info["bits"]) > limit:
                raise ValueError(f"{name} exceeds the {config['name']} {limit}-bit port")
            desired.update(f"{name}[{i}]" for i in range(len(info["bits"])))
        elif name == "btn" and board == "arty_a7_100t":
            if len(info["bits"]) > 4:
                raise ValueError("btn exceeds the Arty A7 4-bit button port")
            desired.update(f"btn[{i}]" for i in range(len(info["bits"])))
        else:
            raise ValueError(f"Unsupported {config['name']} port: {name}. Use clk, sw, led, and available buttons.")
    aliases = {xdc_port(board, port): port for port in desired}
    if len(aliases) != len(desired):
        raise ValueError("Two top-level ports map to the same physical board pin")
    found = set()
    selected = []
    for line in lines:
        if not line.startswith("#set_property -dict"):
            continue
        pin = re.search(r"\bPACKAGE_PIN\s+([A-Z]+[0-9]+)\b", line)
        standard = re.search(r"\bIOSTANDARD\s+([A-Z0-9]+)\b", line)
        target = re.search(r"\[get_ports\s+(?:\{\s*([^}]+?)\s*\}|([^\]\s]+))\s*\]", line)
        if pin and standard and target:
            source_port = (target.group(1) or target.group(2)).strip()
            if source_port in aliases:
                port = aliases[source_port]
                selected.append(f"set_property -dict {{ PACKAGE_PIN {pin.group(1)} "
                                f"IOSTANDARD {standard.group(1)} }} [get_ports {{{port}}}]")
                found.add(port)
    missing = desired - found
    if missing:
        raise ValueError(f"No {config['name']} pin mapping for " + ", ".join(sorted(missing)))
    if "clk" in ports:
        selected.append("create_clock -period 10.000 [get_ports clk]")
    (work / "board.xdc").write_text("\n".join(selected) + "\n")


def clock_divider(clock_hz):
    """Return the nearest whole input-clock half-period and resulting output rate."""
    if type(clock_hz) is not int or not 1 <= clock_hz <= BOARD_CLOCK_HZ:
        raise ValueError("Clock must be an integer frequency from 1 Hz to 100 MHz")
    if clock_hz == BOARD_CLOCK_HZ:
        return None, BOARD_CLOCK_HZ
    half_period = max(1, round(BOARD_CLOCK_HZ / (2 * clock_hz)))
    return half_period, BOARD_CLOCK_HZ / (2 * half_period)


def prepare_clock_top(top, ports, clock_hz, *, work):
    """Divide the 100 MHz board input before it reaches the user's clk port."""
    half_period, actual_hz = clock_divider(clock_hz)
    if half_period is None or "clk" not in ports:
        (work / "clock_adapter.v").unlink(missing_ok=True)
        return top, actual_hz
    adapter = "virtual_clock_adapter"
    if top == adapter:
        raise ValueError(f"{adapter} is reserved for the generated clock adapter")
    declarations = []
    connections = []
    for name, info in ports.items():
        width = len(info["bits"])
        span = f"[{width - 1}:0] " if width > 1 else ""
        declarations.append(f"{info['direction']} wire {span}{name}")
        connections.append(f".{name}({'user_clk' if name == 'clk' else name})")
    if half_period == 1:
        divider = "reg divided_clk = 0;\nalways @(posedge clk) divided_clk <= ~divided_clk;"
    else:
        width = max(1, (half_period - 1).bit_length())
        divider = (f"reg [{width - 1}:0] clock_count = 0;\n"
                   "reg divided_clk = 0;\n"
                   "always @(posedge clk) begin\n"
                   f"    if (clock_count == {width}'d{half_period - 1}) begin\n"
                   "        clock_count <= 0;\n"
                   "        divided_clk <= ~divided_clk;\n"
                   "    end else clock_count <= clock_count + 1'b1;\n"
                   "end")
    wrapper = (f"module {adapter}(\n    " + ",\n    ".join(declarations) + "\n);\n"
               "`ifdef FPGA_PREVIEW\nwire user_clk = clk;\n`else\n"
               f"{divider}\nwire user_clk = divided_clk;\n`endif\n"
               f"{top} design_instance (" + ", ".join(connections) + ");\nendmodule\n")
    (work / "clock_adapter.v").write_text(wrapper)
    return adapter, actual_hz


def prepare_board_top(user_top, ports, board="basys3", *, work):
    """Keep matching board ports, or wrap smaller designs onto switches and LEDs."""
    config = BOARDS[board]
    switch_count, led_count = config["switches"], config["leds"]
    allowed = ("sw", "led", "clk", *config["buttons"])
    if board == "arty_a7_100t":
        allowed += ("btn",)
    else:
        allowed += ("seg", "dp", "an")
    native = (len(ports.get("sw", {}).get("bits", [])) == switch_count and
              ports.get("sw", {}).get("direction") == "input" and
              len(ports.get("led", {}).get("bits", [])) == led_count and
              ports.get("led", {}).get("direction") == "output" and
              all(name in allowed for name in ports))
    if native:
        (work / "adapter.v").unlink(missing_ok=True)
        return (user_top, ports,
                f"Direct {config['name']} ports: sw[{switch_count-1}:0] → SW; "
                f"led[{led_count-1}:0] → LD.")

    adapter = "virtual_basys3_adapter" if board == "basys3" else "virtual_board_adapter"
    if user_top == adapter:
        raise ValueError(f"{adapter} is reserved for the generated board adapter")
    switch_offset = led_offset = 0
    connections = []
    output_wires = []
    inputs = [f"input wire [{switch_count-1}:0] sw",
              f"output wire [{led_count-1}:0] led"]
    input_map, output_map = [], []
    led_pieces = []
    for name, info in ports.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
            raise ValueError(f"Unsupported port name: {name}")
        if board == "arty_a7_100t" and name in ("seg", "dp", "an"):
            raise ValueError("Arty A7 has no seven-segment display")
        width = len(info["bits"])
        direction = info["direction"]
        if direction == "input":
            if name == "clk" and width == 1:
                inputs.append("input wire clk")
                expression = "clk"
                input_map.append("CLK → clk")
            elif name in BUTTONS and width == 1:
                if name not in config["buttons"]:
                    raise ValueError(f"{config['name']} has no {name} button")
                inputs.append(f"input wire {name}")
                expression = name
                input_map.append(f"{xdc_port(board, name)} → {name}")
            elif name == "btn" and board == "arty_a7_100t" and width == 4:
                inputs.append("input wire [3:0] btn")
                expression = "btn"
                input_map.append("BTN[3:0] → btn")
            elif name == "sw" and board == "arty_a7_100t" and width == 16:
                if switch_offset:
                    raise ValueError("This design has more than 4 switch input bits")
                expression = "{12'b0, sw}"
                switch_offset = 4
                input_map.append("SW[3:0] → sw[3:0]; sw[15:4] tied low")
            else:
                if switch_offset + width > switch_count:
                    raise ValueError(f"This design has more than {switch_count} switch input bits")
                high = switch_offset + width - 1
                expression = f"sw[{switch_offset}]" if width == 1 else f"sw[{high}:{switch_offset}]"
                input_map.append(f"SW{switch_offset} → {name}" if width == 1 else
                                 f"SW[{high}:{switch_offset}] → {name}")
                switch_offset += width
            connections.append(f".{name}({expression})")
        elif direction == "output":
            wide_arty_led = name == "led" and board == "arty_a7_100t" and width == 16
            used_width = 4 if wide_arty_led else width
            if led_offset + used_width > led_count:
                raise ValueError(f"This design has more than {led_count} LED output bits")
            wire = f"mapped_{name}"
            output_wires.append(f"wire {f'[{width - 1}:0] ' if width > 1 else ''}{wire};")
            connections.append(f".{name}({wire})")
            high = led_offset + used_width - 1
            if wide_arty_led:
                led_pieces.append(f"{wire}[3:0]")
                output_map.append("LD[3:0] ← led[3:0]; led[15:4] not connected")
            else:
                led_pieces.append(wire)
                output_map.append(f"LD{led_offset} ← {name}" if width == 1 else
                                  f"LD[{high}:{led_offset}] ← {name}")
            led_offset += used_width
        else:
            raise ValueError(f"Port {name} is {direction}; only input and output ports are supported")
    if not output_wires:
        raise ValueError("The top module needs at least one output to show on the board")
    pieces = list(reversed(led_pieces))
    if led_offset < led_count:
        pieces.insert(0, f"{led_count - led_offset}'b0")
    wrapper = (f"module {adapter}(\n    " + ",\n    ".join(inputs) + "\n);\n" +
               "\n".join(output_wires) + "\n" +
               f"{user_top} design_instance (" + ", ".join(connections) + ");\n" +
               "assign led = {" + ", ".join(pieces) + "};\nendmodule\n")
    (work / "adapter.v").write_text(wrapper)
    board_ports = {"sw": {"bits": list(range(switch_count)), "direction": "input"},
                   "led": {"bits": list(range(led_count)), "direction": "output"}}
    if "input wire clk" in inputs:
        board_ports["clk"] = {"bits": [0], "direction": "input"}
    for name in config["buttons"]:
        if f"input wire {name}" in inputs:
            board_ports[name] = {"bits": [0], "direction": "input"}
    if "input wire [3:0] btn" in inputs:
        board_ports["btn"] = {"bits": list(range(4)), "direction": "input"}
    summary = "; ".join(input_map + output_map) + "."
    return adapter, board_ports, summary


def write_vivado_scripts(top, board="basys3", *, work):
    part = BOARDS[board]["part"]
    source = tcl_path(work / "design.v")
    xdc = tcl_path(work / "board.xdc")
    base = f"create_project -in_memory -part {part}\nread_verilog -sv {source}\n"
    for name in ("adapter.v", "clock_adapter.v"):
        if (work / name).is_file():
            base += f"read_verilog {tcl_path(work / name)}\n"
    (work / "synth.tcl").write_text(base + f"synth_design -top {top} -part {part}\n"
                                      "write_checkpoint -force synth.dcp\nreport_utilization -file synth.rpt\nexit 0\n")
    (work / "implement.tcl").write_text(
        "open_checkpoint synth.dcp\n" + f"read_xdc {xdc}\n"
        + "set_property CFGBVS VCCO [current_design]\n"
        + "set_property CONFIG_VOLTAGE 3.3 [current_design]\n"
        + "opt_design\nplace_design\nroute_design\n"
        + "write_checkpoint -force routed.dcp\n"
        + "report_timing_summary -file timing.rpt\nexit 0\n")
    (work / "bitstream.tcl").write_text(
        "open_checkpoint routed.dcp\n"
        "set_property BITSTREAM.STARTUP.STARTUPCLK JtagClk [current_design]\n"
        "set_property BITSTREAM.GENERAL.COMPRESS FALSE [current_design]\n"
        "write_bitstream -force design.bit\nexit 0\n")


class Lab:
    def __init__(self, work):
        self.work = Path(work).resolve()
        self.board = "basys3"
        self.clock_hz = BOARD_CLOCK_HZ
        self.clock_actual_hz = BOARD_CLOCK_HZ
        self.work.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("design.v", "adapter.v", "clock_adapter.v", "board.xdc", "netlist.json", "user_netlist.json",
                     "xc7_netlist.json", "routed.json", "design.fasm", "design.frames", "route.log",
                     "synth.rpt", "timing.rpt",
                     "routed.dcp", "synth.dcp", "design.bit", "sim.vvp", "tb.v", "stimulus.hex"):
            (self.work / name).unlink(missing_ok=True)
        self.lock = threading.RLock()
        self.code = DEFAULT_CODE
        self.top = "counter"
        self.build_top = "counter"
        self.mapping_summary = "Direct Basys3 ports: sw[15:0] → SW[15:0]; led[15:0] → LD[15:0]."
        self.ports = {}
        self.phase = "ready"
        self.log = "Ready. Edit the Verilog, then synthesize."
        self.switches = 0
        self.buttons = {name: 0 for name in BUTTONS}
        self.samples = []
        self.leds = "x" * 16
        self.gates = None
        self.bitstream = False
        self.backend = None
        self.fabric = None
        self.fabric_armed = False
        self.active_job = None
        self.request_busy = False
        self.deadline = None
        self.progress = None

    def run(self, command, timeout=120):
        if self.progress:
            self.progress(f"Running {Path(command[0]).name}…\n")
        return run(self.sandbox_command(command), cwd=self.work, timeout=timeout,
                   deadline=self.deadline, progress=self.progress)

    def run_to_file(self, command, output_file, timeout=120):
        if self.progress:
            self.progress(f"Running {Path(command[0]).name}…\n")
        return run_to_file(self.sandbox_command(command), output_file, cwd=self.work, timeout=timeout,
                           deadline=self.deadline, progress=self.progress)

    def sandbox_command(self, command):
        # HDL can read files with `include/$readmemh/$fopen. Hide sibling workspaces
        # and the legacy shared output before exposing only this session's files.
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise ValueError("Bubblewrap (bwrap) is required to isolate FPGA builds")
        (self.work / "tmp").mkdir(exist_ok=True)
        args = [bwrap, "--die-with-parent", "--unshare-all", "--cap-drop", "ALL"]
        # Start with an empty filesystem. Only public system/toolchain files and
        # this lab's writable directory are visible, never host home or siblings.
        roots = [Path(name) for name in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")]
        roots.append(Path(command[0]).resolve().parent.parent)
        paths = openxc7_paths(self.board)
        if paths:
            roots.extend((paths["root"].resolve(), paths["family"].resolve()))
        roots.extend(getattr(self, "decoder_mounts", []))
        for root in dict.fromkeys(roots):
            if root.exists():
                args += ["--ro-bind", str(root), str(root)]
        args += ["--proc", "/proc", "--dev", "/dev", "--dir", "/tmp", "--dir", "/var/tmp",
                 "--bind", str(self.work / "tmp"), "/tmp",
                 "--bind", str(self.work / "tmp"), "/var/tmp", "--bind", str(self.work), str(self.work),
                 "--chdir", str(self.work), "--clearenv", "--setenv", "PATH", os.defpath,
                 "--setenv", "HOME", "/tmp", "--setenv", "LANG", "C.UTF-8",
                 "--setenv", "OMP_NUM_THREADS", "2", "--", *command]
        return args

    def close(self):
        if self.fabric:
            self.fabric.close(wait=True)

    def select_board(self, board):
        if board not in BOARDS:
            raise ValueError("Choose Basys 3, Nexys A7 100T, or Arty A7 100T")
        if board == self.board:
            return self.snapshot()
        if self.fabric:
            self.fabric.close()
            self.fabric = None
            self.fabric_armed = False
        self.board = board
        self.phase, self.backend, self.bitstream = "ready", None, False
        self.mapping_summary = f"Synthesize to map this design to {BOARDS[board]['name']}."
        self.ports, self.samples, self.gates = {}, [], None
        self.switches, self.leds = 0, "xxxx"
        self.buttons = {name: 0 for name in BUTTONS}
        for name in ("adapter.v", "clock_adapter.v", "board.xdc", "netlist.json", "user_netlist.json",
                     "xc7_netlist.json", "routed.json", "design.fasm", "design.frames",
                     "route.log", "synth.rpt", "timing.rpt", "routed.dcp", "synth.dcp",
                     "design.bit", "sim.vvp", "tb.v", "stimulus.hex"):
            (self.work / name).unlink(missing_ok=True)
        self.log = f"Selected {BOARDS[board]['name']} ({BOARDS[board]['part']}). Synthesize to build."
        return self.snapshot()

    def snapshot(self):
        config = BOARDS[self.board]
        fabric_status = None
        fabric_error = None
        fabric_waveform = None
        leds = self.leds
        if self.fabric:
            actual = self.fabric.snapshot()
            fabric_status = actual["status"]
            fabric_error = actual["error"]
            if fabric_status == "ready":
                if not self.fabric_armed:
                    catalog = self.fabric.logic_catalog()
                    available = {item["id"] for item in catalog}
                    probes = [name for name in ("pin:sw[0]", "pin:sw[1]",
                              *(f"pin:led[{i}]" for i in range(8))) if name in available]
                    if probes:
                        self.fabric.logic_control({"action": "configure", "probes": probes,
                                                   "depth": 256, "trigger": {"mode": "immediate"}})
                        self.fabric.logic_control({"action": "arm"})
                    self.fabric_armed = True
                bits = actual["leds"]
                if all(bit in (0, 1) for bit in bits):
                    leds = f"{sum(bit << i for i, bit in enumerate(bits)):04x}"
                capture = self.fabric.logic_snapshot()
                if capture:
                    labels = {item["id"]: item["label"] for item in self.fabric.logic_catalog()}
                    fabric_waveform = {"signals": [labels.get(item, item) for item in capture["probes"]],
                                       "samples": [item["values"] for item in capture["samples"][-128:]]}
        artifact_names = [name for name in ("design.v", "adapter.v", "clock_adapter.v", "board.xdc", "netlist.json",
                          "xc7_netlist.json", "routed.json", "design.fasm", "design.frames",
                          "route.log", "synth.rpt", "timing.rpt", "routed.dcp", "design.bit", "user_design.v", "testbench.v", "testbench.vcd")
                          if (self.work / name).is_file() and (name != "design.bit" or self.bitstream)]
        return {"phase": self.phase, "top": self.top, "buildTop": self.build_top,
                "mappingSummary": self.mapping_summary,
                "board": self.board, "boardName": config["name"], "part": config["part"],
                "clockHz": self.clock_hz, "clockActualHz": self.clock_actual_hz,
                "clockPort": "clk" in self.ports,
                "switchCount": config["switches"], "ledCount": config["leds"],
                "availableBoards": [{"id": key, "name": item["name"], "part": item["part"]}
                                    for key, item in BOARDS.items()],
                "boardButtons": [name for name in config["buttons"]
                                 if name in self.ports or
                                 (self.board == "arty_a7_100t" and "btn" in self.ports)],
                "switches": self.switches,
                "buttons": self.buttons, "leds": self.leds, "samples": self.samples[-128:],
                "cycles": len(self.samples), "gates": self.gates, "log": self.log[-12_000:],
                "tools": {**{name: bool(tool_path(name)) for name in
                             ("yosys", "iverilog", "vvp", "vivado")},
                          "openxc7": bool(openxc7_paths(self.board))},
                "backend": self.backend,
                "bitstream": self.bitstream, "download": "/api/artifact/design.bit" if self.bitstream else None,
                "preview": (self.work / "sim.vvp").exists(), "displayLeds": leds,
                "fabricStatus": fabric_status, "fabricError": fabric_error,
                "fabricWaveform": fabric_waveform, "artifacts": artifact_names}

    def synthesize(self, code, top, clock_hz=BOARD_CLOCK_HZ):
        if not isinstance(code, str) or not 1 <= len(code.encode("utf-8")) <= MAX_SOURCE:
            raise ValueError("Verilog source must be between 1 and 100,000 UTF-8 bytes")
        clock_divider(clock_hz)
        top = resolve_top(code, top)
        if not tool_path("yosys"):
            raise ValueError("Yosys is required for synthesis")
        self.code, self.top, self.clock_hz = code, top, clock_hz
        if self.fabric:
            self.fabric.close()
            self.fabric = None
            self.fabric_armed = False
        self.phase, self.bitstream, self.backend = "ready", False, None
        self.samples, self.gates, self.ports = [], None, {}
        for name in ("adapter.v", "clock_adapter.v", "board.xdc", "netlist.json", "user_netlist.json",
                     "xc7_netlist.json", "routed.json", "design.fasm", "design.frames", "route.log",
                     "synth.rpt", "timing.rpt",
                     "routed.dcp", "synth.dcp", "design.bit", "sim.vvp", "tb.v", "stimulus.hex"):
            (self.work / name).unlink(missing_ok=True)
        (self.work / "design.v").write_text(code)
        script = f"read_verilog -sv design.v; synth -top {top}; write_json user_netlist.json; stat"
        output = self.run([tool_path("yosys"), "-Q", "-T", "-p", script])
        design = json.loads((self.work / "user_netlist.json").read_text())
        module = design["modules"].get(top)
        if not module:
            raise ValueError(f"Top module {top} not found")
        self.build_top, self.ports, self.mapping_summary = prepare_board_top(
            top, module["ports"], self.board, work=self.work)
        self.build_top, self.clock_actual_hz = prepare_clock_top(
            self.build_top, self.ports, self.clock_hz, work=self.work)
        make_constraints(self.ports, self.board, work=self.work)
        write_vivado_scripts(self.build_top, self.board, work=self.work)
        self.gates = len(module.get("cells", {}))
        if self.build_top != top:
            sources = " ".join(name for name in ("design.v", "adapter.v", "clock_adapter.v")
                               if (self.work / name).is_file())
            build_script = (f"read_verilog -sv {sources}; "
                            f"synth -top {self.build_top}; write_json netlist.json; stat")
            output += "\n--- Board adapters ---\n" + self.run(
                [tool_path("yosys"), "-Q", "-T", "-p", build_script])
        else:
            (self.work / "netlist.json").write_bytes((self.work / "user_netlist.json").read_bytes())
        if tool_path("vivado"):
            output += "\n--- Vivado synthesis ---\n" + self.run(
                [tool_path("vivado"), "-mode", "batch", "-source", "synth.tcl", "-nojournal", "-nolog"], timeout=900)
            self.backend = "vivado"
        elif openxc7_paths(self.board):
            output += "\n--- Yosys XC7 technology mapping ---\n" + self.synthesize_xc7()
            self.backend = "openxc7"
        self.phase = "synthesized"
        self.log = (f"Synthesis passed for {top}. {self.gates} mapped cells.\n"
                    f"Board mapping: {self.mapping_summary}\n" + output[-11_000:])
        return self.snapshot()

    def synthesize_xc7(self):
        sources = " ".join(name for name in ("design.v", "adapter.v", "clock_adapter.v")
                           if (self.work / name).is_file())
        script = (f"read_verilog -sv {sources}; synth_xilinx -family xc7 "
                  f"-top {self.build_top}; write_json xc7_netlist.json; stat")
        output = self.run([tool_path("yosys"), "-Q", "-T", "-p", script], timeout=900)
        return output

    def compile_preview(self):
        if not tool_path("iverilog") or not tool_path("vvp"):
            raise ValueError("Icarus Verilog (iverilog and vvp) is required for board preview")
        connections = []
        for port in self.ports:
            if port in ("clk", "sw", "led", "btn", *BUTTONS):
                connections.append(f".{port}({port})")
            elif port in ("seg", "an", "dp"):
                connections.append(f".{port}()")
        config = BOARDS[self.board]
        decl = ("reg clk=0; reg [15:0] sw=0; "
                f"wire [{config['leds']-1}:0] led; reg [3:0] btn=0;\n")
        decl += " ".join(f"reg {name}=0;" for name in BUTTONS)
        testbench = f"""`timescale 1ns/1ps
module tb;
{decl}
reg [20:0] stimulus[0:{MAX_PREVIEW_CYCLES - 1}];
integer i, n;
{self.build_top} uut ({', '.join(connections)});
initial begin
    if (!$value$plusargs("N=%d", n)) n=0;
    $readmemh("stimulus.hex", stimulus);
    for (i=0; i<n; i=i+1) begin
        {{btnR, btnL, btnD, btnU, btnC, sw}} = stimulus[i];
        btn = {{btnL, btnD, btnU, btnC}};
        #5; clk=1; #1;
        $display("@@SAMPLE %0d %h %h %h", i, sw, led, {{btnR, btnL, btnD, btnU, btnC}});
        #4; clk=0;
    end
    $finish;
end
endmodule
"""
        (self.work / "tb.v").write_text(testbench)
        sources = [name for name in ("design.v", "adapter.v", "clock_adapter.v")
                   if (self.work / name).is_file()]
        self.run([tool_path("iverilog"), "-g2012", "-DFPGA_PREVIEW", "-s", "tb", "-o", "sim.vvp", *sources, "tb.v"])

    def simulate(self):
        if not (self.work / "sim.vvp").exists():
            self.compile_preview()
        history = self.samples
        vectors = [((sample["buttons"] & 31) << 16) | sample["switches"] for sample in history]
        (self.work / "stimulus.hex").write_text("\n".join(f"{value:06x}" for value in vectors) + "\n")
        output = self.run([tool_path("vvp"), "sim.vvp", f"+N={len(vectors)}"], timeout=30)
        parsed = [line for line in output.splitlines() if line.startswith("@@SAMPLE ")]
        if len(parsed) != len(vectors):
            raise ValueError("Simulation ended before all samples were produced.\n" + output[-3000:])
        for sample, line in zip(history, parsed):
            _, _, _, led, _ = line.split()
            sample["leds"] = led.lower().zfill(4)[-4:]
        self.samples = history
        self.leds = history[-1]["leds"] if history else "xxxx"

    def add_sample(self):
        if len(self.samples) >= MAX_PREVIEW_CYCLES:
            raise ValueError(f"Preview reached {MAX_PREVIEW_CYCLES} cycles. Choose Implement to restart the capture.")
        button_word = sum(value << index for index, value in enumerate(self.buttons.values()))
        self.samples.append({"switches": self.switches, "buttons": button_word, "leds": "xxxx"})
        self.simulate()

    def implement(self, switches):
        if self.phase not in ("synthesized", "preview", "implemented", "bitstream"):
            raise ValueError("Synthesize the source first")
        max_switch = (1 << BOARDS[self.board]["switches"]) - 1
        if type(switches) is not int or not 0 <= switches <= max_switch:
            raise ValueError(f"Switches must be a {BOARDS[self.board]['switches']}-bit number")
        if self.fabric:
            self.fabric.close()
            self.fabric = None
            self.fabric_armed = False
        self.bitstream = False
        self.phase = "synthesized"
        for name in ("design.bit", "routed.dcp", "timing.rpt", "routed.json",
                     "design.fasm", "design.frames", "route.log"):
            (self.work / name).unlink(missing_ok=True)
        self.switches = switches
        self.buttons = {name: 0 for name in BUTTONS}
        self.samples = []
        self.compile_preview()
        for _ in range(16):
            button_word = 0
            self.samples.append({"switches": switches, "buttons": button_word, "leds": "xxxx"})
        self.simulate()
        if self.backend == "vivado":
            output = self.run([tool_path("vivado"), "-mode", "batch", "-source", "implement.tcl",
                          "-nojournal", "-nolog"], timeout=1800)
            self.phase = "implemented"
            self.log = "Placement and routing completed.\n" + output[-11_000:]
        elif openxc7_paths(self.board):
            paths = openxc7_paths(self.board)
            if not (self.work / "xc7_netlist.json").is_file():
                self.synthesize_xc7()
            output = self.run([str(paths["nextpnr-xilinx"]), "--chipdb", str(paths["chipdb"]),
                          "--xdc", "board.xdc", "--json", "xc7_netlist.json",
                          "--write", "routed.json", "--fasm", "design.fasm",
                          "--router", "router2"], timeout=1800)
            (self.work / "route.log").write_text(output)
            if not (self.work / "design.fasm").is_file() or not (self.work / "routed.json").is_file():
                raise ValueError("nextpnr finished without a routed design and FASM file")
            self.backend = "openxc7"
            self.phase = "implemented"
            self.log = "Open XC7 placement and routing completed.\n" + output[-11_000:]
        else:
            self.phase = "preview"
            self.log = ("RTL board preview is running with the requested switch word. "
                        "Install the open XC7 Basys3 toolchain or Vivado for placement and routing.")
        return self.snapshot()

    def generate(self):
        if self.phase not in ("implemented", "bitstream"):
            raise ValueError("Complete placement and routing before generating a bitstream")
        if self.fabric:
            self.fabric.close()
            self.fabric = None
            self.fabric_armed = False
        self.phase, self.bitstream = "implemented", False
        (self.work / "design.bit").unlink(missing_ok=True)
        (self.work / "design.frames").unlink(missing_ok=True)
        if self.backend == "vivado":
            output = self.run([tool_path("vivado"), "-mode", "batch", "-source", "bitstream.tcl",
                          "-nojournal", "-nolog"], timeout=1200)
        elif self.backend == "openxc7":
            paths = openxc7_paths(self.board)
            if not paths:
                raise ValueError("Open XC7 tools or Basys3 chip database are missing")
            part = BOARDS[self.board]["part"]
            output = self.run_to_file([str(paths["fasm2frames"]), "--part", part,
                                  "--db-root", str(paths["family"]), "design.fasm"],
                                 "design.frames", timeout=300)
            output += self.run([str(paths["xc7frames2bit"]), "--part_file", str(paths["part_file"]),
                           "--part_name", part, "--frm_file", "design.frames",
                           "--output_file", "design.bit"], timeout=300)
        else:
            raise ValueError("No implementation backend is available")
        if not (self.work / "design.bit").is_file():
            raise ValueError("The bitstream tool did not produce design.bit")
        self.phase, self.bitstream = "bitstream", True
        self.log = "Artix-7 bitstream generated: design.bit\n" + output[-11_000:]
        if self.clock_hz != BOARD_CLOCK_HZ:
            self.log = "Bitstream generated. A divided clock uses RTL playback.\n" + self.log
            return self.snapshot()
        try:
            if importlib.util.find_spec("fasm") is None or importlib.util.find_spec("fasm2bels") is None:
                raise ModuleNotFoundError(
                    "Decoder needs fasm and fasm2bels. Install requirements-decoder.txt "
                    "in the Python environment running this server")
            for name in ("sim-core", "bitstream-decode"):
                path = str(ROOT / name)
                if path not in sys.path:
                    sys.path.insert(0, path)
            from runtime import BoardRuntime
            from pipeline import database_root
            config = BOARDS[self.board]
            database = database_root(config["part"])
            cache = ROOT / ".tools/decoder" / f"{config['part']}.db"
            if not cache.is_file():
                raise FileNotFoundError("Prepare the trusted decoder cache; see docs/DEPLOY.md")
            self.decoder_mounts = [ROOT / "bitstream-decode", Path(sys.prefix), database, cache]
            def convert(capture, output):
                self.run([sys.executable, str(ROOT / "bitstream-decode/pipeline.py"), str(capture),
                          "--output", str(output), "--part", config["part"], "--db-root", str(database),
                          "--connection-database", str(cache), "--yosys", tool_path("yosys")], timeout=1200)
                return output / "netlist.json"
            self.fabric = BoardRuntime(program_root=self.work / "programs", part=config["part"],
                                       master_xdc=self.work / "board.xdc", switch_count=config["switches"],
                                       led_count=config["leds"], buttons=config["buttons"], converter=convert,
                                       db_root=database,
                                       button_ports={name: f"btn[{i}]" for i, name in enumerate(config["buttons"])}
                                       if "btn" in self.ports else None)
            self.fabric_armed = False
            self.fabric.control({"switches": self.switches, "running": False})
            self.fabric.programmed((self.work / "design.bit").read_bytes(), synchronous=True)
            self.log = "Bitstream generated. Decoding the programmed fabric for the board display.\n" + self.log
        except Exception as error:
            self.log = f"Bitstream generated; fabric decoder unavailable: {error}\n" + self.log
        return self.snapshot()

    def testbench(self, code, testbench, top="tb"):
        for source in (code, testbench):
            if not isinstance(source, str) or not 1 <= len(source.encode("utf-8")) <= MAX_SOURCE:
                raise ValueError("Source must be between 1 and 100,000 UTF-8 bytes")
        if not isinstance(top, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", top):
            raise ValueError("Invalid testbench module name")
        if not tool_path("iverilog") or not tool_path("vvp"):
            raise ValueError("Icarus Verilog is required for testbenches")
        for name in ("testbench.vcd", "testbench.vvp", "user_design.v", "testbench.v"):
            (self.work / name).unlink(missing_ok=True)
        (self.work / "user_design.v").write_text(code)
        (self.work / "testbench.v").write_text(testbench)
        self.run([tool_path("iverilog"), "-g2012", "-s", top, "-o", "testbench.vvp", "user_design.v", "testbench.v"])
        output = self.run([tool_path("vvp"), "testbench.vvp"], timeout=30)
        self.log = "Testbench completed.\n" + output[-11000:]
        return self.snapshot()

    def control(self, payload):
        if not (self.work / "sim.vvp").exists():
            raise ValueError("Choose switches with Implement to start the board preview")
        if "switches" in payload:
            value = payload["switches"]
            max_switch = (1 << BOARDS[self.board]["switches"]) - 1
            if type(value) is not int or not 0 <= value <= max_switch:
                raise ValueError(f"Switches must be a {BOARDS[self.board]['switches']}-bit number")
            self.switches = value
        if "button" in payload:
            name = payload["button"]
            if name not in BOARDS[self.board]["buttons"] or type(payload.get("value")) is not bool:
                raise ValueError("Invalid button control")
            self.buttons[name] = int(payload["value"])
        if self.fabric and self.fabric.snapshot()["status"] == "ready":
            command = {"switches": self.switches, "step": payload.get("step", 1)}
            if "button" in payload:
                command["button"] = payload["button"]
                command["value"] = payload["value"]
            self.fabric.control(command)
        count = payload.get("step", 1)
        if type(count) is not int or not 1 <= count <= 32:
            raise ValueError("Step must be 1–32 cycles")
        if len(self.samples) + count > MAX_PREVIEW_CYCLES:
            raise ValueError(f"Preview reached {MAX_PREVIEW_CYCLES} cycles. Choose Implement to restart the capture.")
        button_word = sum(value << index for index, value in enumerate(self.buttons.values()))
        self.samples.extend({"switches": self.switches, "buttons": button_word, "leds": "xxxx"} for _ in range(count))
        previous_deadline = self.deadline
        self.deadline = min(previous_deadline or float("inf"), time.monotonic() + 5)
        try:
            self.simulate()
        finally:
            self.deadline = previous_deadline
        return self.snapshot()


SESSION_COOKIE = "fpga_session"
SESSION_TTL = 24 * 60 * 60


class SessionError(Exception):
    def __init__(self, message="Session expired. Reload the page to open your private workbench.", status=401):
        super().__init__(message)
        self.status = status


class SessionStore:
    def __init__(self, root, ttl=SESSION_TTL, limit=64):
        self.root = Path(root)
        self.ttl, self.limit = ttl, limit
        self.lock = threading.RLock()
        self.entries = {}

    @staticmethod
    def key(token):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return None
        return hashlib.sha256(token.encode()).hexdigest()

    @contextmanager
    def acquire(self, token, create=False, lock_lab=True):
        now = time.monotonic()
        expired = []
        with self.lock:
            for key, entry in list(self.entries.items()):
                if not entry["active"] and now - entry["seen"] >= self.ttl:
                    expired.append(self.entries.pop(key)["lab"])
            key = self.key(token)
            entry = self.entries.get(key)
            fresh = entry is None
            if fresh:
                if not create:
                    error = SessionError()
                elif len(self.entries) >= self.limit:
                    error = SessionError("All workspaces are in use. Please try again later.", 503)
                else:
                    error = None
                    self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                    token = secrets.token_urlsafe(32)
                    key = self.key(token)
                    # Directory names are independent of the bearer cookie.
                    work = tempfile.mkdtemp(prefix="lab-", dir=self.root)
                    entry = {"lab": Lab(work), "seen": now, "active": 0}
                    self.entries[key] = entry
            else:
                error = None
            if error is None:
                entry["active"] += 1
                entry["seen"] = now
        for lab in expired:
            lab.close()
            shutil.rmtree(lab.work)
        if error:
            raise error
        try:
            with entry["lab"].lock if lock_lab else nullcontext():
                yield entry["lab"], token if fresh else None
        finally:
            with self.lock:
                entry["active"] -= 1
                entry["seen"] = time.monotonic()


class JobStore:
    """One worker; jobs are private to a session and pinned until completion."""
    def __init__(self, depth=None, retention=3600):
        self.depth = depth if depth is not None else int(os.environ.get("BUILD_QUEUE_DEPTH", "4"))
        if not 1 <= self.depth <= 64:
            raise ValueError("BUILD_QUEUE_DEPTH must be between 1 and 64")
        self.retention = retention
        self.lock = threading.RLock()
        self.queue = queue.Queue(maxsize=self.depth)
        self.jobs = {}
        self.rates = {}
        self.worker = None

    def _prune(self):
        now = time.monotonic()
        for key, job in list(self.jobs.items()):
            if job["status"] in ("succeeded", "failed") and now - job["finished"] > self.retention:
                del self.jobs[key]
        for ip, hits in list(self.rates.items()):
            if not hits or now - hits[-1] >= 60:
                del self.rates[ip]

    def submit(self, store, token, lab, action, payload, ip):
        with self.lock:
            self._prune()
            if lab.active_job or lab.request_busy:
                raise SessionError("This workspace already has an active build.", 409)
            if len(self.jobs) >= 256:
                completed = [key for key, job in self.jobs.items() if job["status"] in ("succeeded", "failed")]
                if completed:
                    del self.jobs[min(completed, key=lambda key: self.jobs[key]["finished"])]
            if self.queue.full() or len(self.jobs) >= 256:
                raise SessionError("Build queue is full. Try again shortly.", 429)
            now = time.monotonic()
            hits = self.rates.setdefault(ip, deque())
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= int(os.environ.get("BUILDS_PER_MINUTE", "6")):
                raise SessionError("Build rate limit reached. Try again in a minute.", 429)
            if not hits and len(self.rates) > 4096:
                self.rates.pop(ip, None)
                raise SessionError("Too many build clients. Try again shortly.", 429)
            key = store.key(token)
            job_id = secrets.token_urlsafe(24)
            job = {"id": job_id, "owner": key, "action": action, "status": "queued",
                   "log": "Waiting for the build worker.", "created": now, "result": None}
            # Retain the session while queued/running, even if its browser leaves.
            with store.lock:
                store.entries[key]["active"] += 1
            lab.active_job = job_id
            job["initial"] = lab.snapshot()
            self.jobs[job_id] = job
            hits.append(now)
            self.queue.put_nowait((job, store, lab, payload))
            if self.worker is None:
                self.worker = threading.Thread(target=self._work, daemon=True, name="build-worker")
                self.worker.start()
            return self.snapshot(job_id, key)

    def snapshot(self, job_id, owner):
        with self.lock:
            self._prune()
            job = self.jobs.get(job_id)
            if job is None or job["owner"] != owner:
                raise SessionError("Job not found.", 404)
            with self.queue.mutex:
                waiting = list(self.queue.queue)
            position = next((i + 1 for i, item in enumerate(waiting) if item[0] is job), 0)
            return {"jobId": job["id"], "status": job["status"], "action": job["action"],
                    "queuePosition": position, "log": job["log"],
                    "elapsedSeconds": round(job.get("finished", time.monotonic()) - job.get("started", job["created"])),
                    "error": job.get("error"), "result": job["result"]}

    def _progress(self, job, chunk):
        with self.lock:
            job["log"] = (job["log"] + chunk)[-12000:]

    def _work(self):
        while True:
            job, store, lab, payload = self.queue.get()
            try:
                with lab.lock:
                    with self.lock:
                        job.update(status="running", started=time.monotonic(), log="Build started.\n")
                    lab.deadline = time.monotonic() + BUILD_TIMEOUT
                    lab.progress = lambda chunk: self._progress(job, chunk)
                    try:
                        action = job["action"]
                        if action == "synthesize":
                            result = lab.synthesize(payload.get("code"), payload.get("top"), payload.get("clockHz", BOARD_CLOCK_HZ))
                        elif action == "implement":
                            result = lab.implement(payload.get("switches"))
                        elif action == "testbench":
                            result = lab.testbench(payload.get("code"), payload.get("testbench"), payload.get("top", "tb"))
                        else:
                            result = lab.generate()
                        with self.lock:
                            job.update(status="succeeded", result=result, finished=time.monotonic())
                    except Exception as error:
                        lab.log = str(error)
                        if job["action"] == "synthesize":
                            lab.phase, lab.backend, lab.gates, lab.ports = "ready", None, None, {}
                            for name in ("adapter.v", "clock_adapter.v", "netlist.json", "user_netlist.json", "board.xdc",
                                         "xc7_netlist.json", "routed.json", "design.fasm", "design.frames", "route.log",
                                         "synth.rpt", "synth.dcp", "timing.rpt", "routed.dcp", "sim.vvp"):
                                (lab.work / name).unlink(missing_ok=True)
                        if job["action"] != "testbench":
                            lab.bitstream = False
                            (lab.work / "design.bit").unlink(missing_ok=True)
                        with self.lock:
                            job.update(status="failed", error=str(error), result=lab.snapshot(), finished=time.monotonic())
                    finally:
                        lab.deadline = lab.progress = None
                        with self.lock:
                            job["finished"] = time.monotonic()
                            lab.active_job = None
            finally:
                with store.lock:
                    entry = store.entries[job["owner"]]
                    entry["active"] -= 1
                    entry["seen"] = time.monotonic()
                self.queue.task_done()


JOBS = JobStore()

SESSIONS = SessionStore(WORK / "sessions")
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".jpg": "image/jpeg", ".png": "image/png",
        ".svg": "image/svg+xml", ".avif": "image/avif"}


class Handler(BaseHTTPRequestHandler):
    def send_data(self, data, kind="application/json", status=200, filename=None):
        if isinstance(data, dict):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("CDN-Cache-Control", "no-store")
        self.send_header("Vary", "Cookie")
        if getattr(self, "pending_cookie", None):
            self.send_header("Set-Cookie", self.pending_cookie)
            self.pending_cookie = None
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        item = cookie.get(SESSION_COOKIE)
        return item.value if item else None

    def set_session_cookie(self, token):
        cookie = SimpleCookie()
        cookie[SESSION_COOKIE] = token
        cookie[SESSION_COOKIE]["path"] = "/"
        cookie[SESSION_COOKIE]["httponly"] = True
        cookie[SESSION_COOKIE]["samesite"] = "Strict"
        # TLS terminates at Cloudflare/Vercel; plain HTTP is only for local use.
        if self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https":
            cookie[SESSION_COOKIE]["secure"] = True
        self.pending_cookie = cookie.output(header="").strip()

    def valid_origin(self):
        origin = self.headers.get("Origin")
        allowed = {value.strip().rstrip("/") for value in
                   os.environ.get("ALLOWED_ORIGINS", "").split(",") if value.strip()}
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        return not origin or (urlsplit(origin).netloc == self.headers.get("Host")
                              and urlsplit(origin).scheme in ("http", "https")) or origin in allowed

    def authorized(self):
        secret = os.environ.get("BACKEND_SECRET", "")
        if secret:
            return hmac.compare_digest(self.headers.get("X-Backend-Secret", "").encode(), secret.encode())
        return (self.client_address[0] in ("127.0.0.1", "::1")
                and not os.environ.get("ALLOWED_ORIGINS")
                and not self.headers.get("X-Forwarded-For")
                and not self.headers.get("X-Forwarded-Proto")
                and not self.headers.get("CF-Connecting-IP"))

    def require_auth(self):
        if self.authorized():
            return True
        self.send_data({"error": "Backend authentication required"}, status=403)
        return False

    def client_ip(self):
        # Only the authenticated proxy supplies this header; ignore generic forwarded headers.
        if os.environ.get("BACKEND_SECRET"):
            return self.headers.get("X-Build-Client-IP", self.client_address[0])[:64]
        return self.client_address[0]

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if (path.startswith("/api/") or path == "/healthz") and not self.require_auth():
            return
        if path in ("/api/health", "/healthz"):
            self.send_data({"ok": True})
            return
        if path.startswith("/api/"):
            if not self.valid_origin():
                self.send_data({"error": "Cross-origin request rejected"}, status=403)
                return
            if path not in ("/api/session", "/api/source", "/api/status") and not path.startswith(("/api/artifact/", "/api/jobs/")):
                self.send_data({"error": "Not found"}, status=404)
                return
            try:
                # /source also bootstraps older deployed clients, but only ever
                # returns the public starter design to a visitor without a cookie.
                with SESSIONS.acquire(self.session_token(), create=path in ("/api/session", "/api/source"), lock_lab=False) as (lab, token):
                    if token:
                        self.set_session_cookie(token)
                    if path == "/api/session":
                        self.send_data({"ok": True})
                    elif path == "/api/source":
                        self.send_data({"code": lab.code, "top": lab.top})
                    elif path.startswith("/api/jobs/"):
                        self.send_data(JOBS.snapshot(path.removeprefix("/api/jobs/"), SESSIONS.key(self.session_token())))
                    elif path == "/api/status":
                        with JOBS.lock:
                            job_id = lab.active_job
                            if job_id:
                                job = JOBS.jobs[job_id]
                                result = {**job["initial"], "job": JOBS.snapshot(job_id, job["owner"])}
                            else:
                                # Never wait for a build that was enqueued during this read.
                                if not lab.lock.acquire(blocking=False):
                                    raise SessionError("Workspace busy. Retry shortly.", 409)
                                try:
                                    result = lab.snapshot()
                                finally:
                                    lab.lock.release()
                        self.send_data(result)
                    else:
                        with JOBS.lock:
                            if lab.active_job:
                                raise SessionError("Build in progress. Download artifacts when it finishes.", 409)
                            with lab.lock:
                                artifact = self.read_artifact(lab, path.removeprefix("/api/artifact/"))
                        self.send_data(*artifact[0], **artifact[1])
            except SessionError as error:
                self.send_data({"error": str(error)}, status=error.status)
            return
        if path == "/basys3.jpg":
            self.send_data((ROOT / "web/basys3.jpg").read_bytes(), "image/jpeg")
            return
        if path in ("/artya7.png", "/nexysa7.avif"):
            file = ROOT / path.lstrip("/")
            if file.is_file():
                self.send_data(file.read_bytes(), MIME[file.suffix])
            else:
                self.send_data({"error": "Board image not found"}, status=404)
            return
        name = "index.html" if path == "/" else path.lstrip("/")
        file = (ROOT / "web" / name).resolve()
        if file.is_file() and file.parent == ROOT / "web" and file.suffix in MIME:
            self.send_data(file.read_bytes(), MIME[file.suffix])
        else:
            self.send_data({"error": "Not found"}, status=404)

    def read_artifact(self, lab, name):
        allowed = {"design.v", "adapter.v", "clock_adapter.v", "board.xdc", "netlist.json", "xc7_netlist.json",
                   "routed.json", "design.fasm", "design.frames", "route.log", "synth.rpt",
                   "timing.rpt", "routed.dcp", "design.bit", "user_design.v", "testbench.v", "testbench.vcd"}
        file = lab.work / name
        if name not in allowed or file.is_symlink() or not file.is_file() or (name == "design.bit" and not lab.bitstream):
            return (({"error": "Artifact not found"},), {"status": 404})
        kind = ("text/plain; charset=utf-8" if file.suffix in
                (".v", ".xdc", ".rpt", ".json", ".fasm", ".frames", ".log")
                else "application/octet-stream")
        return ((file.read_bytes(), kind), {"filename": name})

    def do_POST(self):
        path = urlsplit(self.path).path
        if not self.require_auth():
            return
        if path not in ("/api/board", "/api/synthesize", "/api/implement", "/api/bitstream", "/api/control", "/api/testbench"):
            self.send_data({"error": "Not found"}, status=404)
            return
        if not self.valid_origin():
            self.send_data({"error": "Cross-origin request rejected"}, status=403)
            return
        try:
            with SESSIONS.acquire(self.session_token(), lock_lab=False) as (lab, _):
                self.post_to_lab(lab, path)
        except SessionError as error:
            self.send_data({"error": str(error)}, status=error.status)

    def post_to_lab(self, lab, path):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= MAX_REQUEST:
                raise ValueError("Invalid request size")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Expected JSON")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected an object")
            with JOBS.lock:
                if getattr(lab, "active_job", None) or getattr(lab, "request_busy", False):
                    raise SessionError("This workspace already has an active build.", 409)
                if path in ("/api/synthesize", "/api/implement", "/api/bitstream", "/api/testbench"):
                    if path in ("/api/synthesize", "/api/testbench"):
                        for field in (("code", "testbench") if path == "/api/testbench" else ("code",)):
                            code = payload.get(field)
                            if not isinstance(code, str) or not 1 <= len(code.encode("utf-8")) <= MAX_SOURCE:
                                raise ValueError("Source must be between 1 and 100,000 UTF-8 bytes")
                    result = JOBS.submit(SESSIONS, self.session_token(), lab, path.removeprefix("/api/"), payload, self.client_ip())
                    response_status = 202
                else:
                    # Reserve this lab without holding the global job lock during tools.
                    lab.request_busy = True
                    result = None
                    response_status = 200
            if result is None:
                try:
                    with lab.lock:
                        result = lab.select_board(payload.get("board")) if path == "/api/board" else lab.control(payload)
                finally:
                    with JOBS.lock:
                        lab.request_busy = False
            self.send_data(result, status=response_status)
        except (ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            self.send_data({"error": str(error)}, status=400)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not os.environ.get("BACKEND_SECRET") and (args.host not in ("127.0.0.1", "::1") or os.environ.get("ALLOWED_ORIGINS")):
        parser.error("BACKEND_SECRET is required for a public/proxied backend")
    print(f"FPGA workbench: http://{args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
