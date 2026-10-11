# Deploy FPGA Workbench

The frontend and health function live on Vercel. Builds run on a Linux host through
a stable named Cloudflare tunnel, or an HTTPS server such as Render. The public
homepage always serves the editor; backend outages disable builds and retain
browser drafts. `/healthz` reports build availability independently of the editor.

## Vercel and backend authentication

Set these **server-side Vercel environment variables** and redeploy once:

- `BACKEND_URL=https://fpga-api.example.com` (the origin only).
- `BACKEND_SECRET`: a random 256-bit secret, shared only with the Python service.

Generate a value with `python3 -c 'import secrets; print(secrets.token_hex(32))'`.
Store the same value in the backend's protected environment file; never put it in
`vercel.json`, browser JavaScript, a URL, or version control. Rotate both copies
together and restart/redeploy the corresponding services.

The API rewrite targets `api/proxy.js`. That function overwrites incoming
`X-Backend-Secret` and `X-Build-Client-IP`, forwards cookies and the browser Origin,
and streams artifact responses. Vercel supplies the caller IP in
[`X-Forwarded-For`](https://vercel.com/docs/headers/request-headers). Python trusts
only the authenticated proxy's custom IP header for its build rate limit.
`ALLOWED_ORIGINS` is still a separate browser request check, **not authentication**.
Set it to exact frontend origins, separated by commas, including approved preview
origins. Do not wildcard it.

Every backend API route, including health, job polling, and downloads, requires
the secret in public mode. A forged Origin without the secret returns 403. Local
loopback development can omit it only when no proxy headers or allowed origins
are configured. A public bind or configured frontend origin without a secret
fails at startup.

The proxy has an eight-second upstream deadline, and health has a four-second
deadline. Builds do not stay attached to either request. Verify `/healthz` and
`/api/session` **through Vercel**; opening the tunnel API directly without the
secret should return 403.

## Named Cloudflare tunnel

Use a domain whose DNS is managed in your Cloudflare account. Follow
[Cloudflare's named tunnel guide](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/create-local-tunnel/).
Run account-level setup on your administration machine:

```sh
cloudflared tunnel login
cloudflared tunnel create fpga-workbench
cloudflared tunnel route dns fpga-workbench fpga-api.example.com
```

Copy only the generated tunnel UUID credentials JSON to
`/etc/cloudflared/YOUR-TUNNEL-UUID.json` on the backend. Keep the account-wide
`cert.pem` on the administration machine. Install
[cloudflared.yml.example](deploy/cloudflared.yml.example) as
`/etc/cloudflared/config.yml`, substituting the UUID, credential path, and hostname.
Run it with your service manager (`cloudflared --config /etc/cloudflared/config.yml
 tunnel run`), or install the system service with
`sudo cloudflared --config /etc/cloudflared/config.yml service install`.

The tunnel forwards to `127.0.0.1:8000`; keep the Python service loopback-only. The
hostname survives restarts and never requires a Vercel rewrite change. This
repository deliberately contains no Quick Tunnel hostname or CLI redeploy loop.
The build machine needs neither a Vercel login nor Cloudflare account-wide
credentials. It still needs its scoped tunnel credential and backend secret.

## Linux service and aggregate quotas

Install Python 3.11+, Yosys, Icarus, Bubblewrap, and `util-linux`. Ensure
unprivileged user namespaces work:

```sh
bwrap --unshare-all --ro-bind / / -- /bin/true
```

Use a dedicated `fpga-workbench` account, with UID/GID 10001 for the supplied
mount configuration, and put the checkout and tools in `/srv/fpga-workbench`.
Change the paths, numeric IDs, and interpreter in
[fpga-workbench.service](deploy/fpga-workbench.service) to match your installation.
Create its `.work` mountpoint before installing the unit.

Copy [backend.env.example](deploy/backend.env.example) to
`/etc/fpga-workbench/backend.env`, fill in the secret/origins/tool paths, and make
it root-owned with mode 0600. Install the unit under `/etc/systemd/system/`, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now fpga-workbench
journalctl -u fpga-workbench -f
journalctl -u cloudflared -f
```

The service's cgroup limits the **whole backend and all descendant processes** to
150% of one CPU, 6 GiB RAM, no swap, and 128 tasks. Its `.work` filesystem is a
2 GiB tmpfs, a hard aggregate ceiling for temporary files and all sessions. Tune
those values below what your host can spare; tmpfs also consumes the service's
memory budget. The workspace is ephemeral and disappears on service restart.
A dedicated unprivileged account and read-only checkout limit host access.

Application defaults, read when the server starts:

| Variable | Default | Scope |
| --- | --- | --- |
| `BUILD_QUEUE_DEPTH` | 4 | Waiting jobs; one worker runs at a time (valid range 1–64) |
| `BUILDS_PER_MINUTE` | 6 | Accepted build submissions per authenticated client IP, across cookies |
| `BUILD_TIMEOUT_SECONDS` | 2400 | Total wall time across a job's tool stages |
| `BUILD_MEMORY_MB` | 4096 | Inherited per-process address-space limit (`RLIMIT_AS`) |
| `BUILD_CPU_SECONDS` | 1800 | Inherited per-process CPU-time limit (`RLIMIT_CPU`) |
| `BUILD_DISK_MB` | 512 | Per-workspace size, checked during and after execution |
| `BUILD_FILE_MB` | 128 | Hard per-file limit (`RLIMIT_FSIZE`) |

Source and testbench inputs are each limited to 100,000 UTF-8 bytes; JSON requests
are capped at 210,000 bytes. At most 2,048 workspace files/directories are allowed.
All tool requests, including interactive RTL stepping, share one execution slot.
Tool wall-time limits remain in place (routing up to 1,800 seconds; testbenches
30 seconds; interactive stepping five seconds). Logs retain only a bounded tail.
Limit failures kill the process group and Bubblewrap PID namespace, including
children, and release the worker for the next queued job.

The workspace check is a watchdog, not a filesystem quota: it can overshoot
between checks. Per-process limits also do not bound the sum of a tool's children.
**Use the provided cgroup and bounded workspace mount for public home-PC hosting**
so neither case can consume unlimited host resources. An equivalent container
needs CPU/memory/PID limits and a bounded tmpfs for `/app/.work`, plus support for
Bubblewrap user namespaces. Do not disable isolation when the host forbids it.

## Job and session API

1. `GET /api/session` creates/reuses a private workspace cookie.
2. `POST /api/synthesize`, `/api/implement`, `/api/bitstream`, or `/api/testbench`
   returns 202 with `jobId`, `status`, and `queuePosition`.
3. Poll `GET /api/jobs/JOB_ID` with that cookie. States are `queued`, `running`,
   `succeeded`, or `failed`; responses include bounded `log`, `elapsedSeconds`,
   `error`, and the final workspace snapshot in `result`.
4. `GET /api/status` returns the last snapshot plus `job` while a build is active.
   Source, health, and job/status polling do not wait on the build lock. Conflicting
   mutations and downloads return 409 until the job completes.

Job IDs alone grant no access: another cookie gets 404. Queue saturation and
per-IP rate limits return 429; retry after a minute. One active job per workspace
includes both queued and running jobs. Sessions are pinned while work runs so
expiration cannot delete a queued build. Terminal jobs are retained for one hour
(up to 256 records); queue and rate state are process-local. Run one backend process.
A restart loses queue/session state; it does not resume incomplete builds.

Cookies are host-only, HttpOnly, SameSite=Strict, and Secure through the HTTPS
proxy. Artifacts never carry session tokens in their URLs. API responses are
uncached and vary by Cookie. Workspaces expire after 24 hours of inactivity;
64 may exist at once. Anonymous sessions are temporary workspaces, not accounts.

## Optional bitstream decoder

Core builds use supported Python 3.11+ without pip dependencies. The optional
F4PGA decoder still depends on an upstream `pycapnp==1.1.0` pin, confirmed in its
[package configuration](https://raw.githubusercontent.com/chipsalliance/python-fpga-interchange/master/setup.py).
It is isolated in `requirements-decoder.txt`; the server no longer requires or
recommends Python 3.9. Install its supported Python 3.11 build environment with:

```sh
bash scripts/install_decoder.sh
```

This creates `.venv-decoder` and compiles legacy dependencies with Cython 0.29.37,
then runs `pip check` and a decoder import/CLI smoke check. GitHub Actions verifies
this installation on Python 3.11. Use `.venv-decoder/bin/python` for the backend
when decoding is wanted; change the service's `ExecStart` accordingly. This is a
compatibility environment, not a replacement for upstream dependency upgrades.

Install the open XC7 database or set `PRJXRAY_DB_ROOT` to an Artix-7 database
containing each target's `part.json` and `package_pins.csv`. Prepare trusted
connection caches once as an operator, **before enabling public builds**:

```sh
.venv-decoder/bin/python scripts/prepare_decoder_cache.py --part xc7a35tcpg236-1
.venv-decoder/bin/python scripts/prepare_decoder_cache.py --part xc7a100tcsg324-1
```

Cache preparation can take significant time and disk space. It operates only on
installed device databases and is intentionally separate from visitor builds.
Completed caches are installed atomically under `.tools/decoder/PART.db` and
mounted read-only in visitor sandboxes. The 100T cache serves both Nexys and Arty;
board-specific generated XDC and switch/button/LED widths select the pin mapping.
Decoder conversion runs synchronously **inside the queued job**, with the same
limits as synthesis/routing, rather than spawning unbounded background tools.

Only the simulator's supported primitive set can be reconstructed. Unknown bits,
incomplete frames/database coverage, unsupported primitives, or exhausted limits
produce a decoder error with the RTL view retained. Divided clocks keep RTL
playback. Full real-device decoder integration needs installed caches and toolchain;
small synthetic board-mapping tests cannot establish complete fabric coverage.

## Render RTL-only alternative

The `Dockerfile` supplies Python 3.11, Yosys, Icarus, Bubblewrap, and `prlimit` and
runs as a non-root account. Deploy it as a Docker web service; configure
`BACKEND_SECRET` and `ALLOWED_ORIGINS`, then set Vercel's `BACKEND_URL` to its stable
HTTPS origin. The container honors `PORT`. Use `/` for platform liveness checks;
the protected API health check runs through Vercel at `/healthz`. Direct
`/api/health` without the secret is 403.

The platform must permit Bubblewrap user namespaces. Test isolation before making
it public, and use platform/container quotas to bound aggregate resources. Small
RTL demos fit a lightweight service; 100T routing needs a larger machine and FPGA
tools. Hosted restarts/sleep invalidate jobs and sessions. The frontend remains an
offline editor during outages. No automatic backend switching is implemented:
switching backends invalidates workspace cookies and queued jobs.

## Migration from Quick Tunnel and cron

The previous README described PC-local scripts (`start_pc_backend.sh`,
`boot_pc_backend.sh`, `pc_boot.py`), `.work/backend.log`, `.work/tunnel.log`, and
`.work/boot.log`, plus a cron task that changed the rewrite and redeployed Vercel.
Those scripts are not tracked in this checkout and are no longer part of the
supported deployment path.

Stop the old watcher/tunnel, remove only this project's marked cron entries while
preserving unrelated jobs, and migrate to the named tunnel and system service
above. Set both Vercel environment variables and deploy the new proxy before
exposing the authenticated backend. Remove/revoke the saved Vercel CLI credential
on the build box after migration; redeployments now happen from your development
machine or Vercel's Git integration. Do not keep a redeploy job running against
this version: there is no external tunnel rewrite to patch.

This repository change does not modify installed cron jobs, account credentials,
DNS, tunnel services, or the live Vercel project. Those changes require your owned
domain, scoped tunnel credentials, and shared secret at deployment time.
