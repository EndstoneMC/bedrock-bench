# Endstone configuration notes

Endstone runs as `python -m endstone -y --no-interactive -s <server dir>` from a virtual environment that bedrock-bench
builds from PyPI. Every run starts from a fresh copy of the verified BDS build plus the files below. This page lists
only keys and files that exist in the pinned releases (0.11.13 and 0.11.14).

## Files the adapter writes

| File | Content | Category | Why |
|---|---|---|---|
| `server.properties` | the values in `servers/bds/properties.toml` | harness, parity | same settings as vanilla BDS |
| `version.txt` | `26.52` (the `version_marker` in `artifacts/endstone.toml`) | harness | the launcher compares this marker with the BDS build it supports; when it is missing or older, the launcher downloads BDS again over the verified copy |
| `plugins/bstats/config.toml` | `plugins/bstats/config.toml` from this folder: `enabled = false` | harness | no usage statistics upload during measurement |

The launcher's environment gets `PYTHONSAFEPATH=1`, `PYTHONIOENCODING=utf-8`, `PYTHONUNBUFFERED=1` and
`PYTHONDONTWRITEBYTECODE=1`, and loses `PYTHONPATH`, `PYTHONHOME` and `VIRTUAL_ENV` (and `LD_PRELOAD` on Linux). Each
of these is recorded as a harness override.

## What the launcher does on every start

These happen in both pinned releases and are visible in the server directory after a run (`config/` in the run
directory keeps the files as found after shutdown):

- creates `plugins/` and, if missing, `endstone.toml` with Endstone's defaults;
- appends a rate limit for packet 115 (NetworkStackLatency: bucket of 20, drained at 5 per second) to
  `packetlimitconfig.json` and logs `Added a rate limit for packet 115 to packetlimitconfig.json.`;
- on Windows, checks that the Minecraft app may connect to loopback and, if not, asks for elevation to add the
  exemption. Add it once before benchmarking so no prompt appears during a run:
  `CheckNetIsolation LoopbackExempt -a -p=S-1-15-2-1958404141-86561845-1752920682-3514627264-368642714-62675701-733520436`
  (from an elevated prompt).

Because the adapter writes `server.properties` itself and the BDS copy is never downloaded by the launcher, the
launcher's defaults for new servers (`server-name`, `view-distance`, and in releases before 0.11.14
`client-side-chunk-generation-enabled=false`) never apply.

## endstone.toml

Left at Endstone's defaults:

| Key | Default | Effect on a benchmark |
|---|---|---|
| `commands.log` | `true` | logs commands players run; the bots run none |
| `settings.allow-client-packs` | `false` | none |
| `network.ipv6` | `false` | Endstone binds IPv4 only, while vanilla BDS also binds `server-portv6`; the bots use IPv4 |
| `network.stun-servers` | two public STUN servers | NetherNet only; the benchmark uses RakNet |

## Not switchable

Endstone's crash reporter uploads a report when the server crashes. The pinned releases have no switch for it; the
adapter lists it under `facts.crash_upload` in run.json. A crash also writes `crash_reports/crash-*.txt`, which the
harness collects.

## Dependency locks

`requirements/<version>.txt` pins every dependency of the Endstone wheel with hashes, for all platforms and supported
Python versions (`uv pip compile --universal`). The wheel itself is pinned per platform and Python in
`artifacts/endstone.toml`. To add a release:

1. Add its table and wheel pins to `artifacts/endstone.toml` (URL, sha256 and size from `https://pypi.org/pypi/endstone/<version>/json`).
2. Generate its lock:

   ```shell
   echo "endstone==<version>" | uv pip compile --universal --generate-hashes --python-version 3.11 \
     --no-emit-package endstone --no-header - -o servers/endstone/requirements/<version>.txt
   ```

   Use the release's lowest supported Python for `--python-version`.
3. Add a range for the release to `features/endstone.toml`.
4. Run `bbench smoke endstone@<version>`.
