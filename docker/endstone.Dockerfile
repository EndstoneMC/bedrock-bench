# SPDX-License-Identifier: Apache-2.0
#
# Endstone from PyPI on CPython, installed hash-checked: the dependencies from
# servers/endstone/requirements/<version>.txt, then the wheel pinned in artifacts/endstone.toml. BDS is not part of
# the image: each run copies the verified server files into a fresh volume mounted at /data, with the launcher's
# version marker already in place, so the launcher never downloads a server of its own.
#
# Build arguments (the runtime passes them from artifacts/images.toml and artifacts/endstone.toml):
#   BASE_IMAGE             CPython slim, pinned by digest; its minor version must match the wheel's cp tag
#   DEBIAN_SNAPSHOT        snapshot.debian.org timestamp for the apt installs; empty uses the live mirrors
#   ENDSTONE_VERSION       release, e.g. 0.11.14
#   ENDSTONE_WHEEL_URL     the release's manylinux wheel for the base image's CPython
#   ENDSTONE_WHEEL_SHA256  sha256 of that wheel

ARG BASE_IMAGE=python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
FROM ${BASE_IMAGE}

ARG DEBIAN_SNAPSHOT=20261005T000000Z
RUN --mount=type=bind,source=docker/debian-snapshot.sh,target=/tmp/debian-snapshot.sh \
    sh /tmp/debian-snapshot.sh "${DEBIAN_SNAPSHOT}" \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends libcurl4 \
    && rm -rf /var/lib/apt/lists/*

ARG ENDSTONE_VERSION
ARG ENDSTONE_WHEEL_URL
ARG ENDSTONE_WHEEL_SHA256
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
RUN --mount=type=bind,source=servers/endstone/requirements,target=/tmp/requirements \
    test -n "${ENDSTONE_VERSION}" && test -n "${ENDSTONE_WHEEL_URL}" && test -n "${ENDSTONE_WHEEL_SHA256}" \
    && pip install --require-hashes --no-deps -r "/tmp/requirements/${ENDSTONE_VERSION}.txt" \
    && printf 'endstone @ %s --hash=sha256:%s\n' "${ENDSTONE_WHEEL_URL}" "${ENDSTONE_WHEEL_SHA256}" > /tmp/endstone.txt \
    && pip install --require-hashes --no-deps -r /tmp/endstone.txt \
    && rm /tmp/endstone.txt \
    && pip check \
    && python -c 'import importlib.metadata as m, sys; v = m.version("endstone"); sys.exit(v != sys.argv[1] and f"endstone {v} installed, {sys.argv[1]} expected")' "${ENDSTONE_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /data
