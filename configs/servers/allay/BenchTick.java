// SPDX-License-Identifier: Apache-2.0
package bench;

import org.allaymc.api.command.Command;
import org.allaymc.api.command.tree.CommandTree;
import org.allaymc.api.plugin.Plugin;
import org.allaymc.api.registry.Registries;
import org.allaymc.api.server.Server;
import org.allaymc.server.terminal.AllayTerminalConsole;

import java.util.ArrayList;
import java.util.Locale;

/**
 * Console commands for bedrock-bench on Allay.
 *
 * <ul>
 *   <li>{@code gametime}: {@code Gametime is <n>}, the default world's tick counter; it advances once per executed
 *   world tick and continues from the time saved with the world.</li>
 *   <li>{@code positions}: {@code positions: <n>}, then {@code position: <name> <x> <y> <z> <dimension>} per player,
 *   at feet height.</li>
 * </ul>
 *
 * <p>Allay starts its console only when {@code System.console()} exists, which current JDKs do not give a process
 * whose stdin is a pipe. In that case this plugin starts Allay's own console once the server runs, which reads
 * commands from stdin. That console class is not API, which ties this plugin to the pinned Allay release.
 */
public final class BenchTick extends Plugin {
    @Override
    public void onEnable() {
        Registries.COMMANDS.register(new GameTime());
        Registries.COMMANDS.register(new Positions());
        if (System.console() == null) {
            var server = Server.getInstance();
            server.getScheduler().runLater(this, () ->
                    Thread.ofVirtual().name("Console Thread").start(new AllayTerminalConsole(server)::start));
        }
    }

    static final class GameTime extends Command {
        GameTime() {
            super("gametime", "Prints the default world's tick counter", "bench.command.gametime");
        }

        @Override
        public void prepareCommandTree(CommandTree tree) {
            tree.getRoot().exec(context -> {
                var world = Server.getInstance().getWorldPool().getDefaultWorld();
                context.getSender().sendMessage("Gametime is " + world.getTick());
                return context.success();
            });
        }
    }

    static final class Positions extends Command {
        Positions() {
            super("positions", "Prints every player's position", "bench.command.positions");
        }

        @Override
        public void prepareCommandTree(CommandTree tree) {
            tree.getRoot().exec(context -> {
                var lines = new ArrayList<String>();
                for (var player : Server.getInstance().getPlayerManager().getPlayers().values()) {
                    var entity = player.getControlledEntity();
                    if (entity == null) {
                        continue;
                    }
                    var loc = entity.getLocation();
                    lines.add(String.format(Locale.ROOT, "position: %s %.2f %.2f %.2f %d", player.getOriginName(),
                            loc.x(), loc.y(), loc.z(), loc.dimension().getDimensionType().getId()));
                }
                var sender = context.getSender();
                sender.sendMessage("positions: " + lines.size());
                lines.forEach(sender::sendMessage);
                return context.success();
            });
        }
    }
}
