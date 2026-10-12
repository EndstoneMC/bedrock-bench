# dfbench

Dragonfly's example server with a console on standard input, for bedrock-bench. `bbench fetch dragonfly@<version>`
builds it against the Dragonfly module pinned in `server.toml`; the harness then writes `config.toml` and
drives it through the console. The commands, the `[Bench]` settings and the overrides are documented in
[docs/servers/dragonfly.md](../../../docs/servers/dragonfly.md).

| File | |
|---|---|
| `main.go` | flags, server start, version banner, idle ticking |
| `config.go` | `config.toml` loading and the `[Bench]` table |
| `console.go` | console commands |
| `server.toml` | the pinned Dragonfly module, declared parity, and what the harness writes into the run's `config.toml` |
| `testdata/config.toml` | the `config.toml` the harness writes for `spread100`; Go and Python tests both check it |

```shell
GOWORK=off go vet ./... && GOWORK=off go test ./...
go run . -version     # build information as JSON
go run . -defaults    # the default configuration as TOML
```

The module may not contain `replace` directives: a benchmark builds published Dragonfly code only. To measure a
Dragonfly fork, build dfbench against it elsewhere and pass the binary as an arm artifact.

Apache-2.0, like the rest of the repository. dfbench links Dragonfly (MIT); see `NOTICE`.
