# SPDX-License-Identifier: Apache-2.0
#
# PHP for PocketMine-MP: Debian slim with the PocketMine-MP 5 build of PHP from pmmp/PHP-Binaries, checked against
# its pinned sha256 and set up as PocketMine-MP's installer does: the php.ini next to the binary points at the
# bundled extensions, so OPcache loads. `php` is on PATH. The phar is copied into a fresh volume mounted at /data per
# run, never baked in.
#
# Build arguments (pinned in artifacts/images.toml; the defaults must stay equal to it, which the tests check):
#   BASE_IMAGE       Debian slim, pinned by digest
#   DEBIAN_SNAPSHOT  snapshot.debian.org timestamp for the apt installs; empty uses the live mirrors
#   PHP_URL          release asset of pmmp/PHP-Binaries (an immutable php-build-N tag, never a *-latest one)
#   PHP_SHA256       sha256 of that asset

ARG BASE_IMAGE=debian:bookworm-20261005-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587
FROM ${BASE_IMAGE}

ARG DEBIAN_SNAPSHOT=20261005T000000Z
RUN --mount=type=bind,source=docker/debian-snapshot.sh,target=/tmp/debian-snapshot.sh \
    sh /tmp/debian-snapshot.sh "${DEBIAN_SNAPSHOT}" \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG PHP_URL=https://github.com/pmmp/PHP-Binaries/releases/download/php-build-524/PHP-8.2-Linux-x86_64-PM5.tar.gz
ARG PHP_SHA256=000fc605878d24be3d6faa6991c0d3a39ae90a0f9ff24d9f4e4722ef823082fc
ADD ${PHP_URL} /tmp/php.tar.gz
RUN echo "${PHP_SHA256}  /tmp/php.tar.gz" | sha256sum -c - \
    && mkdir -p /opt/pmmp \
    && tar -xzf /tmp/php.tar.gz -C /opt/pmmp --no-same-owner \
    && rm /tmp/php.tar.gz \
    && extensions="$(find /opt/pmmp/bin/php7/lib/php/extensions -mindepth 1 -maxdepth 1 -type d -name '*debug-zts*')" \
    && test -n "${extensions}" \
    && echo "extension_dir=\"${extensions}\"" >> /opt/pmmp/bin/php7/bin/php.ini \
    && /opt/pmmp/bin/php7/bin/php -r 'exit(extension_loaded("Zend OPcache") ? 0 : 1);'

ENV PATH=/opt/pmmp/bin/php7/bin:${PATH}
WORKDIR /data
