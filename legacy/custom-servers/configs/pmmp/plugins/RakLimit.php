<?php

/**
 * @name RakLimit
 * @main raklimit\RakLimit
 * @version 1.0.0
 * @api 5.0.0
 */

namespace raklimit;

use pocketmine\network\mcpe\raklib\RakLibInterface;
use pocketmine\plugin\PluginBase;
use pocketmine\scheduler\ClosureTask;

class RakLimit extends PluginBase{
	public function onEnable() : void{
		$this->getScheduler()->scheduleDelayedTask(new ClosureTask(function() : void{ $this->apply(); }), 20);
	}

	private function apply() : void{
		foreach($this->getServer()->getNetwork()->getInterfaces() as $interface){
			if($interface instanceof RakLibInterface){
				$interface->setPacketLimit(PHP_INT_MAX);
				$this->getLogger()->info("RakLib per-IP packet limit disabled");
			}
		}
	}
}
