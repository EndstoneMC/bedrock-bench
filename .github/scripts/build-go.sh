#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Reproducible builds of the Go programs: the two load bots and the Dragonfly helper.
#
#   .github/scripts/build-go.sh OUT_DIR [PROGRAM...]
#
# Builds each PROGRAM (default: all) for linux/amd64 and windows/amd64 into OUT_DIR and writes OUT_DIR/SHA256SUMS.
# Each module is built on its own (GOWORK=off) with exactly the dependencies its go.mod pins. Keep the flags in sync
# with `bbench bots build` so a local build reproduces the release checksums.
set -euo pipefail

TARGETS=(linux/amd64 windows/amd64)
ALL_PROGRAMS=(loadbot legacy1001 dfbench)

source_dir() {
    case "$1" in
        loadbot) echo bots/loadbot ;;
        legacy1001) echo bots/legacy1001 ;;
        dfbench) echo servers/dragonfly ;;
        *)
            echo "unknown program: $1 (expected one of: ${ALL_PROGRAMS[*]})" >&2
            return 1
            ;;
    esac
}

if [[ $# -lt 1 ]]; then
    echo "usage: $0 OUT_DIR [PROGRAM...]" >&2
    exit 2
fi

root=$(git rev-parse --show-toplevel)
mkdir -p "$1"
out=$(cd "$1" && pwd)
shift
programs=("$@")
[[ ${#programs[@]} -gt 0 ]] || programs=("${ALL_PROGRAMS[@]}")

version=$(git -C "$root" describe --tags --always --match 'v*')
commit=$(git -C "$root" rev-parse HEAD)

export CGO_ENABLED=0 GOWORK=off GOAMD64=v1 GOFLAGS=-mod=readonly
ldflags="-s -w -buildid= -X main.version=$version -X main.commit=$commit"

echo "version $version, commit $commit, $(go version)"
built=()
for program in "${programs[@]}"; do
    dir=$(source_dir "$program")
    for target in "${TARGETS[@]}"; do
        goos=${target%/*}
        goarch=${target#*/}
        ext=""
        [[ $goos == windows ]] && ext=.exe
        name="$program-$goos-$goarch$ext"
        echo "building $name from $dir"
        (cd "$root/$dir" && GOOS=$goos GOARCH=$goarch go build -trimpath -buildvcs=false -ldflags "$ldflags" -o "$out/$name" .)
        built+=("$name")
    done
done

# Hash from stdin: sha256sum on Windows prints '*' before file names.
cd "$out"
mapfile -t built < <(printf '%s\n' "${built[@]}" | LC_ALL=C sort)
for name in "${built[@]}"; do
    printf '%s  %s\n' "$(sha256sum <"$name" | cut -d' ' -f1)" "$name"
done >SHA256SUMS
cat SHA256SUMS
