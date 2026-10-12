| metric | Endstone 0.11.14 (BDS 1.26.52) | pmmp (pmmp-100-nolimit) | pnx (pnx-100) | pnx (pnx-100-pregen2) | dragonfly (dragonfly-100) | nukkit (nukkit-100-pregen) |
|---|---|---|---|---|---|---|
| steady TPS median / min | 19.99 / 19.84 | 4.62 / 3.58 | 1.34 / 1.22 | 4.25 / 3.28 | 19.99 / 19.95 | 20.00 / 19.32 |
| loading TPS median / min (join+settle) | 19.99 / 18.61 | 6.00 / 1.54 | 1.63 / 0.69 | 3.06 / 1.66 | 20.00 / 14.57 | 5.68 / 0.69 |
| join-storm TPS median / min | 19.82 / 19.59 | 4.56 / 2.27 | 3.76 / 1.71 | 4.15 / 2.99 | 20.00 / 19.07 | 2.64 / 0.69 |
| main thread ms/s (steady) | 833 (~42 ms/tick) | 980 (~212 ms/tick) | 915 (~683 ms/tick) | 885 (~208 ms/tick) | n/a (Go: no main thread) | 547 (~27 ms/tick) |
| process CPU cores (steady / loading) | 1.04 / 1.94 | 1.07 / 3.70 | 3.67 / 3.86 | 7.39 / 7.05 | 0.56 / 0.71 | 1.32 / 2.23 |
| memory (working set / private MB) | 4078 / 4828 (chaos-run start) | 6052 / 6193 (end of steady); peak 6498 / 6596 | 16071 / 16136 (end of steady); peak 16376 / 16442 | 17173 / 17182 (end of steady); peak 17534 / 17563 | 1885 / 1921 (end of steady); peak 2546 / 2592 | 5444 / 5004 (end of steady); peak 6234 / 5080 |
| join time median (p90) | 3.72 s (4.99) | 3.55 s (6.90) | 0.67 s (0.89) | 0.71 s (1.00) | 0.00 s (0.00) | 9.21 s (18.48) |
| time to 90% of view median (p90) | 132 s (256) | 96 s (134) | view never filled | 84 s (600) | 90 s (90) | 228 s (258) |
| unique chunks per bot (median) | 103 (client-side generation on) | 456 | 16 | 453 | 461 | 480 |
| settle | 327.1 s | 300.1 s | 300.1 s | 603.0 s (capped) | 300.1 s | 300.1 s (capped) |
| crash / error lines | no / 0 | no / 0 | no / 8 | no / 15 | no / 0 | no / 0 |
| chaos (first 5 min) TPS median / min | 2.02 / 1.08 | 3.93 / 1.29 | 1.34 / 0.72 | 3.30 / 0.83 | 20.00 / 19.93 | 7.88 / 0.00 |
| chaos outcome | no crash; main 973 ms/s; 232 error lines | no crash; main 972 ms/s; 0 error lines; 1 killed bots not respawned; skipped: blocks, cleanup, dimension, summon, tickingarea, tickspeed, waypoints, weather | no crash; main 870 ms/s; 6 error lines; 0 killed bots not respawned; skipped: waypoints | no crash; main 822 ms/s; 12 error lines; 2 killed bots not respawned; skipped: waypoints | no crash; main 76 ms/s; 0 error lines; 15 killed bots not respawned; skipped: cleanup, summon, tickingarea, tickspeed, waypoints | no crash; main 938 ms/s; 0 error lines; 2 killed bots not respawned; skipped: tickingarea, waypoints |
