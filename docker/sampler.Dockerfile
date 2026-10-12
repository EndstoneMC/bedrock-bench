# SPDX-License-Identifier: Apache-2.0
#
# The harness's sidecar image: this package and exactly the dependencies uv.lock pins, on CPython. The Docker runtime
# runs `python -m bedrock_bench.sampling` (thread, tick and process samplers) and `python -m
# bedrock_bench.platform.docker` (engine probe, process inspection, machine sampler) from it, in containers that
# share the server's PID namespace.
#
# Build arguments (pinned in artifacts/images.toml; the defaults must stay equal to it, which the tests check):
#   UV_IMAGE    uv, pinned by digest
#   BASE_IMAGE  CPython slim, pinned by digest

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.14@sha256:1025398289b62de8269e70c45b91ffa37c373f38118d7da036fb8bb8efc85d97
ARG BASE_IMAGE=python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
FROM ${UV_IMAGE} AS uv
FROM ${BASE_IMAGE}

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system \
    UV_PROJECT_ENVIRONMENT=/opt/bedrock-bench

WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
    && rm -rf /src /usr/local/bin/uv

ENV PATH=/opt/bedrock-bench/bin:${PATH} PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /
