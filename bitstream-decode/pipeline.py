"""Decode captured configuration into a simulator-ready physical netlist.

Never reads the original RTL, checkpoint, or Vivado netlist. It only uses the
captured configuration and the pinned Project X-Ray device database.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def database_root():
    """Locate the Artix-7 database supplied by the open XC7 installation."""
    configured = os.environ.get("PRJXRAY_DB_ROOT")
    candidates = ([Path(configured)] if configured else [
        Path(os.environ["OPENXC7_DIR"]) / "share/nextpnr/external/prjxray-db/artix7"
        if os.environ.get("OPENXC7_DIR") else ROOT / ".tools/openxc7/share/nextpnr/external/prjxray-db/artix7",
        Path.home() / ".apio/packages/openxc7/share/nextpnr/external/prjxray-db/artix7",
    ])
    for candidate in candidates:
        if (candidate / "xc7a35tcpg236-1/part.json").is_file() and (
                candidate / "xc7a35tcpg236-1/package_pins.csv").is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "Basys 3 decoder needs the Artix-7 Project X-Ray database. "
        "Run scripts/install_openxc7.sh or set PRJXRAY_DB_ROOT")


def run_stage(label, command, cwd, output, timeout):
    log_path = output / f"{label}.log"
    with log_path.open("w") as log:
        try:
            subprocess.run(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=timeout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            # The subprocess's stderr is redirected to its log, so the default
            # exception hides the useful traceback from the server and board UI.
            excerpt = log_path.read_bytes()[-16384:].decode("utf-8", errors="replace").strip()
            detail = f"{label} failed; see {log_path}"
            if excerpt:
                detail += f"\n{excerpt}"
            raise RuntimeError(detail) from error


def convert(input_path, output_path):
    source, output = Path(input_path).resolve(), Path(output_path).resolve()
    output.mkdir(parents=True, exist_ok=True)
    database = database_root()
    connection_database = ROOT / "build/xc7a35t.db"
    connection_database.parent.mkdir(parents=True, exist_ok=True)
    complete_marker = connection_database.with_suffix(".complete")
    if not (connection_database.is_file() and complete_marker.is_file()):
        # fasm2bels skips any existing database, including one left by an
        # interrupted build. Recreate an unmarked cache on the next run.
        connection_database.unlink(missing_ok=True)
        complete_marker.unlink(missing_ok=True)
    for label, command in [
        ("decode", [sys.executable, str(ROOT / "bitstream-decode/decode.py"), str(source),
                    "--output", str(output / "design.fasm"), "--db-root", str(database)]),
        ("fasm2bels", [sys.executable, "-m", "fasm2bels", "--connection_database", str(connection_database),
                        "--db_root", str(database), "--part", "xc7a35tcpg236-1",
                        "--fasm_file", str(output / "design.fasm"),
                        "--verilog_file", str(output / "decoded.v"),
                        "--xdc_file", str(output / "decoded.xdc"), "--iostandard", "LVCMOS33", "--drive", "12"]),
    ]:
        run_stage(label, command, ROOT, output, timeout=600)
        if label == "fasm2bels":
            complete_marker.touch()
    # Fixed filenames in a controlled working directory avoid Yosys command injection.
    command = "read_verilog -lib -nowb +/xilinx/cells_sim.v; read_verilog decoded.v; hierarchy -top top; write_json netlist.json"
    run_stage("yosys", ["yosys", "-Q", "-T", "-p", command], output, output,
              timeout=60)
    return output / "netlist.json"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--output", default="build/decoded")
    args = parser.parse_args()
    print(convert(args.input, args.output))
