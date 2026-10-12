// SPDX-License-Identifier: Apache-2.0
package bench;

import cn.nukkit.Player;
import cn.nukkit.command.Command;
import cn.nukkit.command.CommandSender;
import cn.nukkit.plugin.PluginBase;

import java.util.Collection;
import java.util.Locale;

/**
 * Console commands for bedrock-bench on Nukkit-MOT.
 *
 * <ul>
 *   <li>{@code gametime}: {@code Gametime is <n>}, the server tick counter; it advances once per executed tick.</li>
 *   <li>{@code positions}: {@code positions: <n>}, then {@code position: <name> <x> <y> <z> <dimension>} per player.</li>
 * </ul>
 */
public final class BenchTick extends PluginBase {
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
