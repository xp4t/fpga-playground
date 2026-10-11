# FPGA Playground — fpgaplayground.com on AWS EC2

![The editor, virtual Basys 3, and captured waveform](docs/images/workbench.png)

This folder is a self-contained deployment of FPGA Playground. It includes the
Python API/build worker, browser editor, Vercel functions, all three board
constraints/assets, decoder, tests, and deployment templates. It contains no
credentials, installed tool binaries, visitor workspaces, or local Python environments.

**Deployment target:** Vercel serves `https://fpgaplayground.com`; an AWS EC2 Linux
instance runs Yosys, Icarus and open XC7 at `https://api.fpgaplayground.com` through
Caddy. Vercel adds a private secret to API requests. The browser uses same-origin
`/api/...` requests and polls asynchronous jobs. The editor stays available when
EC2 is stopped; drafts remain in browser storage. This setup needs no home PC,
Cloudflare tunnel, hostname redeploy cron, or Vercel credentials on EC2.

## 1. Accounts and decisions

You need access to your domain's DNS, an AWS account, a Vercel account, and a Git
repository for this code. The domain can stay at its current registrar; you do
not need to transfer it or buy a second domain. Keep existing email MX/TXT records.

Use an **Ubuntu Server 24.04 LTS x86-64/amd64** EC2 instance, not an ARM/Graviton
image: the tool installer downloads Linux x86-64 binaries. Start with
`m7i.xlarge` (4 vCPUs, 16 GiB RAM) and a **100 GiB encrypted gp3 EBS** volume. This
is an initial capacity choice, not a measured guarantee for every 100T design.
Benchmark your designs before increasing concurrency; the application runs one
worker. Check availability and current hourly prices in your region. [AWS instance specifications](https://docs.aws.amazon.com/ec2/latest/instancetypes/gp.html)

Create an AWS monthly cost budget with email thresholds you choose, for example
50%, 80%, and 100%. Budget notifications do not automatically stop the VM.
Compute, EBS, snapshots, bandwidth and public IPv4 can all cost money; stopping
EC2 leaves storage/address costs. Check Vercel plan usage too. [AWS budget setup](https://docs.aws.amazon.com/cost-management/latest/userguide/create-cost-budget.html),
[public IPv4 charges](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-instance-addressing.html)

## 2. Launch EC2 and fix its address

In the EC2 console, choose a region near your visitors and create the instance:

1. Name it `fpgapg-tools`; choose the official Canonical Ubuntu 24.04 amd64 AMI.
2. Choose `m7i.xlarge` and 100 GiB encrypted gp3 storage.
3. Create/download an SSH key pair. Keep the private `.pem` on your own computer,
   outside this folder and Git.
4. Use a public subnet with an internet-gateway route, and initially enable a
   public IPv4 address. A default VPC/public subnet is adequate for this setup.
5. Create a security group with the following **inbound TCP rules**. Keep outbound
   traffic available for package downloads and certificate issuance.
6. Require IMDSv2 in instance metadata options. This app needs no AWS access key
   or instance IAM role. Enable EBS encryption and record your recovery procedure.

| Port | Source | Purpose |
| --- | --- | --- |
| 22 | Your public IP `/32` | SSH administration |
| 80 | `0.0.0.0/0` | HTTPS certificate validation/HTTP redirect |
| 443 | `0.0.0.0/0` | HTTPS API from Vercel |

Do not expose 8000. If you enable IPv6, add corresponding IPv6 web rules and DNS;
otherwise keep this deployment IPv4-only. Update the SSH `/32` when your home IP
changes. [AWS launch guide](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/EC2_GetStarted.html),
[security groups](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/creating-security-group.html)

In EC2 → **Elastic IP addresses**, allocate an Elastic IP and associate it with
this instance. Use it as `YOUR_ELASTIC_IP` below. It stays stable across ordinary
stop/start cycles, unlike an automatically assigned public IP. Keep this address
allocated while its DNS record is in use. [AWS IP address management](https://docs.aws.amazon.com/us_en/AWSEC2/latest/UserGuide/working-with-ip-addresses.html)

On your own Linux/macOS computer:

```sh
chmod 600 /path/to/fpgapg.pem
ssh -i /path/to/fpgapg.pem ubuntu@YOUR_ELASTIC_IP
```

Ubuntu's SSH username is `ubuntu`. Windows users can use OpenSSH after restricting
the private key's file permissions. [AWS connection documentation](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-connect-methods.html)

## 3. Upload this folder

Run this **on your own computer**, from the parent directory of `fpgapg-ws`:

```sh
rsync -az --exclude='.git/' --exclude='.vercel/' --exclude='.tools/' \
  --exclude='.work/' --exclude='.venv*/' --exclude='node_modules/' \
  --exclude='.env*' --exclude='__pycache__/' \
  -e 'ssh -i /path/to/fpgapg.pem' \
  fpgapg-ws/ ubuntu@YOUR_ELASTIC_IP:/tmp/fpgapg-ws/
```

All following commands through section 8 run **inside your EC2 SSH session**,
unless explicitly marked otherwise. Install the source outside `/home` so the
hardened service can hide home directories:

```sh
sudo install -d -o ubuntu -g ubuntu -m 0755 /srv/fpgapg-ws
sudo rsync -a /tmp/fpgapg-ws/ /srv/fpgapg-ws/
sudo chown -R ubuntu:ubuntu /srv/fpgapg-ws
cd /srv/fpgapg-ws
```

Keep this exact path unless you edit **all** paths in `deploy/fpgapg.service` and
`deploy/backend.env.example`. Do not copy `.tools`, database caches or virtualenvs
from another machine; install them for the target host.

## 4. Install tools and verify isolation

```sh
sudo apt-get update
sudo apt-get upgrade -y
sudo apt-get install -y python3 python3-venv python3-dev yosys iverilog \
  bubblewrap util-linux git curl rsync build-essential cmake pkg-config \
  ca-certificates unzip
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
bash scripts/install_openxc7.sh
```

The core API has no pip dependencies and supports Python 3.11+. Ubuntu 24.04's
system Python is suitable for core builds. The open XC7 installer downloads one
matching release of nextpnr-xilinx, fasm2frames, xc7frames2bit and both chip
databases into `.tools/openxc7`; installation can take time and several GB.
Record the release shown by the installer. For a reproducible reinstall, pass
that tag as `OPENXC7_RELEASE=YOUR_TAG bash scripts/install_openxc7.sh`. It refuses
to overwrite an existing installation. [Tool package releases](https://github.com/FPGAwars/tools-openxc7/releases)

Create the dedicated service account with UID/GID 10001:

```sh
sudo groupadd --gid 10001 fpgapg
sudo useradd --uid 10001 --gid fpgapg --system --no-create-home \
  --home-dir /nonexistent --shell /usr/sbin/nologin fpgapg
sudo install -d -o fpgapg -g fpgapg -m 0700 /srv/fpgapg-ws/.work
sudo -u fpgapg bwrap --unshare-all --ro-bind / / -- /bin/true
```

If an account or UID already exists, inspect it and adjust the unit's tmpfs
`uid/gid` to match the dedicated account; do not reassign an existing user's UID.
**The sandbox command must succeed as the service user.** Ubuntu can restrict
unprivileged namespaces with AppArmor. Apply current distro updates, inspect
`sudo journalctl -k`, and resolve the distro's Bubblewrap profile if denied.
Keep isolation enabled; switching to a verified VM image is preferable to
globally disabling AppArmor or running submitted code as root.
[Ubuntu namespace restrictions](https://discourse.ubuntu.com/t/understanding-apparmor-user-namespace-restriction/58007)

Check real tools with a disposable, trusted half-adder:

```sh
sudo -u fpgapg .venv/bin/python scripts/smoke_build.py --board all
sudo -u fpgapg .venv/bin/python -m unittest discover -s tests -v
```

The smoke test must print a successful **real bitstream build** for Basys 3,
Nexys A7 100T, and Arty A7 100T. Tests skip integrations when tools/isolation are
missing; skipped tests do not establish a working deployment. Optional fabric
decoding is a separate installation in section 11.

## 5. Create the shared API secret

Create a root-only configuration directory and generate the secret directly into
its file, using the supplied template:

```sh
sudo install -d -o root -g root -m 0700 /etc/fpgapg
sudo python3 - <<'PY'
from pathlib import Path
import secrets
target = Path('/etc/fpgapg/backend.env')
if target.exists():
    raise SystemExit('Config exists; edit it or deliberately rotate the secret instead')
text = Path('/srv/fpgapg-ws/deploy/backend.env.example').read_text()
target.write_text(text.replace('REPLACE_WITH_64_RANDOM_HEX_CHARACTERS', secrets.token_hex(32)))
target.chmod(0o600)
PY
sudoedit /etc/fpgapg/backend.env
```

The origin list already contains `https://fpgaplayground.com` and
`https://www.fpgaplayground.com`. Add your **exact** production `.vercel.app` URL
if you want to test there too. Preview deployments must have explicitly approved
origins; do not wildcard them. `ALLOWED_ORIGINS` is a browser check, while
`BACKEND_SECRET` provides backend authentication. A forged Origin is insufficient.

You will copy the secret into Vercel in section 9. To display it privately when
needed: `sudo sed -n 's/^BACKEND_SECRET=//p' /etc/fpgapg/backend.env`. Do not paste
it into source, Git, issue reports, screenshots, URLs, or browser variables.
The only copies should be the protected backend file and server-side Vercel setting.

## 6. Start the worker with hard host quotas

```sh
sudo install -o root -g root -m 0644 deploy/fpgapg.service /etc/systemd/system/fpgapg.service
sudo systemctl daemon-reload
sudo systemctl enable --now fpgapg
sudo systemctl status fpgapg --no-pager
```

The supplied service binds only `127.0.0.1:8000`. On the proposed 16 GiB VM it
limits the entire API and all children to **3 CPU cores, 10 GiB RAM, no swap,
128 tasks**, and a **2 GiB ephemeral workspace tmpfs**. Only `.work` is writable
inside the service. Tmpfs counts toward the RAM budget. Do not use this unit
unchanged on a smaller VM.

| Limit | Cloud template value |
| --- | --- |
| Waiting jobs / active workers | 4 / 1 |
| Build submissions per minute per authenticated client IP | 6 |
| Total build wall time | 2,400 seconds |
| Per-process address space / CPU time | 8 GiB / 1,800 seconds |
| Per-workspace watchdog / per-file hard limit | 512 MiB / 128 MiB |
| Source or testbench / entire JSON request | 100,000 UTF-8 bytes / 210,000 bytes |
| Workspace files/directories | 2,048 |
| Custom testbench simulation | 30 seconds |

The systemd cgroup and bounded tmpfs supply aggregate ceilings; per-process
limits and the workspace watchdog alone are insufficient for a public host.
The watchdog can overshoot between checks. If the entire cgroup reaches its RAM
ceiling, systemd can restart the backend, losing sessions/jobs. Tune limits after
measuring trusted designs. Keep headroom for Linux and Caddy.

Check localhost auth without putting the secret on a command line:

```sh
sudo bash -c 'set -a; source /etc/fpgapg/backend.env; set +a; exec /srv/fpgapg-ws/.venv/bin/python /srv/fpgapg-ws/scripts/check_cloud_backend.py'
```

This requires unauthenticated health to return **403** and authenticated health
to return **200**. Inspect the printed tool availability; it should include
open XC7 and sandbox support.

## 7. Add DNS for the API

At the authoritative DNS provider for `fpgaplayground.com`, create:

| Name | Type | Value |
| --- | --- | --- |
| `api` | A | Your EC2 Elastic IPv4 address |

Use **DNS only** initially if your DNS is on Cloudflare. Remove stale/conflicting
`api` A/CNAME/AAAA records. Do not add AAAA unless IPv6 is actually configured on
this instance. Check `dig +short api.fpgaplayground.com` from your own computer
(install `dnsutils` if needed). It must resolve to the Elastic IP.

## 8. Enable HTTPS with Caddy

Install Caddy from its stable package repository:

```sh
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https gnupg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
sudo apt-get update
sudo apt-get install -y caddy
sudo install -o root -g root -m 0644 deploy/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

These commands follow [Caddy's official installation guide](https://caddyserver.com/docs/install#debian-ubuntu-raspbian).
They replace the Caddyfile on a dedicated new instance; merge the site block if
this host already serves other sites. Caddy forwards API requests to localhost
without adding the build secret: the Vercel function alone adds it. Do not put
`header_up X-Backend-Secret ...` in Caddy, since that would authorize direct visitors.

Caddy obtains and renews a public certificate for `api.fpgaplayground.com`.
DNS must point here and TCP 80/443 must reach this instance. If restrictive CAA
records exist, allow your selected certificate issuer according to Caddy's
documentation. [Automatic HTTPS prerequisites](https://caddyserver.com/docs/automatic-https)

```sh
curl -i https://api.fpgaplayground.com/api/health
```

Expected: valid HTTPS and **403**, because curl has no secret. The API hostname's
root `/` intentionally returns 404. A 502 means the Python service is unavailable;
a certificate error points to DNS/network/CAA rather than the application.

## 9. Deploy the editor and proxy on Vercel

Do this from your own computer/browser; EC2 needs no Vercel login.

1. Commit/push the deployment source to your Git host, excluding secrets and build
   outputs. You can keep `fpgapg-ws` inside the existing repository, or copy its
   contents into a new standalone repository.
2. Import that repository into Vercel. Select **Other** as the framework. Set
   **Root Directory = `fpgapg-ws`** if using the existing parent repository, or
   repository root if publishing its contents as a standalone repository.
3. Use Node **22.x** for functions. Let `vercel.json` supply the build command,
   `dist` output directory, and API rewrites. There is no npm install step.
4. Add these **server-side Production environment variables**:

   | Key | Value |
   | --- | --- |
   | `BACKEND_URL` | `https://api.fpgaplayground.com` |
   | `BACKEND_SECRET` | Exact secret from `/etc/fpgapg/backend.env` |

5. Deploy. If variables change afterward, redeploy so the functions receive them.
   Mark the secret sensitive where supported. Do not prefix it `NEXT_PUBLIC_`.
6. In Project → Settings → Domains, add `fpgaplayground.com` and
   `www.fpgaplayground.com`. Set `www` to redirect to the apex as the canonical URL.
7. At your DNS provider, enter the **exact A/CNAME/TXT values Vercel shows for
   your project**. Do not copy a generic Vercel IP from an old tutorial.

   | Name | Type | Value |
   | --- | --- | --- |
   | `@` | Usually A | Exact apex target shown by Vercel |
   | `www` | CNAME | Exact project target shown by Vercel |
   | Verification name, if requested | TXT | Exact Vercel verification value |

8. Wait for both domains to show valid configuration and HTTPS. Keep `api` on
   the EC2 Elastic IP. Preserve email records; remove only conflicting web records.

[Vercel project/root settings](https://vercel.com/docs/project-configuration/general-settings),
[custom-domain setup](https://vercel.com/docs/domains/working-with-domains/add-a-domain),
[environment variables](https://vercel.com/docs/environment-variables)

## 10. Verify the public site before sharing it

On your own computer:

```sh
curl -I https://fpgaplayground.com/
curl -i https://fpgaplayground.com/healthz
curl -i https://api.fpgaplayground.com/api/health
```

Expected: homepage **200**, proxied health **200**, direct unauthenticated API
**403**. Then open the site in a browser and check:

- Basys 3 counter: Synthesize → Implement → Generate .bit; download `design.bit`.
- Change switches, step/reset, and inspect/export the waveform.
- Small half-adder on Nexys A7 100T and Arty A7 100T; check real bitstreams.
- Testbench tab: run a bench with `$dumpfile("testbench.vcd")`, `$dumpvars`, and
  `$finish`; download its VCD.
- In browser Network tools, build POSTs return **202 immediately**, followed by
  private job polling. Reload during routing; the job should resume polling.
- Edit a draft, then deliberately stop only `fpgapg` with
  `sudo systemctl stop fpgapg`. Reload: the editor and draft still load, an offline
  message appears, and builds are disabled. Restart with `sudo systemctl start fpgapg`.

Every visitor gets a private session cookie; a job ID from another session
returns 404. Queue/rate saturation returns 429; concurrent changes to a busy
workspace return 409. Do not put the backend secret into browser curl examples.

## 11. Optional decoded fabric simulation

**RTL simulation and real bitstream generation already work without this.**
The optional decoder reconstructs a limited supported primitive set from a
bitstream for the virtual board. The code selects the correct part, package and
board pin mapping for all three boards. Divided clocks retain RTL playback.
Unknown configuration bits, unsupported primitives or resource limits report an
error and retain the RTL view; this is not complete hardware emulation.

The legacy upstream dependencies require **Python 3.11 specifically**, Cython
0.29.37 and pycapnp 1.1.0. Keep them isolated from Ubuntu's system Python. On
Ubuntu 24.04, build a separate Python 3.11 in `/opt` (do not change `/usr/bin/python3`):

```sh
sudo apt-get install -y libssl-dev zlib1g-dev libbz2-dev libreadline-dev \
  libsqlite3-dev libffi-dev liblzma-dev libncurses-dev uuid-dev
cd /tmp
curl -fL --proto '=https' --tlsv1.2 -o Python-3.11.17.tgz https://www.python.org/ftp/python/3.11.17/Python-3.11.17.tgz
tar -xzf Python-3.11.17.tgz
cd Python-3.11.17
./configure --prefix=/opt/fpgapg-python311 --with-ensurepip=install
make -j2
sudo make altinstall
cd /srv/fpgapg-ws
PYTHON_BIN=/opt/fpgapg-python311/bin/python3.11 bash scripts/install_decoder.sh
.venv-decoder/bin/python scripts/prepare_decoder_cache.py --part xc7a35tcpg236-1
.venv-decoder/bin/python scripts/prepare_decoder_cache.py --part xc7a100tcsg324-1
```

Python 3.11.17 is the verified release for this guide; periodically check for
security updates and rebuild the private interpreter/environment as needed.
[Official release and download verification](https://www.python.org/downloads/release/python-31117/)
Do cache preparation as an operator while the public worker is stopped. It can
consume significant memory/time/disk, especially for 100T, and is not covered by
the service's runtime quotas. Start with the 35T cache; monitor the VM before
preparing 100T. Completed caches are atomically installed read-only under
`.tools/decoder/PART.db`; both 100T boards share one part cache.

Verify decoding and then select its interpreter for the service:

```sh
sudo -u fpgapg .venv-decoder/bin/python scripts/smoke_build.py --board basys3 --require-fabric
sudo -u fpgapg .venv-decoder/bin/python scripts/smoke_build.py --board all --require-fabric
sudo systemctl edit fpgapg
```

Enter this drop-in:

```ini
[Service]
ExecStart=
ExecStart=/srv/fpgapg-ws/.venv-decoder/bin/python /srv/fpgapg-ws/server.py --host 127.0.0.1 --port 8000
```

Then `sudo systemctl restart fpgapg`. Keep both core and decoder environments;
removing the drop-in reverts to core builds. Python dependency installation and
cache preparation are one-time operator steps, never triggered by visitor HDL.

## 12. Updates, recovery and troubleshooting

**Source updates:** run `python3 scripts/sync_cloud_bundle.py` from the parent
development repository to refresh this folder's source while preserving its
cloud README/configs. Commit and deploy the Vercel change. Upload the folder again
to `/tmp/fpgapg-ws`, then stop `fpgapg`, copy source with `sudo rsync -a` to `/srv`,
rerun required dependency/test checks, and start the service. Preserve remote
`.tools`, `.venv*` and `/etc/fpgapg/backend.env`. Do not use an indiscriminate
`rsync --delete`. Roll back source from a known Git revision if verification fails.

**Restarts:** systemd starts the API and Caddy at boot. The service's workspace
tmpfs is ephemeral; restart, stop/start or an OOM loses queued jobs, session files
and artifacts. Terminal job records last one hour in memory (up to 256 records).
Idle sessions expire after 24 hours, with at most 64 sessions. Browser drafts
survive. Visitors should download artifacts they need to keep. Use one backend
process; the queue is not a distributed/durable job system.

**Logs and resource checks:**

```sh
sudo journalctl -u fpgapg -n 100 --no-pager
sudo journalctl -u caddy -n 100 --no-pager
sudo systemctl show fpgapg -p MemoryCurrent -p MemoryMax -p TasksCurrent
df -h /srv/fpgapg-ws
free -h
```

The service has a private mount namespace; host `df` does not show its `.work`
tmpfs. Inspect it with `sudo nsenter -t "$(systemctl show fpgapg -p MainPID --value)" -m df -h /srv/fpgapg-ws/.work`.
Avoid printing full environment files or logging request secret headers.

| Symptom | Check |
| --- | --- |
| SSH timeout | Elastic IP, SSH `/32`, public subnet route, key and `ubuntu` username |
| API TLS fails | `api` DNS, stale AAAA, SG/NACL TCP 80/443, Caddy journal, CAA |
| Direct API 403 | Expected without the private secret |
| Frontend API 403 | Both secret copies match; exact Origin is allowed; redeploy/restart after changes |
| `/healthz` 503 | EC2/service/Caddy running; Vercel `BACKEND_URL`/secret set; backend tool health |
| RTL-only mode | open XC7 install, environment path, both chip databases, execute permissions |
| Sandbox denial | Test as `fpgapg`, distro AppArmor policy, user namespaces; keep sandbox enabled |
| 429 | Queue full or six accepted submissions/minute/IP; wait and retry |
| 409 | This workspace already has a queued/running job |
| Build times out/gets killed | Build log, cgroup OOM journal, RAM/CPU/disk quotas; benchmark before tuning |
| Decoder unavailable | Python 3.11 decoder env, package smoke check, matching read-only part cache |

**Backups:** keep source in Git and take EBS snapshots before tool/OS upgrades.
Keep an encrypted recovery copy of the backend secret separately. Workspaces need
no backup. For secret rotation, generate a new value, update the protected file
and Vercel Production setting, restart the API and redeploy Vercel; expect a brief
transition while the two copies differ. No Vercel CLI login or AWS access keys
are needed on the build VM.

**Retire the home setup:** after the cloud/domain acceptance checks pass, stop
the old backend/tunnel, remove its redeploy cron, revoke unused tunnel credentials
and remove its Vercel login. Keep the old machine available until rollback is no
longer needed. A stable Elastic IP and DNS replace the home tunnel entirely.

## Development tests, board mapping and license

Core tests: `python3 -m unittest discover -s tests -v`. Function/frontend tests:
`node --test tests/*.cjs` with Node 22+. GitHub Actions runs Python/Node checks and
the optional decoder installer; installed-tool tests are required for actual VM
acceptance. The Vercel build can be checked locally with
`mkdir -p dist && cp -r web/. dist/ && cp artya7.png nexysa7.avif dist/`.

| Board | FPGA | Switches / LEDs |
| --- | --- | --- |
| Basys 3 | `xc7a35tcpg236-1` | 16 / 16 |
| Nexys A7 100T | `xc7a100tcsg324-1` | 16 / 16 |
| Arty A7 100T | `xc7a100tcsg324-1` | 4 / 4 |

A single module's top name is detected automatically. Arbitrary input/output bits
map to switches/LEDs in declaration order; `clk` and board buttons retain special
mapping. For example, a half-adder maps `a,b` to SW0/SW1 and `sum,carry` to LD0/LD1.
Arty supports native `btn[3:0]` or the mapped button names. Generated XDC uses the
selected board; changing board clears build artifacts but preserves source.

Project code is [MIT licensed](LICENSE). Board constraints retain the
[Digilent license](boards/Digilent-License.txt). Yosys, Icarus, open XC7, Project
X-Ray/FASM and other downloaded tools keep their upstream licenses. The simulator
originates from [virtual-basys3](https://github.com/xp4t/virtual-basys3).
