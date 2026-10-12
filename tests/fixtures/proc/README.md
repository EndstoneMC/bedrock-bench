# /proc fixtures

Synthetic files in the formats of Linux `/proc` (see proc(5)). The values are made up; only the layout matters.

| File | Shape |
|---|---|
| `task_stat_rest_thread.txt` | `/proc/<pid>/task/<tid>/stat` of a thread whose name contains a space and parentheses: `REST Thread(0)` |
| `schedstat.txt` | `/proc/<pid>/task/<tid>/schedstat`: ns on CPU, ns waiting, timeslices |
| `status.txt` | `/proc/<pid>/status` with `VmRSS`, `RssAnon`, `VmSwap`, `Threads`, `Cpus_allowed_list` |
| `smaps_rollup.txt` | `/proc/<pid>/smaps_rollup` |
| `maps.txt` | `/proc/<pid>/maps` with anonymous, pseudo and deleted mappings |
| `cpuinfo_vm.txt` | `/proc/cpuinfo` of a guest (the `hypervisor` flag) |

Fake `/proc` and `/sys` trees for whole-function tests are built from these in `tmp_path` by the tests.
