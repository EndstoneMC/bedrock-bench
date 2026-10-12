# Docker runtime

A machine profile with `runtime.kind = "docker"` runs every server from its Linux build in a container: on a native
Linux engine, on Docker Desktop (Windows, macOS) or on an engine inside WSL2. The harness itself runs on the host.
The code is `src/bedrock_bench/platform/docker.py`; this folder holds the images.

Published Linux numbers should come from native Linux runs. On Docker Desktop and WSL2 the containers run on the
virtual CPUs of a VM; those runs are recorded with placement `vcpu` and the report warns about them.

## Requirements

- Docker Engine 23 or later (BuildKit is the default builder), or Docker Desktop, with Linux containers.
- An engine that supports `--cpuset-cpus` (`docker info` shows `CPUSet: true`).
- The Linux bot binary at `bots/bin/linux-amd64/loadbot`: `bbench bots build --target linux/amd64`,
  `bbench bots fetch --target linux/amd64`, or `bots.Dockerfile` below.
- `bbench doctor --machine <profile>` checks all of this, builds the sampler image once and runs a network namespace
  check between two containers.

## One run

Each run is a small pod of containers that share one PID namespace and one network namespace:

| Container | CPU set | Image | Job |
|---|---|---|---|
| `pod` | harness | `runtime` | holds the namespaces (tini and `sleep infinity`), so the others can stop in any order |
| `server` | server | the adapter's server image | the launch command in a fresh volume mounted at `/data` |
| `threads` | harness | `sampler` | `python -m bedrock_bench.sampling`: thread, tick-thread and process samplers |
| `machine` | harness | `sampler` | machine sampler; only when the engine runs in a VM |
| `bots` | bot | `runtime` | the bot binary, mounted read-only; dials `127.0.0.1` |

- The server files come from the verified cache. The adapter prepares a fresh copy on the host as on a native run,
  the runtime streams it into a new volume, and copies the config files, the executable and crash folders back after
  the run. The volume is deleted with the run's containers.
- The bots share the server's network namespace. Publishing the UDP port with `-p` instead goes through Docker
  Desktop's userspace proxy, which delivers a fraction of the native chunk traffic. `doctor` compares the chunk totals
  of a native and a Docker smoke of the same server and warns above 20%.
- Pinning is the container's cgroup cpuset, which the kernel applies to every thread. The profile priority
  `above_normal` runs the server under `nice -n -5` with `CAP_SYS_NICE`, as a native Linux run does.
- Console input is written with LF line endings.
- On a native Linux engine the sidecars share the harness's monotonic clock. On a VM engine their stamps are mapped
  onto the harness clock from heartbeat events after the run.
- Every container and volume carries the label `org.bedrock-bench.run=<run id>`.

## Images

Base images are pinned by digest in [`artifacts/images.toml`](../artifacts/images.toml). Each Dockerfile repeats its
pins as the defaults of its build arguments, so a manual build gives the same image; `tests/unit/test_docker.py`
keeps the two equal. The runtime tags what it builds `bedrock-bench/<key>:<inputs hash>`, so a changed Dockerfile,
pin or lock file gives a new tag.

| File | Image | Contents |
|---|---|---|
| `bds.Dockerfile` | `bds` | Debian slim, `libcurl4`, `ca-certificates` |
| `endstone.Dockerfile` | `endstone` | CPython slim with Endstone from PyPI: the lock in `servers/endstone/requirements/<version>.txt` and the wheel pinned in `artifacts/endstone.toml`, both hash-checked |
| `jvm.Dockerfile` | `jvm` | Eclipse Temurin 21 JRE |
| `php.Dockerfile` | `php` | Debian slim with the PocketMine-MP 5 build of PHP 8.2, sha256-checked, its php.ini pointed at the bundled extensions as PocketMine-MP's installer does |
| `sampler.Dockerfile` | `sampler` | this package and the dependencies `uv.lock` pins |
| `bots.Dockerfile` | (none) | builds a bot binary for `linux/amd64` without a local Go toolchain |
| (pulled) | `runtime` | the pinned Debian slim image as is: the pod and the bots |
| (pulled) | `dragonfly` | the same image for statically linked Go servers |

No Minecraft server binary, world or template is ever part of an image. Debian packages come from
snapshot.debian.org at the timestamp in `images.toml` (`DEBIAN_SNAPSHOT`, see `debian-snapshot.sh`), so a rebuild
installs the same packages; an empty value uses the live mirrors.

### Updating a pin

Change the reference in `artifacts/images.toml` and the matching `ARG` default in the Dockerfile, then run
`uv run pytest tests/unit/test_docker.py`. A base image digest can be read with
`docker buildx imagetools inspect <name:tag>`.

### Building by hand

The runtime builds images itself, sending only the files each image needs. A manual build from the project root
uses the same Dockerfiles; each has a `<name>.Dockerfile.dockerignore` that limits the context the same way:

```sh
docker build -f docker/bds.Dockerfile -t bedrock-bench/bds:manual .

docker build -f docker/endstone.Dockerfile -t bedrock-bench/endstone:0.11.14-manual \
    --build-arg ENDSTONE_VERSION=0.11.14 \
    --build-arg ENDSTONE_WHEEL_URL=<linux-cp313 url from artifacts/endstone.toml> \
    --build-arg ENDSTONE_WHEEL_SHA256=<its sha256> .
```

To build the Linux bot without Go, pass the version and commit that `bbench bots build` stamps (the harness version
and `git rev-parse --short=12 HEAD`) to get the same bytes:

```sh
docker build -f docker/bots.Dockerfile --build-arg MODULE=loadbot \
    --build-arg VERSION=<version> --build-arg COMMIT=<commit> \
    --output type=local,dest=bots/bin/linux-amd64 .
```

## Leftovers

An interrupted harness can leave containers behind, and a running one is foreign load for the next run. `doctor`
fails while any is running. To remove them:

```sh
docker rm -f $(docker ps -aq --filter label=org.bedrock-bench.run)
docker volume rm $(docker volume ls -q --filter label=org.bedrock-bench.run)
```
