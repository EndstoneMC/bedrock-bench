// SPDX-License-Identifier: Apache-2.0
package bench;

import cn.nukkit.Player;
import cn.nukkit.command.Command;
import cn.nukkit.command.CommandSender;
import cn.nukkit.network.RakNetInterface;
import cn.nukkit.network.SourceInterface;
import cn.nukkit.plugin.PluginBase;
import io.netty.channel.Channel;
import org.cloudburstmc.netty.channel.raknet.config.RakChannelOption;

import java.lang.reflect.Field;
import java.util.Collection;
import java.util.List;
import java.util.Locale;

/**
 * Console commands for bedrock-bench on Nukkit.
 *
 * <ul>
 *   <li>{@code gametime}: {@code Gametime is <n>}, the server tick counter; it advances once per executed tick.</li>
 *   <li>{@code positions}: {@code positions: <n>}, then {@code position: <name> <x> <y> <z> <dimension>} per player.</li>
 * </ul>
 *
 * <p>It also lifts the per-IP packet limit of the RakNet listener, which the harness's bots, all on 127.0.0.1, would
 * otherwise trip. Nukkit has no setting for it, so the channel option is set by reflection, which ties this plugin
 * to the pinned Nukkit commit.
 */
public final class BenchTick extends PluginBase {
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
}
