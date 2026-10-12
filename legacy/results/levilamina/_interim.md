| metric | levi-opt-100 | levi-100(contaminated) | endstone-0.11.14 | vanilla-1.26.52 |
|---|---|---|---|---|
| steady TPS median / lowest 5 s / lowest 15 s | 10.27 / 6.53 / 8.01 | 10.61 / 9.72 / 10.15 | 20.00 / 19.85 / 19.96 | 9.40 / 4.96 / 7.66 |
| loading TPS (join+settle) median / lowest 5 s / lowest 15 s | 10.27 / 2.03 / 5.19 | 8.58 / 1.17 / 1.85 | 19.99 / 16.53 / 17.46 | 10.35 / 3.98 / 5.86 |
| join-storm TPS median / lowest 5 s | 19.74 / 18.26 | 19.74 / 15.39 | 19.96 / 19.89 | 19.90 / 19.77 |
| main thread ms/s steady (ms/tick) | 1004 (97.8) | 991 (93.4) | 857 (42.9) | 971 (103.3) |
| main thread ms/s while loading | 952 | 963 | 854 | 964 |
| view load t90 after first TP, median / p90 | 192.0 s / 246.7 s | 256.1 s / 271.5 s | 80.5 s / 206.8 s | 191.5 s / 247.1 s |
| last bot at 90% after first connect | 454 s | 394 s | 443 s | 454 s |
| join time median / p90 | 3.65 s / 3.98 s | 3.76 s / 4.43 s | 3.70 s / 4.92 s | 3.62 s / 3.71 s |
| gen workers, first 60 s: busy (>100 ms/s) / sum ms/s | 4 of 7 / 2755 | 7 of 7 / 3547 | 7 of 7 / 5348 | 4 of 7 / 2543 |
| gen workers, whole load: sum ms/s (max thread) | 1135 (436) | 1609 (491) | 1224 (181) | 973 (437) |
| process cores steady / loading | 1.09 / 2.19 | 1.09 / 2.71 | 1.04 / 2.23 | 1.07 / 2.05 |
| memory end of steady, private / working set (MB) | 4888 / 4818 | 4856 / 4760 | 4857 / 4094 | 4063 / 4052 |
| unique chunks per bot (median) | 113 | 107 | 99 | 111 |
| settle | 328 s | 330 s | 300 s | 327 s |
| crash / error lines | no / 0 | no / 0 | no / 0 | no / 0 |
| busiest foreign process, loading / steady (ms/s) | msedge.exe 248 / msedge.exe 241 | Minecraft.Windows.exe 2784 / msedge.exe 173 | lghub_system_tray.exe 128 / lghub_system_tray.exe 124 | System 82 / msedge.exe 291 |
