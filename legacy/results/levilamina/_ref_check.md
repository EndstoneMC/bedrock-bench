| metric | endstone-0.11.14 | vanilla-1.26.52 |
|---|---|---|
| steady TPS median / lowest 5 s / lowest 15 s | 20.00 / 19.85 / 19.96 | 9.40 / 4.96 / 7.66 |
| loading TPS (join+settle) median / lowest 5 s / lowest 15 s | 19.99 / 16.53 / 17.46 | 10.35 / 3.98 / 5.86 |
| join-storm TPS median / lowest 5 s | 19.96 / 19.89 | 19.90 / 19.77 |
| main thread ms/s steady (ms/tick) | 857 (42.9) | 971 (103.3) |
| main thread ms/s while loading | 854 | 964 |
| view load t90 after first TP, median / p90 | 80.5 s / 206.8 s | 191.5 s / 247.1 s |
| last bot at 90% after first connect | 443 s | 454 s |
| join time median / p90 | 3.70 s / 4.92 s | 3.62 s / 3.71 s |
| gen workers, first 60 s: busy (>100 ms/s) / sum ms/s | 7 of 7 / 5348 | 4 of 7 / 2543 |
| gen workers, whole load: sum ms/s (max thread) | 1224 (181) | 973 (437) |
| process cores steady / loading | 1.04 / 2.23 | 1.07 / 2.05 |
| memory end of steady, private / working set (MB) | 4857 / 4094 | 4063 / 4052 |
| unique chunks per bot (median) | 99 | 111 |
| settle | 300 s | 327 s |
| crash / error lines | no / 0 | no / 2 |
