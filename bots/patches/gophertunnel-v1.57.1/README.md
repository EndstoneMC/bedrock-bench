# Patches to gophertunnel v1.57.1

`bots/legacy1001` builds against [gophertunnel](https://github.com/Sandertv/gophertunnel) v1.57.1, the last release
for protocol 1001, plus the patch below. Patches follow the policy in [`bots/PATCHES.md`](../../PATCHES.md).

| Patch | Change | Upstream | Failing case | Drop when |
|---|---|---|---|---|
| `0001-login-backport.patch` | The offline login in the format gophertunnel adopted in v1.58.0: the self-signed identity token gets the multiplayer audience and carries the player UUID as `leguuid`, and the client data sends `FilterProfanity`, `ClientEditorConnectionIntent`, `ClientIsEditorCapable` and `ProfileHash` instead of `IsEditorMode`. | Back-port of [#468](https://github.com/Sandertv/gophertunnel/pull/468) (654cd5f) and the `ProfileHash` field of [#481](https://github.com/Sandertv/gophertunnel/pull/481) (b36ddad), both released in v1.58.0 | `TestLoginRequestFormat` in `bots/legacy1001` | The pinned PocketMine-MP accepts the protocol `bots/loadbot` speaks. `bots/legacy1001` is removed with it. |

PocketMine-MP 5.44.3 speaks protocol 1001 but rejects the offline login of unpatched v1.57.1. Upstream has no
release that speaks protocol 1001 with the newer login, so there is nothing to wait for upstream.

Not carried:

- The legacy fork's `RequestRadius` global in `minecraft/conn.go`. As in `loadbot`, the radius is requested with a
  normal `RequestChunkRadius` packet after spawn.
- `ClientData.Nonce` ([#501](https://github.com/Sandertv/gophertunnel/pull/501)). It is `omitempty` and the bot never
  sets it, so it never reaches the wire.
- The login parsing changes made to the same files before v1.58.0 (#474 and the `Validate` part of #481). The bot
  never parses a login.

## The fork tag

`bots/legacy1001/go.mod` replaces gophertunnel v1.57.1 with tag `v1.57.1-bench.1` of
`github.com/EndstoneMC/gophertunnel`. That tag must hold upstream v1.57.1 with the patches in this directory applied
in order, and nothing else. `bots/legacy1001/go.sum` pins the module hash of exactly that tree, so Go refuses a tag
with any other content: the download fails with a checksum mismatch. The tag is checked on every build; CI needs no
separate comparison.

Publishing the tag, from a fork of Sandertv/gophertunnel at `github.com/EndstoneMC/gophertunnel`:

```sh
git -c core.autocrlf=false clone https://github.com/EndstoneMC/gophertunnel
cd gophertunnel
git switch --detach v1.57.1
git apply ../bedrock-bench/bots/patches/gophertunnel-v1.57.1/0001-login-backport.patch
git commit -am "Apply bedrock-bench patches for protocol 1001"
git tag v1.57.1-bench.1
git push origin v1.57.1-bench.1

cd ../bedrock-bench/bots/legacy1001
GOWORK=off go mod download   # "SECURITY ERROR ... does NOT match ... go.sum" means the tag is not upstream + patches
GOWORK=off go test ./...
```

A pushed tag is permanent: the Go module proxy and checksum database keep the first content they see. To change the
patches, add or edit the files here, publish the next tag (`v1.57.1-bench.2`), point the `replace` in
`bots/legacy1001/go.mod` at it, run `GOWORK=off go mod tidy` there, and update `carriedPatches` in
`bots/legacy1001/main.go`. All of that goes in one commit.
