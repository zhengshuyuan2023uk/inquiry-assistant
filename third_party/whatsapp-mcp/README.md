# WhatsApp bridge source snapshot

This directory contains a source-only snapshot based on [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp), whose MIT license and Luke Harries copyright are retained in `LICENSE`.

The upstream base commit is `7d6a06dcdce1f01dfb24f60e1030d5efba9f3b88`. The imported files include local compatibility changes, including newer pinned WhatsMeow dependencies, and are **not claimed to be the unchanged contents of that commit**. `PROVENANCE.json` records SHA-256 checksums for the exact three imported build files and the upstream license. Those three source hashes were checked against the previously validated bridge build manifest before import.

Only `main.go`, `go.mod`, `go.sum`, and `LICENSE` were copied from the original checkout. This directory has no WhatsApp account database, message database, media cache, tokens, or login state. The provenance and this README were authored for this source distribution.

Use the repository's `scripts/build_mac_bridge.py`, not this snapshot's original entry point. The build script removes the original main and REST server and supplies the Inquiry Assistant loopback control entry point. Read [the bridge build guide](../../docs/BRIDGE_BUILD.md) for commands and licensing scope.

To deliberately update the pinned snapshot, review the source/dependency changes and update `PROVENANCE.json` with the correct base commit, modification explanation, and hashes. The builder rejects mismatches. Hash verification checks consistency with the manifest; it does not establish upstream authorship by itself.
