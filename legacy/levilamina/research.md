# LeviOptimize: what each optimization does

Researched 2026-10-12, read-only, from GitHub source. Comparison scope: Windows.

- LeviOptimize **v0.17.0** (2026-09-18, the same commit as `main` HEAD `1e71327`): https://github.com/LiteLDev/LeviOptimize/tree/v0.17.0
- LeviLamina **v26.51.6** (2026-09-29, latest release): https://github.com/LiteLDev/LeviLamina/tree/v26.51.6

Link shorthands used below:
`LO` = `https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0`, `LL` = `https://github.com/LiteLDev/LeviLamina/blob/v26.51.6`.

Behaviour classes: **V0** keeps vanilla behaviour exactly, **V1** changes only timing or order, **V2** changes gameplay or other visible behaviour.

---

## 1. Summary

- **A fresh v0.17.0 install turns on one optimization, `fixChunkLeak`, plus the `timing` diagnostic command.** `optMovingBlock`, `optSeenPercent` and `optPushEntity` are all off by default ([LO/src/Config.h#L13-L19](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/Config.h#L13-L19), [LO/src/features/features.h#L31](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/features/features.h#L31)).
- **Upgraded installs can still run the old defaults.** Before v0.14.0, `optMovingBlock` and `optPushEntity.enable` defaulted to `true`. When the config version changes, LeviLamina merges the user's existing JSON over the new defaults, so an older `config.json` keeps `true` (see §3).
- **Every feature is a function hook (trampoline detour via `LL_TYPE_INSTANCE_HOOK` / `LL_STATIC_HOOK`) or a LeviLamina event listener. LeviOptimize has no byte patches.**
- The current features cover four niches: piston moving-block packets, entity-push cramming, explosion exposure, and map-item tracking. **None of them overlaps Endstone's fixes** (chunk generation and sending, autosave, compression, peer chain, thread pools, allocator).
- Overlap exists only in **removed** LeviOptimize features (`PlayerLookupOpt`, `PacketSenderOpt`) and in **LeviLamina core**, which swaps BDS's allocator to **mimalloc on Windows**, unconditionally, on every LeviLamina server (§6).
- **Neither project publishes any benchmark** (§8).
- **Version targets:** LeviOptimize 0.17.0 needs LeviLamina `26.51.*`, and LeviLamina 26.51.6 pins BDS **1.26.51** (bdsdown `version://windows/1.26.51.1`). Endstone's BENCHMARK.md measures BDS **1.26.52**. LeviLamina has no 1.26.52 release or branch yet.
- **Linux:** LeviLamina ships Windows only. Its tooth variants are all `win-x64`, Linux runs only through a Wine Docker image, and the maintainer answered "no" to Linux support in [LeviOptimize#29](https://github.com/LiteLDev/LeviOptimize/issues/29).

---

## 2. Versions

| Item | Value | Source |
|---|---|---|
| LeviOptimize latest | v0.17.0, 2026-09-18, "Adapted to LeviLamina 26.51.*" | [CHANGELOG](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/CHANGELOG.md), [tooth.json](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/tooth.json) (`"github.com/LiteLDev/LeviLamina": "26.51.*"`), [xmake.lua#L11](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/xmake.lua#L11) |
| LeviLamina latest | v26.51.6, 2026-09-29 (`main` tooth.json already says 26.51.7, unreleased) | [releases](https://github.com/LiteLDev/LeviLamina/releases) |
| BDS targeted | 1.26.51 (`"github.com/LiteLDev/bds": "1.26.51"`). That package downloads `version://windows/1.26.51.1` | [LL/tooth.json#L16](https://github.com/LiteLDev/LeviLamina/blob/v26.51.6/tooth.json#L16), [LiteLDev/bds tooth v1.26.51](https://github.com/LiteLDev/bds/blob/v1.26.51/tooth.json) |
| Platforms | LeviOptimize builds a Windows x64 server variant and, since 0.12.0, a Windows client variant. CI runs only on `windows-latest` | [release.yml](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/.github/workflows/release.yml) |

---

## 3. How the config turns features on

- Each feature is an `ll::reflection::Dispatcher<Storage, Listener>`. When the config loads, LeviLamina deserializes `storage` in place and then calls `listener.call(storage)` ([LL/src/ll/api/reflection/Deserialization.h#L69-L74](https://github.com/LiteLDev/LeviLamina/blob/v26.51.6/src/ll/api/reflection/Deserialization.h#L69-L74)). Each listener's `call(bool)` creates or destroys a `HookRegistrar`, so the hooks go in at mod `load()` ([LO/src/LeviOptimize.cpp#L24-L25](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/LeviOptimize.cpp#L24-L25)) and come out at `disable()` ([#L44-L46](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/LeviOptimize.cpp#L44-L46)).
- **Upgrade merge:** when the stored `version` (now 18) differs, `defaultConfigUpdater` serializes the new defaults and runs `merge_patch(userData)`, so the user's old values win ([LL/src/ll/api/Config.h#L37-L42](https://github.com/LiteLDev/LeviLamina/blob/v26.51.6/src/ll/api/Config.h#L37-L42)). A config written by v0.13.2 or earlier therefore keeps `optMovingBlock: true` and `optPushEntity.enable: true` after an upgrade to 0.14+. Only new installs get the `false` defaults that [231e5f1](https://github.com/LiteLDev/LeviOptimize/commit/231e5f168) introduced.
- `disable()` saves the in-memory config over the file. Editing the file while the server runs is therefore overwritten at shutdown ([#27 comments](https://github.com/LiteLDev/LeviOptimize/issues/27)).
- Unreleased LeviLamina `main` ([b77ac6a](https://github.com/LiteLDev/LeviLamina/commit/b77ac6a69)) now requires `Dispatcher` listeners to be `std::movable`. LeviOptimize's listeners are not movable (they hold a `unique_ptr` and declare a destructor), so the next LeviLamina release will need a LeviOptimize change. This does not affect current releases.

---

## 4. Current features (v0.17.0)

### 4.1 Overview

| Config key | Default (fresh install) | BDS target | Mechanism | Class |
|---|---|---|---|---|
| `features.fixChunkLeak` | **true** | `MapDataManager::mMapData` → `MapItemSavedData::mTrackedEntities`, on LeviLamina's `PlayerDisconnectEvent` (hooks `ServerPlayer::disconnect`) | Event listener that mutates BDS data | V2 (bug fix that diverges from vanilla) |
| `features.optMovingBlock` | false (true before 0.14.0) | `MovingBlockActor::_getUpdatePacket`, `MovingBlockActor::save` | Wrap hook plus a conditional replacement of `save` | V2 (client-visible only; server simulation unchanged) |
| `features.optSeenPercent` | false (true before 0.6.1) | `BlockSource::getSeenPercent`, `Level::tick` | Memoizing wrap hook, cleared every tick | V2 (narrow) |
| `features.optPushEntity.enable` | false (true before 0.14.0) | master switch for the two options below | — | — |
| `optPushEntity.disableVec0Push` | true, but inert while `enable` is false | `PushableByEntityUtility::push(Actor&, Vec3 const&)` | Wrap hook with early return | V0 presumed (not verifiable from source) |
| `optPushEntity.maxPushTimes` | -1 (off) | `PushableByEntityUtility::push(Actor&, Actor&, bool)`, `Level::tick` | Wrap hook with early return, per-tick counter | V2 |
| `optPushEntity.unlimitedPlayerPush` | false | modifier for `maxPushTimes` | — | V2 (only with `maxPushTimes`) |
| `commands.timingCommand` | true | `EntitySystems::mEnableTimingCapture` | `/timing` command | V0 (diagnostic) |

### 4.2 `fixChunkLeak` (on by default)

- **What it does:** on `PlayerDisconnectEvent`, it walks every `MapItemSavedData` in the level's `MapDataManager` and erases each `MapItemTrackedActor` whose `keyEntityId` equals the leaving player's unique ID ([LO/src/features/ChunkLeakFix.cpp#L17-L35](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/features/ChunkLeakFix.cpp#L17-L35)).
- **Event source:** LeviLamina emits the event from a hook on `ServerPlayer::disconnect`, before the original runs ([LL/src/ll/api/event/player/PlayerDisconnectEvent.cpp#L7-L10](https://github.com/LiteLDev/LeviLamina/blob/v26.51.6/src/ll/api/event/player/PlayerDisconnectEvent.cpp#L7-L10)).
- **Why it frees chunks:** each `MapItemTrackedActor` owns a `std::shared_ptr<ChunkViewSource>` ([LL/.../MapItemTrackedActor.h#L55](https://github.com/LiteLDev/LeviLamina/blob/v26.51.6/src/mc/world/level/saveddata/maps/MapItemTrackedActor.h#L55)). A stale entry for a departed player keeps that chunk view, and the chunks and entities in it, loaded. Removing the entry releases it.
- **Class V2 (strictly):** this is a leak fix, so the server state after a disconnect differs from vanilla. Chunks and entities that vanilla keeps loaded are released, and the departed player stops being tracked on maps. It does nothing while players stay connected. The maintainer confirmed it only acts on quit, not when a player moves away ([#22](https://github.com/LiteLDev/LeviOptimize/issues/22)). The move-away case is still open as [#24](https://github.com/LiteLDev/LeviOptimize/issues/24).
- **Benchmark relevance:** in a steady-state run with no disconnects, this feature never executes.

### 4.3 `optMovingBlock` (off by default since 0.14.0)

- **Hook 1, wrap:** `MovingBlockActor::$_getUpdatePacket` sets a `thread_local` flag around the original ([LO/src/features/MovingBlockOpt.cpp#L13-L27](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/features/MovingBlockOpt.cpp#L13-L27)).
  - LeviLamina marks this thunk `MCFOLD`, meaning "Identical COMDAT Folding" ([LL/.../MovingBlockActor.h#L98](https://github.com/LiteLDev/LeviLamina/blob/v26.51.6/src/mc/world/level/block/actor/MovingBlockActor.h#L98), [LL/src/mc/_HeaderOutputPredefine.h#L15](https://github.com/LiteLDev/LeviLamina/blob/v26.51.6/src/mc/_HeaderOutputPredefine.h#L15)). The hooked address is therefore shared with other folded `_getUpdatePacket` bodies, and the flag is set for those block actors as well. Only the save hook reads the flag, so this is harmless.
- **Hook 2, conditional replacement:** in `MovingBlockActor::$save`, when the flag is clear (disk saves) it calls the original. When the flag is set (building the network update packet) it writes only the base `BlockActor::save` fields plus `movingBlock`, `movingBlockExtra`, `pistonPosX/Y/Z` and `expanding`, and does not serialize `mWrappedBlockActor` ([#L29-L53](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/features/MovingBlockOpt.cpp#L29-L53)).
  - The tag it drops is the optional `movingEntity` compound, which holds the moved block entity, for example a chest with its items ([minecraft.wiki block entity format](https://minecraft.wiki/w/Bedrock_Edition_level_format/Block_entity_format)).
- **Effect:** smaller and cheaper `BlockActorDataPacket`s while pistons move block entities. Clients get no block-entity data for the block during the move animation. Saves and server simulation are unchanged.
- **Class V2, client-visible only.** The README claims "Improves FPS and TPS" without numbers.

### 4.4 `optSeenPercent` (off by default since 0.6.1)

- **What it does:** memoizes `BlockSource::getSeenPercent(Vec3 center, AABB bb)`, the explosion exposure ray test, in a static `phmap::parallel_flat_hash_map` keyed on `(DimensionType, center, bb)`. A second `Level::$tick` hook clears the map after every tick ([LO/src/features/SeenPercentOpt.cpp#L14-L40](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/features/SeenPercentOpt.cpp#L14-L40)). The map uses the default no-op mutex.
- **Class V2 (narrow):** a repeat query within the same tick with an identical center and AABB returns the first result, even if blocks changed in between (for example, an earlier explosion in that tick destroyed cover). It only hits when several explosions share an exact center in one tick.
- **History:** crash reports [#8](https://github.com/LiteLDev/LeviOptimize/issues/8) and [#15](https://github.com/LiteLDev/LeviOptimize/issues/15) led to the default becoming off in 0.6.1 ([54f50b6](https://github.com/LiteLDev/LeviOptimize/commit/54f50b64a)).

### 4.5 `optPushEntity` (whole group off by default since 0.14.0)

The `enable` gate and option wiring are at [LO/src/features/PushEntityOpt.cpp#L68-L88](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/features/PushEntityOpt.cpp#L68-L88). The defaults are at [features.h#L31-L34](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/features/features.h#L31-L34).

- **`disableVec0Push`** hooks `PushableByEntityUtility::push(Actor& owner, Vec3 const& vec)` and returns without calling the original when `vec == 0` ([#L14-L27](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/features/PushEntityOpt.cpp#L14-L27)).
  - It is V0 if vanilla's zero-vector push is a pure no-op. That cannot be confirmed from LeviOptimize's source. The README itself says only "without altering gameplay significantly".
- **`maxPushTimes`** hooks the pairwise `push(Actor& owner, Actor& other, bool pushSelfOnly)` with a static `flat_hash_map<Actor*, int>` keyed on `owner` ([#L29-L55](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/features/PushEntityOpt.cpp#L29-L55)). A `Level::$tick` post-hook clears it ([#L58-L61](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/features/PushEntityOpt.cpp#L58-L61)).
  - Once the count is above the limit, it skips the whole pair push for the rest of the tick. Because the check is `> max` after counting from 1, it actually allows `maxPushTimes + 1` pushes per tick.
  - **Class V2:** it changes entity cramming and separation, so dense mob pens and farms behave differently.
- **`unlimitedPlayerPush`** bypasses the cap when either actor is a player ([#L41](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/features/PushEntityOpt.cpp#L41)).
- **Open issues:** a crash report "EntityPushOpt部分崩服" ([#25](https://github.com/LiteLDev/LeviOptimize/issues/25), open) and a desync report alongside LeviAnticheat ([#27](https://github.com/LiteLDev/LeviOptimize/issues/27), open).

### 4.6 `commands.timingCommand` (on by default)

- Registers `/timing [counts]` at Host permission ([LO/src/features/command/TimingCommand.cpp#L37-L43](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/features/command/TimingCommand.cpp#L37-L43)).
- A detached thread sets `EntitySystems::mEnableTimingCapture`, accumulates per-system ECS timings over 100 ticks, then turns capture off and logs TPS plus the top systems ([#L54-L121](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/features/command/TimingCommand.cpp#L54-L121)).
- **Class V0.** It is not an optimization, and it adds BDS's own timing overhead only while it runs.

---

## 5. Removed features (history, for context)

None of these are in v0.17.0. Defaults are from the last tag that shipped each one.

| Feature (key) | Shipped | Default | BDS target and mechanism | Class |
|---|---|---|---|---|
| **HopperItemOpt** (`optHopperItem`; earlier `fixHopperItem`) | ≤0.1.5 to 0.13.2; removed in 0.14.0 (2026-08-27) | true | **Replaces** `Hopper::_addItem` (never calls the original): a hand-written slot loop that merges or places the item, sets a hopper cooldown and calls `setContainerChanged` ([v0.13.2 HopperItemOpt.cpp#L13-L67](https://github.com/LiteLDev/LeviOptimize/blob/v0.13.2/src/features/HopperItemOpt.cpp#L13-L67)). In v0.13.2 the cooldown block reads the destination hopper's `mLastTick` but assigns the unqualified `mCooldownTime`, which is the hooked *source* hopper's member ([#L54-L62](https://github.com/LiteLDev/LeviOptimize/blob/v0.13.2/src/features/HopperItemOpt.cpp#L54-L62)). v0.1.7 instead called `updateCooldownAfterMove` on the destination ([v0.1.7 HopperItemFix.cpp#L52-L55](https://github.com/LiteLDev/LeviOptimize/blob/v0.1.7/src/levioptimize/features/HopperItemFix.cpp#L52-L55)). Crash report [#16](https://github.com/LiteLDev/LeviOptimize/issues/16). No reason given for the removal ([231e5f1](https://github.com/LiteLDev/LeviOptimize/commit/231e5f168)) | Meant as V0. Reimplementation risk; hopper-chain timing likely V2 in 0.13.x |
| **PacketSenderOpt** (`packetSenderOpt.enable`, `.multiThreadBatch`) | 0.1.9 to 0.8.0; removed in 0.9.0 (2025-09-23) | enable=true, multiThreadBatch=false | **Replaces** `RakNetNetworkPeer::sendPacket` with a 2-buffer RakNet `SendList` (`0xFE` + payload, MediumPriority, channel 0). **Replaces** `NetworkSystem::send`: writes the header varint and `packet.write`, then calls `_sendInternal`. **Replaces** `LoopbackPacketSender::sendToClients`: serializes once, patches the header per recipient, and passes a forged `std::string` layout to `_sendInternal`. `multiThreadBatch` adds a `recursive_mutex`, stashed in `BatchedNetworkPeer`'s cache-line filler, around `sendPacket`/`flush`/`update` ([v0.8.0 PacketSenderOpt.cpp](https://github.com/LiteLDev/LeviOptimize/blob/v0.8.0/src/levioptimize/features/PacketSenderOpt.cpp#L20-L246), [defaults](https://github.com/LiteLDev/LeviOptimize/blob/v0.8.0/src/levioptimize/features/features.h#L69-L73)). It broke chat and `mc.broadcast` ([#15](https://github.com/LiteLDev/LeviOptimize/issues/15)). The maintainer said it "broke on 1.21.102.1" | Meant as V0/V1 (bytes the same, RakNet priority possibly different). Broke in practice |
| **PlayerLookupOpt** (`playerLookupOpt`) | 0.1.9 to 0.5.0; removed in 0.5.1 (2025-03-06) | true | Wraps the three `Level::$getPlayer` overloads (UUID, `ActorUniqueID`, name) with hash-map caches. The caches are maintained by hooks on `ServerLevel::_onGameplayUserAdded`/`_onGameplayUserRemoved`, and a miss falls through to the original ([v0.5.0 PlayerLookupOpt.cpp#L21-L113](https://github.com/LiteLDev/LeviOptimize/blob/v0.5.0/src/levioptimize/features/PlayerLookupOpt.cpp#L21-L113)). 0.2.2 fixed a cache crash | V0 intended (name key is the exact `getRealName`) |
| **TimerFix** (`fixTimer`) | 0.4.0-rc.1 to 0.4.0; removed in 0.5.0 (2025-03-02) | true | Hooks `std::make_unique<Timer>(int const&)` by mangled symbol to build a `FixedTimer`, and **replaces** `Timer::advanceTime` so the last timestamp is kept as a `double` instead of a `float` ([v0.4.0 TimerFix.cpp#L16-L91](https://github.com/LiteLDev/LeviOptimize/blob/v0.4.0/src/levioptimize/features/TimerFix.cpp#L16-L91)) | V1 (tick pacing only) |
| **PowerAssociationMapLeakFix** (`fixPowerAssocMapLeak`) | 0.4.0-rc.1 to 0.4.0; removed in 0.5.0 | true in rc.1, false in 0.4.0 | **Replaces** `CircuitSceneGraph::removeStaleRelationships`. The loop dereferences the iterator that `erase()` returned, which may be `end()`, and calls `removeSource` on the *next* component ([v0.4.0 PowerAssociationMapLeakFix.cpp#L19-L33](https://github.com/LiteLDev/LeviOptimize/blob/v0.4.0/src/levioptimize/features/PowerAssociationMapLeakFix.cpp#L19-L33)) | V2 risk (redstone) |
| **BlockLookupOpt** (`optBlockLookup`) | ≤0.1.5 to 0.1.7; removed in 0.1.8 (2024-03-29) | true | Wraps `BlockTypeRegistry::getDefaultBlockState`, `_lookupByNameImpl` and `lookupByName` with phmap caches built from `mBlockLookupMap` ([v0.1.7 BlockLookupOpt.cpp](https://github.com/LiteLDev/LeviOptimize/blob/v0.1.7/src/levioptimize/features/BlockLookupOpt.cpp#L9-L122)). The default-state cache stores `getRenderBlock()` | V0 intended |
| ItemStackUserDataOpt and InventoryTransactionPatch | PR [#17](https://github.com/LiteLDev/LeviOptimize/pull/17), merged 2025-05-28 and reverted 2025-05-31 ([c7104eb](https://github.com/LiteLDev/LeviOptimize/commit/c7104ebed)) | — | Never in a tagged release (absent from the v0.6.0 and v0.6.1 trees) | — |

---

## 6. LeviLamina core items that matter for a Windows comparison

These ship with the loader itself, not with LeviOptimize, so every LeviLamina server runs them.

- **mimalloc allocator swap (server, Windows, unconditional, no config switch).**
  - At static init, `ModifyMemoryAllocator.cpp` overwrites BDS's `Bedrock::Memory` allocator pointer (`MemoryTrackerAnon::memoryAllocator`, an `std::atomic<IMemoryAllocator*>`) with LeviLamina's default allocator ([LL/src/ll/core/tweak/ModifyMemoryAllocator.cpp#L4-L19](https://github.com/LiteLDev/LeviLamina/blob/v26.51.6/src/ll/core/tweak/ModifyMemoryAllocator.cpp#L4-L19)).
  - On the server that allocator is `MimallocMemoryAllocator` (`mi_malloc`, `mi_free`, `mi_realloc`, `mi_malloc_aligned`) ([LL/src/ll/core/tweak/MemoryOperators_win.cpp#L32-L75](https://github.com/LiteLDev/LeviLamina/blob/v26.51.6/src/ll/core/tweak/MemoryOperators_win.cpp#L32-L75), [#L184-L196](https://github.com/LiteLDev/LeviLamina/blob/v26.51.6/src/ll/core/tweak/MemoryOperators_win.cpp#L184-L196)). The client uses the CRT allocator because "mimalloc crashes on client".
  - mimalloc version is **v3.5.0** ([LL/xmake.lua#L24](https://github.com/LiteLDev/LeviLamina/blob/v26.51.6/xmake.lua#L24)). Mods that define `LL_MEMORY_OPERATORS` route their own `new`/`delete` to the same allocator; LeviOptimize does ([LO/src/LeviOptimize.cpp#L1-L2](https://github.com/LiteLDev/LeviOptimize/blob/v0.17.0/src/LeviOptimize.cpp#L1-L2)).
  - **Difference from Endstone:** Endstone preloads mimalloc 3.5.1 as a process-wide override on Linux only. LeviLamina replaces only the allocator behind BDS's `Bedrock::Memory` layer, but does so on Windows.
- **VulnerabilityFixes** (all on by default; [LL/src-server/ll/core/TargetedConfig.h#L29-L36](https://github.com/LiteLDev/LeviLamina/blob/v26.51.6/src-server/ll/core/TargetedConfig.h#L29-L36), [VulnerabilityFixes.cpp](https://github.com/LiteLDev/LeviLamina/blob/v26.51.6/src-server/ll/core/tweak/VulnerabilityFixes.cpp)). These are security fixes, not performance work:
  - drop `SubChunkRequestPacket` with more than 1000 offsets;
  - hard-close the connection after `disconnectClientWithMessage`;
  - a stricter `Item::readUserData`;
  - run `reloadpacketlimitconfig` at startup.
- **SimulatedPlayer chunk loading** (`simulatedPlayerChunkLoading = true`): non-GameTest `SimulatedPlayer`s use `ServerPlayer` chunk loading with a radius of the server tick range ([SimulatedPlayerFixes.cpp](https://github.com/LiteLDev/LeviLamina/blob/v26.51.6/src/ll/core/tweak/SimulatedPlayerFixes.cpp#L36-L52)). This only matters if a benchmark spawns bots as SimulatedPlayers. Endstone's harness uses network clients.

---

## 7. Overlap with Endstone's fixes

| Endstone fix | LeviOptimize v0.17.0 | Removed LeviOptimize features | LeviLamina core |
|---|---|---|---|
| O(1) autosave chunk pick | none | none | none |
| Cached player list in `Dimension::forEachPlayer` | none | **Similar idea, different target:** PlayerLookupOpt cached `Level::getPlayer` by UUID, unique ID and name (removed in 0.5.1) | none |
| Skip unsendable queued chunks in the chunk publisher | none | none | none |
| Locator-bar skip | none. `fixChunkLeak` touches locator-*map* tracking (`MapItemSavedData`), a different subsystem from the locator bar | none | none |
| Random-tick cache warm-up | none | none | none |
| Raised chunk-blending and existence cache caps | none | none | none |
| libdeflate compression | none | PacketSenderOpt worked in the send path but did no compression | none |
| Peer-chain splice | none | **Adjacent:** PacketSenderOpt replaced the RakNet peer send and locked `BatchedNetworkPeer`, but did not splice the chain (removed in 0.9.0) | none |
| Wake idle chunk-generation workers (minBacklog 0) | none | none | none |
| Scheduler idle threads | none | none | none |
| mimalloc on Linux | none | none | **Yes, on the other platform:** mimalloc v3.5.0 on Windows, always on (§6) |

LeviOptimize's current features have no counterpart in Endstone's list either: moving-block packet trimming, push caps, the exposure cache, and the map-tracking leak fix.

---

## 8. Published benchmarks

- **LeviOptimize:** none. The README and CHANGELOG make only qualitative claims, such as "notable performance improvements", "Improves FPS and TPS" and "reducing TPS drops from entity cramming". The release notes are copies of the CHANGELOG. No issue or PR contains measurements. No methodology is stated anywhere.
- **LeviLamina:** none in the README, docs or CHANGELOG. The only performance-related changelog line is "Fixed the performance issue in 1.2.0-rc.2".
- Web searches in English and Chinese for LeviOptimize TPS or benchmark results found nothing.

---

## 9. Notes for a fair Windows comparison

- "LeviLamina + LeviOptimize" with fresh defaults amounts to BDS 1.26.51.1, plus mimalloc (from LeviLamina), plus `fixChunkLeak`, which only runs on disconnect. Endstone's published Windows numbers are on BDS 1.26.52.
- Turning on every LeviOptimize option adds work only for pistons moving block entities, same-tick explosions, and dense entity pushing. `maxPushTimes`, `optSeenPercent` and `optMovingBlock` are V2, so a run with them on is no longer vanilla-equivalent.
- To isolate LeviLamina's allocator effect on Windows, a LeviLamina-only run (no LeviOptimize) is the baseline. LeviLamina's allocator swap cannot be turned off by config.
