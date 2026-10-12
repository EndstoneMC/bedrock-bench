#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Points apt at snapshot.debian.org for one timestamp (e.g. 20261005T000000Z) and runs apt-get update, so the
# packages an image build installs are the same on every machine and every day. With an empty timestamp the image's
# own mirrors are kept. Used by the Debian-based Dockerfiles through a build-time bind mount.
set -eu

snapshot="${1:-}"
if [ -n "$snapshot" ]; then
    . /etc/os-release
    rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*
    cat > /etc/apt/sources.list.d/snapshot.sources <<EOF
Types: deb
URIs: http://snapshot.debian.org/archive/debian/$snapshot
Suites: $VERSION_CODENAME $VERSION_CODENAME-updates
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://snapshot.debian.org/archive/debian-security/$snapshot
Suites: $VERSION_CODENAME-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
    # Snapshot release files are past their Valid-Until date by design; their signatures are still checked.
    echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99snapshot
fi
apt-get update
