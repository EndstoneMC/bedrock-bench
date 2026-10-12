// SPDX-License-Identifier: Apache-2.0
package bench;

import cn.nukkit.command.CommandSender;
import cn.nukkit.level.Level;
import cn.nukkit.scheduler.TaskHandler;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * Pre-generates the benchmark world through Nukkit's own pipeline, then counts what was saved.
 *
 * <p>{@code pregen <radius> <columns> <spacing> <origin> <cells> [in_flight]} populates every chunk within
 * {@code radius} chunks of each grid cell centre ({@code origin + (i % columns) * spacing},
 * {@code origin + (i / columns) * spacing} in blocks), one grid row at a time, with at most {@code in_flight}
 * populations pending. Chunks are ordered by their position modulo 3, so neighbouring populations rarely wait for each
 * other. Each finished row is unloaded, which saves it, a few hundred chunks per tick so that no tick stalls for long.
 * The check pass then reloads every cell's chunks from storage, one cell per tick, and counts the populated ones.
 *
 * <p>Output, through the plugin logger: {@code pregen: start ...}, {@code pregen: progress ...} every 10 s,
 * {@code pregen: generated <n> chunks in <s> s}, one {@code pregen: cell <i> <found> <expected>} per cell, and
 * {@code pregen: done <cells> cells}.
 */
final class PreGen implements Runnable {
    private static final int UNLOAD_PER_TICK = 256;
    private static final long PROGRESS_NS = 10_000_000_000L;

    private final BenchTick plugin;
    private final Level level;
    private final int radius;
    private final int maxInFlight;
    private final List<int[]> centres = new ArrayList<>();
    private final List<List<Long>> batches = new ArrayList<>();
    private final List<Set<Long>> areas = new ArrayList<>();
    private final ArrayDeque<Long> pending = new ArrayDeque<>();
    private final Set<Long> inFlight = new HashSet<>();
    private final ArrayDeque<Long> unloading = new ArrayDeque<>();
    private final long started = System.nanoTime();
    private int batch = -1;
    private int checked = -1;
    private int done;
    private int total;
    private long lastProgress;
    private TaskHandler handler;

    private PreGen(BenchTick plugin, Level level, int radius, int columns, int spacing, int origin, int cells,
                   int maxInFlight) {
        this.plugin = plugin;
        this.level = level;
        this.radius = radius;
        this.maxInFlight = maxInFlight;
        for (int i = 0; i < cells; i++) {
            centres.add(new int[]{Math.floorDiv(origin + (i % columns) * spacing, 16),
                    Math.floorDiv(origin + (i / columns) * spacing, 16)});
        }
        for (int first = 0; first < cells; first += columns) {
            addBatch(centres.subList(first, Math.min(cells, first + columns)));
        }
    }

    static boolean start(BenchTick plugin, CommandSender sender, String[] args) {
        if (args.length != 5 && args.length != 6) {
            return false;
        }
        int[] values = new int[6];
        values[5] = 64;
        try {
            for (int i = 0; i < args.length; i++) {
                values[i] = Integer.parseInt(args[i]);
            }
        } catch (NumberFormatException e) {
            return false;
        }
        if (values[0] < 1 || values[1] < 1 || values[2] < 1 || values[4] < 1 || values[5] < 1) {
            return false;
        }
        PreGen job = new PreGen(plugin, plugin.getServer().getDefaultLevel(), values[0], values[1], values[2],
                values[3], values[4], values[5]);
        job.log(String.format(Locale.ROOT, "start %d chunks in %d batches, radius %d", job.total, job.batches.size(),
                job.radius));
        job.handler = plugin.getServer().getScheduler().scheduleRepeatingTask(plugin, job, 1);
        return true;
    }

    private void addBatch(List<int[]> cells) {
        Set<Long> chunks = new LinkedHashSet<>();
        Set<Long> area = new HashSet<>();
        for (int[] c : cells) {
            for (int dx = -radius - 1; dx <= radius + 1; dx++) {
                for (int dz = -radius - 1; dz <= radius + 1; dz++) {
                    area.add(Level.chunkHash(c[0] + dx, c[1] + dz));
                    if (dx * dx + dz * dz <= radius * radius) {
                        chunks.add(Level.chunkHash(c[0] + dx, c[1] + dz));
                    }
                }
            }
        }
        List<Long> ordered = new ArrayList<>(chunks);
        ordered.sort(Comparator.comparingInt(
                h -> Math.floorMod(Level.getHashX(h), 3) * 3 + Math.floorMod(Level.getHashZ(h), 3)));
        batches.add(ordered);
        areas.add(area);
        total += ordered.size();
    }

    @Override
    public void run() {
        if (!unloading.isEmpty()) {
            for (int i = 0; i < UNLOAD_PER_TICK && !unloading.isEmpty(); i++) {
                long hash = unloading.poll();
                level.unloadChunk(Level.getHashX(hash), Level.getHashZ(hash), true);
            }
            return;
        }
        if (checked >= 0) {
            check();
            return;
        }
        generate();
    }

    private void generate() {
        inFlight.removeIf(hash -> {
            int x = Level.getHashX(hash), z = Level.getHashZ(hash);
            if (level.isChunkPopulated(x, z)) {
                done++;
                return true;
            }
            level.populateChunk(x, z, true);
            return false;
        });
        if (pending.isEmpty() && inFlight.isEmpty()) {
            if (batch >= 0) {
                unloading.addAll(areas.get(batch));
            }
            if (++batch >= batches.size()) {
                log(String.format(Locale.ROOT, "generated %d chunks in %.1f s", done, seconds()));
                checked = 0;
                return;
            }
            pending.addAll(batches.get(batch));
            if (!unloading.isEmpty()) {
                return;
            }
        }
        while (inFlight.size() < maxInFlight && !pending.isEmpty()) {
            long hash = pending.poll();
            if (level.populateChunk(Level.getHashX(hash), Level.getHashZ(hash), true)) {
                done++;
            } else {
                inFlight.add(hash);
            }
        }
        long now = System.nanoTime();
        if (now - lastProgress > PROGRESS_NS) {
            lastProgress = now;
            log(String.format(Locale.ROOT, "progress %d/%d batch %d in-flight %d %.0f s", done, total, batch,
                    inFlight.size(), seconds()));
        }
    }

    private void check() {
        if (checked >= centres.size()) {
            level.save(true);
            log("done " + centres.size() + " cells");
            handler.cancel();
            return;
        }
        int[] c = centres.get(checked);
        int found = 0, expected = 0;
        for (int dx = -radius; dx <= radius; dx++) {
            for (int dz = -radius; dz <= radius; dz++) {
                if (dx * dx + dz * dz > radius * radius) {
                    continue;
                }
                expected++;
                if (level.isChunkPopulated(c[0] + dx, c[1] + dz)) {
                    found++;
                }
                unloading.add(Level.chunkHash(c[0] + dx, c[1] + dz));
            }
        }
        log("cell " + checked + " " + found + " " + expected);
        checked++;
    }

    private double seconds() {
        return (System.nanoTime() - started) / 1e9;
    }

    private void log(String message) {
        plugin.getLogger().info("pregen: " + message);
    }
}
