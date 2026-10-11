#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
destination="$project_dir/.tools/openxc7"
if [[ -e "$destination" ]]; then
  echo "open XC7 is already installed at $destination" >&2
  exit 1
fi

release="${OPENXC7_RELEASE:-$(python3 - <<'PY'
import json
import urllib.request
url = 'https://api.github.com/repos/FPGAwars/tools-openxc7/releases/latest'
with urllib.request.urlopen(url, timeout=30) as response:
    print(json.load(response)['tag_name'])
PY
)}"
stamp="${release//-/}"
base="https://github.com/FPGAwars/tools-openxc7/releases/download/$release"
temporary="$(mktemp -d)"
trap 'rm -rf "$temporary"' EXIT

echo "Downloading open XC7 release $release and the Basys 3/Nexys A7/Arty A7 databases..."
curl -LfsS --retry 3 -o "$temporary/tools.tgz" \
  "$base/apio-openxc7-linux-x86-64-$stamp.tgz"
mkdir -p "$temporary/package/chipdb"
tar -xzf "$temporary/tools.tgz" -C "$temporary/package"
for device in xc7a35tcpg236 xc7a100tcsg324; do
  curl -LfsS --retry 3 -o "$temporary/$device.tgz" \
    "$base/apio-xilinx-chipdb-$device-$stamp.bin.tgz"
  tar -xzf "$temporary/$device.tgz" -C "$temporary/package/chipdb"
done

for required in bin/nextpnr-xilinx bin/fasm2frames bin/xc7frames2bit \
  chipdb/xc7a35tcpg236.bin chipdb/xc7a100tcsg324.bin \
  share/nextpnr/external/prjxray-db/artix7/xc7a35tcpg236-1/part.yaml \
  share/nextpnr/external/prjxray-db/artix7/xc7a100tcsg324-1/part.yaml; do
  if [[ ! -f "$temporary/package/$required" ]]; then
    echo "Downloaded package is missing $required" >&2
    exit 1
  fi
done

mkdir -p "$project_dir/.tools"
mv "$temporary/package" "$destination"
echo "Installed open XC7 at $destination"
