# gophertunnel patches

`bots/loadbot` builds against upstream [gophertunnel](https://github.com/Sandertv/gophertunnel) **v1.62.0** with
no `replace` directive. It currently carries **no patches**.

## Policy

A patch is carried only when all of these exist:

1. **A failing case.** A test in `bots/loadbot` that fails against upstream, usually a decode test built from the
   bytes a pinned server sends, and a reproduction: `loadbot -decode-errors fail` against that server exits 3.
2. **An upstream report.** An issue or pull request on gophertunnel, linked below.
3. **A drop condition.** The upstream release that makes the patch unnecessary.

A carried patch lives in two places that must agree:

- `bots/patches/gophertunnel-v<version>/NNNN-<topic>.patch`, applied in order to the upstream tag;
- a public fork tag `v<version>-bench.<N>` holding upstream plus exactly those patches, referenced by a `replace`
  in `bots/loadbot/go.mod`.

`bots.yml` checks that the fork tag equals upstream plus the patch files. The patch names also go into
`carriedPatches` in `bots/loadbot/main.go`, so `-version`, the `hello` event and `run.json` list them. Patches keep
gophertunnel's MIT license and copyright.

## Changes in the legacy fork

The harness behind Endstone 0.11.14's published benchmark used a vendored gophertunnel snapshot with eight
modified files. Each one has been decided as follows.

| File | Legacy change | Fate | Reason |
|---|---|---|---|
| `minecraft/conn.go` | Global `RequestRadius` used during spawn | Dropped | The radius is requested with a normal `RequestChunkRadius` packet right after spawn (`-radius`). |
| `minecraft/nethernet.go` | Fixed signalling domain | Dropped | The bots only use RakNet. |
| `minecraft/protocol/sub_chunk.go` | Height maps read as one 272-byte block | Dropped | Upstream v1.62.0 reads a height map as 16 rows, each a varuint32 length of 16 followed by 16 heights: the same 272 bytes, decoded properly. `TestSubChunkHeightMapWireLayout` in `bots/loadbot/decode_test.go` decodes that layout, built byte by byte, with upstream. |
| `minecraft/protocol/info.go` | Version string `1.26.51` instead of `1.26.50` | Not carried | Only changes the game version string in the client's login data; the protocol number is 2193 either way. Carried only if a server is shown to reject or treat the upstream string differently. |
| `minecraft/protocol/item_stack.go` | Filtered custom name read as a plain string | Not carried | Appears only in item stack responses, which answer item stack requests. The bot never sends one. |
| `minecraft/protocol/reader.go`, `writer.go` | `Hand` removed from use-item transaction data | Not carried | The bot never sends item interactions, and no server in the matrices is known to send this structure to a client. |
| `minecraft/protocol/packet/clientbound_update_sound_data.go` | Optional sound update slots | Not carried | Sent only when a server updates a sound it is playing; no benchmark workload is known to cause one. |

"Not carried" changes have no known failing case. If one appears, the bot reports a `decode_error` naming the packet,
the run is marked invalid, and the change is revisited under the policy above.

## Evaluation status

Wire-level evidence (no server needed) is in the loadbot tests. The decode smoke against real servers is a
maintainer step before each release, because CI never downloads BDS:

```sh
loadbot -addr 127.0.0.1:<port> -n 4 -decode-errors fail
```

run for at least five minutes of walking and teleports against BDS 1.26.52.3, Endstone 0.11.14, Nukkit,
PowerNukkitX and Dragonfly at their pinned versions. Exit code 3 is a reproduction; anything else means no patch is
needed. Results of the most recent evaluation are attached to the release.

| Server | Version | Result |
|---|---|---|
| BDS | 1.26.52.3 | not yet run |
| Endstone | 0.11.14 | not yet run |
| Nukkit | pinned in `artifacts/nukkit.toml` | not yet run |
| PowerNukkitX | pinned in `artifacts/powernukkitx.toml` | not yet run |
| Dragonfly | pinned in `servers/dragonfly/go.mod` | not yet run |

## Protocol 1001 (`bots/legacy1001`)

The PocketMine-MP bot targets gophertunnel v1.57.1 and needs a login back-port (the `leguuid` key, the multiplayer
audience, and the client data fields FilterProfanity, ClientEditorConnectionIntent, ClientIsEditorCapable,
ProfileHash and Nonce). It follows the same policy, with its patch in `bots/patches/gophertunnel-v1.57.1/` and the
fork tag `v1.57.1-bench.<N>`.
