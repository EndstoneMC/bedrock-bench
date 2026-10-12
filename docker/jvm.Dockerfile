# SPDX-License-Identifier: Apache-2.0
#
# Java runtime for the JVM servers: the Eclipse Temurin 21 JRE as published. The server jar is copied into a fresh
# volume mounted at /data per run, never baked in.
#
# Build arguments (pinned in artifacts/images.toml; the default must stay equal to it, which the tests check):
#   BASE_IMAGE  Eclipse Temurin 21 JRE, pinned by digest

ARG BASE_IMAGE=eclipse-temurin:21.0.12.1_1-jre-noble@sha256:000fd431958bc81a24abe1e8e5f0f0fd3ae365a594bd50aadb20696805f9408c
FROM ${BASE_IMAGE}

WORKDIR /data
