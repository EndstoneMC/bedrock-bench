# SPDX-License-Identifier: Apache-2.0
"""Test doubles for end-to-end runs without a Minecraft server or the Go load bot.

- ``fake_server.py``: a scripted console that speaks the line format of every server family.
- ``fake_bots.py``: a stand-in for ``loadbot`` that writes schema-valid JSONL events.
- ``fake_adapter.py``: adapters (``fake_bds``, ``fake_endstone``, ...) that launch the fake server.
- ``bench.py``: a temporary workspace that runs ``bbench`` against the fakes.

See ``README.md`` in this folder for the behaviour scripts and the wire protocol between the fakes.
"""
