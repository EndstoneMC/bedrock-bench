# SPDX-License-Identifier: Apache-2.0
"""Test doubles for end-to-end runs without a Minecraft server or the Go load bot.

With them, ``bbench run-one`` runs end to end in seconds, with no download and no EULA, on Windows and Linux. The
integration tests and the adapter contract test use them. Every module here is standard-library Python except
``fake_adapter.py`` and ``bench.py``, which import the package.

- ``fake_server.py``: a scripted console that speaks the line format of every server family. Its docstring covers the
  families, the behaviour scripts and the wire protocol between the fakes.
- ``fake_bots.py``: a stand-in for ``loadbot`` with the same flags, stdin protocol, exit codes and JSONL events.
- ``fake_adapter.py``: adapters (``fake_bds``, ``fake_endstone``, ...) that launch the fake server.
- ``bench.py``: ``FakeBench``, a temporary workspace that runs ``bbench`` against the fakes.
- ``scenarios/fake-quick.toml``: four bots, every phase scaled down to seconds.
- ``scripts/*.json``: behaviour scripts. Each one is also a variant of every fake adapter.
- ``servers/*/server.toml``: declared parity of the fake adapters, V0 for the BDS family and V1 for the rest.
- ``test_fakes.py``: self-tests of all of the above.
"""
