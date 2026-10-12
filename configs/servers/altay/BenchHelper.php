<?php

// SPDX-License-Identifier: Apache-2.0

/**
 * Console commands for bedrock-bench, loaded by Altay as a script plugin.
 *
 * gametime   prints "Gametime is <n>", the server's tick counter: it advances once per executed tick.
 * positions  prints "positions: <n>", then "position: <name> <x> <y> <z> <dimension>" for every online player.
 *
 * It also lifts the RakNet transport's per-IP packet limit, which the harness's bots (all on 127.0.0.1) would
 * otherwise trip.
 *
 * @name BenchHelper
 * @main bedrockbench\BenchHelper
 * @version 1.0.0
 * @api 5.0.0
 */

declare(strict_types=1);

namespace bedrockbench;

use pocketmine\command\Command;
use pocketmine\command\CommandSender;
use pocketmine\network\mcpe\transport\TransportNetworkInterface;
use pocketmine\permission\DefaultPermissions;
use pocketmine\plugin\PluginBase;
use pocketmine\scheduler\ClosureTask;
use function count;
use function sprintf;

final class BenchHelper extends PluginBase{

	protected function onEnable() : void{
		$this->getServer()->getCommandMap()->registerAll("benchhelper", [
			new HelperCommand("gametime", "Prints the server tick counter", function(CommandSender $sender) : void{
				$sender->sendMessage("Gametime is " . $this->getServer()->getTick());
			}),
			new HelperCommand("positions", "Prints every online player's position", function(CommandSender $sender) : void{
				$players = $this->getServer()->getOnlinePlayers();
				$sender->sendMessage("positions: " . count($players));
				foreach($players as $player){
					$position = $player->getPosition();
					// %F ignores the locale; worlds are not dimensions, so always 0.
					$sender->sendMessage(sprintf("position: %s %.2F %.2F %.2F 0", $player->getName(), $position->getX(), $position->getY(), $position->getZ()));
				}
			}),
		]);
		// The network interfaces are registered after plugins are enabled.
		$this->getScheduler()->scheduleDelayedTask(new ClosureTask(function() : void{
			foreach($this->getServer()->getNetwork()->getInterfaces() as $interface){
				if($interface instanceof TransportNetworkInterface){
					// The limit reaches the transport thread as a signed 32-bit integer; PHP_INT_MAX would wrap to -1.
					$interface->setPacketLimit(0x7fffffff);
					$this->getLogger()->info("RakNet per-IP packet limit lifted");
				}
			}
		}), 20);
	}
}

final class HelperCommand extends Command{

	/**
	 * @phpstan-param \Closure(CommandSender) : void $handler
	 */
	public function __construct(string $name, string $description, private \Closure $handler){
		parent::__construct($name, $description);
		$this->setPermission(DefaultPermissions::ROOT_OPERATOR);
	}

	public function execute(CommandSender $sender, string $commandLabel, array $args) : bool{
		($this->handler)($sender);
		return true;
	}
}
