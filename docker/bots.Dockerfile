# SPDX-License-Identifier: Apache-2.0
#
# Builds a bot binary for linux/amd64 without a local Go toolchain, with the same flags as `bbench bots build`:
#
#   docker build -f docker/bots.Dockerfile --build-arg MODULE=loadbot \
#       --build-arg VERSION=<version> --build-arg COMMIT=<commit> \
#       --output type=local,dest=bots/bin/linux-amd64 .
#
# The result is bots/bin/linux-amd64/<module>, the file the Docker runtime mounts into the bots container. Pass the
# VERSION and COMMIT that `bbench bots build` would stamp to get the same bytes. The runtime never builds this image.
#
# Build arguments (GO_IMAGE is pinned in artifacts/images.toml; the default must stay equal to it):
#   GO_IMAGE  Go toolchain, pinned by digest; its version must equal the toolchain line of the bot's go.mod
#   MODULE    loadbot or legacy1001
#   VERSION   stamped into main.version
#   COMMIT    stamped into main.commit

ARG GO_IMAGE=golang:1.26.3-bookworm@sha256:386d475a660466863d9f8c766fec64d7fdad3edac2c6a05020c09534d71edb4b
FROM ${GO_IMAGE} AS build

ARG MODULE=loadbot
ARG VERSION=dev
ARG COMMIT=unknown
ENV CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOAMD64=v1 GOWORK=off GOFLAGS=-mod=readonly GOTOOLCHAIN=local
WORKDIR /src
COPY bots/core ./core
COPY bots/${MODULE} ./${MODULE}
WORKDIR /src/${MODULE}
RUN go build -trimpath -buildvcs=false \
        -ldflags "-s -w -buildid= -X main.version=${VERSION} -X main.commit=${COMMIT}" \
        -o /out/${MODULE} .

FROM scratch
COPY --from=build /out/ /
