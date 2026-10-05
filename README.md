# FPGA Workbench

A browser workbench for editing Verilog, synthesizing it, choosing virtual switch inputs, viewing LEDs and waveforms, and generating an Artix-7 `.bit` with the open XC7 toolchain or Vivado. Select Basys 3, Nexys A7 100T, or Arty A7 100T in the header. The Basys 3 fabric model is included here and comes from [virtual-basys3](https://github.com/xp4t/virtual-basys3).

## Run locally

Install `yosys`, `iverilog`, and `vvp` on your path. For Basys 3 bitstream decoding, also install the Python dependencies into a local environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Then run:

```sh
.venv/bin/python server.py
```

For the optional Basys 3 bitstream decoder, install the Python dependencies in a Python 3.9 environment:

```sh
conda create -n fpga-playground python=3.9 pip
conda activate fpga-playground
python -m pip install -r requirements.txt
python server.py
```

The pinned FPGA Interchange dependency requires `pycapnp==1.1.0`. That release and the pinned `fasm` release provide Linux x86-64 wheels for Python 3.9, but not Python 3.13. Installing with the Python 3.13 Conda base environment tries to compile the older `pycapnp` source and fails during Cython compilation.

Open <http://127.0.0.1:8000>. The default counter is ready to edit. **Synthesize** runs Yosys. **Implement** asks for a switch word sized to the selected board, starts an Icarus Verilog RTL simulation for the board and waveform, then places and routes the design when an Artix-7 backend is installed. The virtual board can then be stepped, run, and controlled with switches and pushbuttons. The switch word sets simulated input pins; it is not baked into the bitstream. Changing boards clears the current build artifacts but keeps the editor source.

The local preview records up to 4096 clock cycles per implementation. Choose **Implement** again to restart the capture with a new switch word. The waveform panel can export its samples as CSV.

The board clock input is 100 MHz. The **Clock** control offers 100, 50, and 25 MHz presets and a logarithmic slider from 25 MHz down to 1 Hz. For a design with a `clk` input, lower settings synthesize a counter divider into the bitstream. Some slider values round to the nearest rate achievable by an integer divider; the interface shows that actual rate. Changing the clock requires **Synthesize**, **Implement**, and **Generate .bit** again. The RTL preview advances one selected clock edge per sample; browser playback runs at the selected rate up to 20 cycles per second, rather than trying to animate millions of cycles per second. A design without `clk` has no clock to divide.

## Connect a Vercel frontend to a backend

`vercel.json` deploys the static frontend only. Until an API route is configured,
`/api/source` and `/api/status` return a Vercel error page instead of JSON.
The interface reports that the backend is not connected.

### Free hosted RTL preview on Render

The included `Dockerfile` installs Python, Yosys, and Icarus Verilog and runs as
an unprivileged user. It supports synthesis, board simulation, and waveforms.
It does not install open XC7, Vivado, or the optional bitstream decoder.

1. Push the updated repository to GitHub.
2. On Render, create a **Web Service**, connect the repository, choose the
   **Docker** runtime, use the repository root and `Dockerfile`, and select **Free**.
3. Add the backend environment variable
   `ALLOWED_ORIGINS=https://fpga-playground.vercel.app`.
   Multiple exact origins can be separated by commas. Add preview deployment
   origins explicitly when needed. This is origin validation, not authentication.
4. Keep the Docker command unchanged; it binds to `0.0.0.0` and honors Render's
   `PORT` environment variable. Use `/api/status` as the health check path.
5. Wait for the service to deploy. Open
   `https://YOUR-SERVICE.onrender.com/api/status` and confirm it returns JSON.
6. Add this top-level property to the existing `vercel.json`, retaining its build
   and installation settings, replace the hostname, and push to redeploy Vercel:

```json
"rewrites": [
  {
    "source": "/api/:path*",
    "destination": "https://YOUR-SERVICE.onrender.com/api/:path*"
  }
]
```

The browser continues to call its own `/api` paths, including artifact downloads;
Vercel forwards them to Render. This workflow does not require browser CORS
headers. `ALLOWED_ORIGINS` allows the original frontend origin on proxied POSTs.
Open the Vercel site's `/api/status` after redeployment and confirm it returns JSON.

[Render's free services](https://render.com/docs/free) sleep after 15 minutes of
inactivity and lose generated files and in-memory state on restart, redeploy, or
sleep. The [free compute plan](https://render.com/docs/compute-plans) provides
512 MB RAM and 0.1 CPU. Use this setup for small, temporary RTL demonstrations;
it is not a suitable default for Artix-7 placement and routing.

### Full FPGA tools on your Linux x86-64 computer

For development, an existing Linux computer can provide the backend compute.
Install `yosys` and `iverilog`, optionally run `./scripts/install_openxc7.sh` for
bitstream builds, and start the updated backend:

```sh
ALLOWED_ORIGINS=https://fpga-playground.vercel.app \
  python3 server.py --host 127.0.0.1 --port 8000
```

After installing the open source
[`cloudflared` client](https://github.com/cloudflare/cloudflared), a temporary
[Quick Tunnel](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
can expose this local server over HTTPS for testing:

```sh
cloudflared tunnel --url http://127.0.0.1:8000
```

Use the printed `https://...trycloudflare.com` hostname as the rewrite destination,
retaining `/api/:path*`. The machine, server, and tunnel must remain running.
Quick Tunnel hostnames change whenever the tunnel restarts. For a stable address,
configure a named tunnel with a domain you control or a Linux server with HTTPS.

Both deployment examples currently expose a single shared workbench, not separate
user projects. Run the HDL tools in an isolated environment and add authentication
before opening the service for public submissions. The Docker image isolates
files from the host, but does not add user authentication, per-job resource limits,
or separate workspaces for visitors.

The build endpoints are currently synchronous and can run for many minutes.
[Vercel external rewrites](https://vercel.com/docs/limits) time out after 120 seconds.
Reliable long builds require a background job API that returns a job ID promptly,
with a worker performing builds and the frontend polling progress. Changing the
backend hostname alone does not solve this timeout.

To verify the connection handling, run `node tests/test_api.cjs` and
`python3 -m unittest discover -s tests -p test_http.py -v`.

## Generate a board bitstream

On Linux x86-64, install the [open XC7 toolchain](https://github.com/FPGAwars/tools-openxc7) and matching databases for both FPGA parts with:

```sh
./scripts/install_openxc7.sh
```

The installer writes to `.tools/openxc7`, which is excluded from version control. You can also set `OPENXC7_DIR` to an installed open XC7 package root. It needs `bin/nextpnr-xilinx`, `bin/fasm2frames`, `bin/xc7frames2bit`, chip databases for `xc7a35tcpg236` and `xc7a100tcsg324`, and the matching Project X-Ray part files. The [FPGAwars release assets](https://github.com/FPGAwars/tools-openxc7/releases) provide the tool package and both chip database archives. Keep their release tags together; a chip database built against a different nextpnr revision can fail at runtime.

With open XC7 installed, **Synthesize** maps the board design with Yosys `synth_xilinx`. **Implement** invokes `nextpnr-xilinx` to produce `routed.json` and `design.fasm`. **Generate .bit** uses `fasm2frames` and `xc7frames2bit` to produce the downloadable `design.bit`. The build artifacts include each intermediate file so you can inspect the actual output of every stage. Without an Artix-7 backend, Implement runs the RTL preview and the interface reports that bitstream generation is unavailable.

Alternatively, install Vivado with support for `xc7a35tcpg236-1` and `xc7a100tcsg324-1` and put `vivado` on `PATH`, or set `VIVADO_BIN` to its executable. Vivado takes precedence when both backends are installed. The Basys 3 bitstream decoder uses the Python packages in `requirements.txt` and the Artix-7 Project X-Ray database shipped with the open XC7 toolchain. If you use Vivado without installing open XC7, set `PRJXRAY_DB_ROOT` to a compatible Artix-7 database directory containing `xc7a35tcpg236-1/part.json` and `package_pins.csv`.

The included `virtual-basys3` bitstream decoder is hardwired to the Basys 3 part and pins. On Basys 3 at 100 MHz, a supported `.bit` drives the virtual board and captured pin waveform; unsupported blocks show a decoder error while the RTL preview remains available. At lower clock settings, the real `.bit` contains the divider, but the board LEDs and waveform show RTL simulation of selected clock cycles because advancing millions of 100 MHz source cycles in the decoded fabric is impractical. On Nexys A7 100T and Arty A7 100T, the generated `.bit` is real and downloadable, while the board LEDs and waveform continue to show RTL simulation. The interface labels these cases.

### Open source tool authors

- [Yosys](https://github.com/YosysHQ/yosys), created by Claire Wolf and maintained with YosysHQ contributors, synthesizes the Verilog.
- [Icarus Verilog](https://github.com/steveicarus/iverilog), by Stephen Williams and contributors, runs the virtual board and waveform simulation.
- [nextpnr-xilinx](https://github.com/openXC7/nextpnr-xilinx), originating with David Shah's nextpnr Xilinx work and extended by openXC7 contributors, places and routes the design.
- [FASM](https://github.com/chipsalliance/fasm), designed by F4PGA project developers, is the readable feature format between routing and bitstream encoding.
- [Project X-Ray](https://github.com/f4pga/prjxray) and its [7-series database](https://github.com/f4pga/prjxray-db), by the Project X-Ray contributors including Antmicro, Google LLC, Claire Wolf, Rick Altherr, Jake Mercer, and David Shah, document and encode the Artix-7 configuration frames.
- The [FPGAwars open XC7 package](https://github.com/FPGAwars/tools-openxc7) was started by Juan González-Gómez, with cross-platform packaging and toolchain contributions by Carlos Venegas. It assembles these tools for the supported Artix-7 parts.

The server is a single-user local development tool. It invokes HDL tools on submitted code and has no authentication or per-user sandbox. Isolate it and add authentication before exposing it on a public network.

Run the automated tool checks with `python3 -m unittest discover -s tests -v`.

## Top module and board pins

When the source declares one module, the workbench detects its name automatically. With multiple modules, set **Top module** to the module you want to build.

Small modules with arbitrary port names map input bits to switches and output bits to LEDs in declaration order. For example, `module ha(input a, b, output sum, carry)` maps `a`/`b` to `SW0`/`SW1` and `sum`/`carry` to `LD0`/`LD1`. The mapping appears beside the board and in the Implement dialog. Ports named `clk` and supported button names use the board clock or buttons instead of switches.

| Board | FPGA part | Switches / LEDs | Buttons | Pin source |
| --- | --- | --- | --- | --- |
| Basys 3 | `xc7a35tcpg236-1` | 16 / 16 | `btnC`, `btnU`, `btnD`, `btnL`, `btnR` | [Basys 3 master XDC](boards/Basys-3-Master.xdc) |
| Nexys A7 **100T** | `xc7a100tcsg324-1` | 16 / 16 | Same five names | [Digilent Nexys A7 100T XDC](https://github.com/Digilent/digilent-xdc/blob/master/Nexys-A7-100T-Master.xdc) |
| Arty A7 **100T** | `xc7a100tcsg324-1` | 4 / 4 | `btnC`→BTN0, `btnU`→BTN1, `btnD`→BTN2, `btnL`→BTN3; or native `btn[3:0]` | [Digilent Arty A7 100 XDC](https://github.com/Digilent/digilent-xdc/blob/master/Arty-A7-100-Master.xdc) |

Modules with full size `sw` and `led` ports connect directly to the selected board. On Arty A7, a 16-bit Basys-style `sw`/`led` design can still build: only the low four switch and LED bits connect; `sw[15:4]` is tied low and `led[15:4]` is not connected. The mapping summary states this. Basys 3 also supports `seg[6:0]`, `dp`, and `an[3:0]`; Nexys A7 supports `seg[6:0]`, `dp`, and `an[7:0]`. Arty A7 has no seven-segment display. The copied Digilent XDC files are under the [Digilent MIT license](boards/Digilent-License.txt).
