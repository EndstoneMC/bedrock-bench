# SPDX-License-Identifier: Apache-2.0
#
# Runtime for the Linux build of Bedrock Dedicated Server. No Mojang file is ever part of this image: each run copies
# the verified server files from the local cache into a fresh volume mounted at /data.
#
# Build arguments (pinned in artifacts/images.toml; the defaults must stay equal to it, which the tests check):
#   BASE_IMAGE       Debian slim, pinned by digest
#   DEBIAN_SNAPSHOT  snapshot.debian.org timestamp for the apt installs; empty uses the live mirrors

ARG BASE_IMAGE=debian:bookworm-20261005-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587
FROM ${BASE_IMAGE}

ARG DEBIAN_SNAPSHOT=20261005T000000Z
RUN --mount=type=bind,source=docker/debian-snapshot.sh,target=/tmp/debian-snapshot.sh \
    sh /tmp/debian-snapshot.sh "${DEBIAN_SNAPSHOT}" \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates libcurl4 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /data
