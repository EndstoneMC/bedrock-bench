// SPDX-License-Identifier: Apache-2.0
package bench;

import cn.nukkit.Player;
import cn.nukkit.block.Block;
import cn.nukkit.command.Command;
import cn.nukkit.command.CommandSender;
import cn.nukkit.level.Level;
import cn.nukkit.level.Location;
import cn.nukkit.math.Vector3;
import cn.nukkit.network.RakNetInterface;
import cn.nukkit.network.SourceInterface;
import cn.nukkit.plugin.PluginBase;
import io.netty.channel.Channel;
import org.cloudburstmc.netty.channel.raknet.config.RakChannelOption;

import java.lang.reflect.Field;
import java.util.Collection;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Console commands for bedrock-bench on Nukkit.
 *
 * <ul>
 *   <li>{@code gametime}: {@code Gametime is <n>}, the server tick counter; it advances once per executed tick.</li>
 *   <li>{@code positions}: {@code positions: <n>}, then {@code position: <name> <x> <y> <z> <dimension>} per player.</li>
 *   <li>{@code fillnear}: fills a box relative to a player (the chaos block actions).</li>
 *   <li>{@code dim}: moves a player to another dimension (the chaos dimension action).</li>
 *   <li>{@code pregen}: builds and checks a pre-generated world, see {@link PreGen}.</li>
 * </ul>
 *
 * <p>It also lifts the per-IP packet limit of the RakNet listener, which the harness's bots, all on 127.0.0.1, would
 * otherwise trip. Nukkit has no setting for it, so the channel option is set by reflection, which ties this plugin
 * to the pinned Nukkit commit.
 */
public final class BenchTick extends PluginBase {
    private static final Map<String, Integer> BLOCKS = Map.of(
            "grass_block", Block.GRASS,
            "farmland", Block.FARMLAND,
            "oak_leaves", Block.LEAVES,
            "oak_sapling", Block.SAPLING);

    @Override
    public void onEnable() {
        // The RakNet interface is registered after plugins are enabled.
        getServer().getScheduler().scheduleDelayedTask(this, this::liftPacketLimit, 20);
    }

    private void liftPacketLimit() {
        for (SourceInterface network : getServer().getNetwork().getInterfaces()) {
            if (!(network instanceof RakNetInterface)) {
                continue;
            }
            try {
                Field field = RakNetInterface.class.getDeclaredField("channels");
                field.setAccessible(true);
                for (Object channel : (List<?>) field.get(network)) {
                    ((Channel) channel).config().setOption(RakChannelOption.RAK_PACKET_LIMIT, Integer.MAX_VALUE);
                }
                getLogger().info("RakNet per-IP packet limit lifted");
            } catch (ReflectiveOperationException | RuntimeException e) {
                getLogger().warning("RakNet per-IP packet limit not lifted: " + e);
            }
        }
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        switch (command.getName()) {
            case "gametime":
                sender.sendMessage("Gametime is " + getServer().getTick());
                return true;
            case "positions":
                positions(sender);
                return true;
            case "fillnear":
                return fillNear(sender, args);
            case "dim":
                return dimension(sender, args);
            case "pregen":
                return PreGen.start(this, sender, args);
            default:
                return false;
        }
    }

    private void positions(CommandSender sender) {
        Collection<Player> players = getServer().getOnlinePlayers().values();
        sender.sendMessage("positions: " + players.size());
        for (Player player : players) {
            sender.sendMessage(String.format(Locale.ROOT, "position: %s %.2f %.2f %.2f %d", player.getName(),
                    player.getX(), player.getY(), player.getZ(), player.getLevel().getDimension()));
        }
    }

    private boolean fillNear(CommandSender sender, String[] args) {
        if (args.length != 8) {
            return false;
        }
        Player player = getServer().getPlayerExact(args[0]);
        Integer id = BLOCKS.get(args[7]);
        if (player == null || id == null) {
            sender.sendMessage("No targets matched");
            return true;
        }
        int[] box = new int[6];
        try {
            for (int i = 0; i < 6; i++) {
                box[i] = Integer.parseInt(args[i + 1]);
            }
        } catch (NumberFormatException e) {
            return false;
        }
        Level level = player.getLevel();
        int bx = player.getFloorX(), by = player.getFloorY(), bz = player.getFloorZ(), filled = 0;
        for (int x = Math.min(box[0], box[3]); x <= Math.max(box[0], box[3]); x++) {
            for (int y = Math.min(box[1], box[4]); y <= Math.max(box[1], box[4]); y++) {
                for (int z = Math.min(box[2], box[5]); z <= Math.max(box[2], box[5]); z++) {
                    int height = by + y;
                    if (height < level.getMinBlockY() || height > level.getMaxBlockY()) {
                        continue;
                    }
                    level.setBlock(new Vector3(bx + x, height, bz + z), Block.get(id), true, true);
                    filled++;
                }
            }
        }
        sender.sendMessage(filled + " blocks filled near " + player.getName());
        return true;
    }

    private boolean dimension(CommandSender sender, String[] args) {
        if (args.length != 5) {
            return false;
        }
        Player player = getServer().getPlayerExact(args[0]);
        String name = args[1].equals("overworld") ? getServer().getDefaultLevel().getName() : args[1];
        Level level = getServer().getLevelByName(name);
        if (player == null || level == null) {
            sender.sendMessage("No targets matched");
            return true;
        }
        try {
            player.teleport(new Location(Double.parseDouble(args[2]), Double.parseDouble(args[3]),
                    Double.parseDouble(args[4]), level));
        } catch (NumberFormatException e) {
            return false;
        }
        sender.sendMessage("Moved " + player.getName() + " to " + level.getName());
        return true;
    }
}
