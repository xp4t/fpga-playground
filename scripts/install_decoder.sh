#!/usr/bin/env bash
# Keep the legacy decoder build dependencies separate from the stdlib server.
set -euo pipefail
cd "$(dirname "$0")/.."
fpga_python="${PYTHON_BIN:-python3.11}"
# Standalone Python builds may default to Clang without matching C++ headers.
# Use the distro GCC toolchain unless the operator selects another compiler.
export CC="${CC:-gcc}" CXX="${CXX:-g++}"
command -v "$CC" >/dev/null
command -v "$CXX" >/dev/null
"$fpga_python" -c 'import sys; assert sys.version_info[:2] == (3, 11), "Decoder installer requires Python 3.11"'
"$fpga_python" -m venv .venv-decoder
.venv-decoder/bin/python -m pip install --upgrade pip
# pycapnp 1.1.0 predates Cython 3. Build it with its compatible Cython release.
.venv-decoder/bin/python -m pip install 'Cython==0.29.37' 'setuptools<70' wheel setuptools-scm pkgconfig
.venv-decoder/bin/python -m pip install --no-build-isolation -r requirements-decoder.txt
.venv-decoder/bin/python -m pip check
.venv-decoder/bin/python -m fasm2bels --help >/dev/null
