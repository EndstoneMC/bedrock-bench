// SPDX-License-Identifier: Apache-2.0
//! chorusbench: the Chorus server with the console commands bedrock-bench needs.
//!
//! chorusbench              start the server from chorus.toml in the working directory
//! chorusbench -version     print build information as JSON and exit
//! chorusbench -defaults    print the default chorus.toml and exit

use bedrock::protocol::ProtoVersion;
use bevy_app::Startup;
use bevy_ecs::prelude::{IntoScheduleConfigs, Res, ResMut, With};
use bevy_tasks::{AsyncComputeTaskPool, ComputeTaskPool, IoTaskPool};
use chorus::Chorus;
use chorus::command::args::CommandArgs;
use chorus::command::command_definition::CommandDefinition;
use chorus::command::command_result::CommandResult;
use chorus::command::context::CommandContext;
use chorus::command::parameter::{ArgumentType, CommandParameter};
use chorus::config::Config;
use chorus::entity::components::actor_id::ActorId;
use chorus::entity::components::transform::Transform;
use chorus::level::DimensionId;
use chorus::network::BedrockProtocol;
use chorus::network::session::Session;
use chorus::player::identity::PlayerIdentity;
use chorus::player::{PLAYER_EYE_HEIGHT, PendingTeleport, Player, teleport};
use chorus::registry::command_registry::CommandRegistry;
use chorus::server::ServerState;
use chorus_core::permission::PermissionLevel;
use glam::Vec3;
use serde_json::json;

const VERSION: &str = env!("CARGO_PKG_VERSION");
const CHORUS_VERSION: &str = env!("CHORUS_VERSION");
const CHORUS_COMMIT: &str = env!("CHORUS_COMMIT");
const RUSTC: &str = env!("CHORUSBENCH_RUSTC");
const TARGET: &str = env!("CHORUSBENCH_TARGET");
const FACTS: &str = "chorusbench.json";

fn build_info() -> serde_json::Value {
    json!({
        "module": "chorusbench",
        "version": VERSION,
        "chorus": &CHORUS_COMMIT[..7],
        "chorus_commit": CHORUS_COMMIT,
        "chorus_version": CHORUS_VERSION,
        "protocol": BedrockProtocol::PROTOCOL_VERSION,
        "minecraft": BedrockProtocol::GAME_VERSION,
        "rustc": RUSTC,
        "target": TARGET,
    })
}

fn banner() -> String {
    format!(
        "chorusbench {} (chorus {CHORUS_VERSION}, Minecraft {}, protocol {}, rustc {})",
        &CHORUS_COMMIT[..7],
        BedrockProtocol::GAME_VERSION,
        BedrockProtocol::PROTOCOL_VERSION,
        RUSTC.split_whitespace().nth(1).unwrap_or(RUSTC),
    )
}

fn feet(transform: &Transform) -> Vec3 {
    transform.position - Vec3::new(0.0, PLAYER_EYE_HEIGHT, 0.0)
}

// Failures go to the log at error level: Chorus prints command errors like any other reply.
fn report(context: &mut CommandContext, result: Result<String, String>) -> CommandResult {
    match result {
        Ok(reply) => context.reply(reply),
        Err(message) => tracing::error!("{message}"),
    }
    Ok(())
}

fn gametime(context: &mut CommandContext, _: &CommandArgs) -> CommandResult {
    let tick = context.resource::<ServerState>().tick();
    context.reply(format!("Gametime is {tick}"));
    Ok(())
}

fn positions(context: &mut CommandContext, _: &CommandArgs) -> CommandResult {
    let world = context.world_mut();
    let mut query =
        world.query_filtered::<(&PlayerIdentity, &Transform, &DimensionId), With<Player>>();
    let mut rows: Vec<String> = query
        .iter(world)
        .map(|(identity, transform, dimension)| {
            let at = feet(transform);
            format!(
                "position: {} {:.2} {:.2} {:.2} {}",
                identity.name(),
                at.x,
                at.y,
                at.z,
                dimension.0
            )
        })
        .collect();
    rows.sort();
    context.reply(format!("positions: {}", rows.len()));
    for row in rows {
        context.reply(row);
    }
    Ok(())
}

fn teleport_player(context: &mut CommandContext, args: &CommandArgs) -> Result<String, String> {
    let name = args.string("player").unwrap_or_default().to_owned();
    let position = args.position("destination").ok_or("missing destination")?;
    let world = context.world_mut();
    let mut query = world.query::<(
        &PlayerIdentity,
        &mut Session,
        &ActorId,
        &mut Transform,
        &mut PendingTeleport,
    )>();
    let (_, mut session, actor, mut transform, mut pending) = query
        .iter_mut(world)
        .find(|(identity, ..)| identity.name().eq_ignore_ascii_case(&name))
        .ok_or_else(|| format!("No player named \"{name}\" is online."))?;
    let destination = position
        .resolve(feet(&transform).as_dvec3(), transform.rotation)
        .as_vec3();
    teleport(
        &mut session,
        actor,
        &mut transform,
        &mut pending,
        destination,
    );
    Ok(format!(
        "Teleported {name} to {:.2}, {:.2}, {:.2}",
        destination.x, destination.y, destination.z
    ))
}

fn kick_player(context: &mut CommandContext, args: &CommandArgs) -> Result<String, String> {
    let name = args.string("player").unwrap_or_default().to_owned();
    let reason = args
        .string("reason")
        .unwrap_or("Kicked by an operator")
        .to_owned();
    let world = context.world_mut();
    let mut query = world.query::<(&PlayerIdentity, &mut Session)>();
    let (_, mut session) = query
        .iter_mut(world)
        .find(|(identity, _)| identity.name().eq_ignore_ascii_case(&name))
        .ok_or_else(|| format!("No player named \"{name}\" is online."))?;
    session.close(Some(&reason));
    Ok(format!("Kicked {name}: {reason}"))
}

const GAMETIME: CommandDefinition =
    CommandDefinition::new("gametime", "Prints the number of ticks run", gametime)
        .permission(PermissionLevel::Operator);

const POSITIONS: CommandDefinition =
    CommandDefinition::new("positions", "Prints every player's position", positions)
        .permission(PermissionLevel::Operator);

const TPP: CommandDefinition = CommandDefinition::new(
    "tpp",
    "Teleports a player to a position",
    |context, args| {
        let result = teleport_player(context, args);
        report(context, result)
    },
)
.permission(PermissionLevel::Operator)
.overloads(chorus::overloads![[
    CommandParameter::new("player", ArgumentType::Target),
    CommandParameter::new("destination", ArgumentType::Position),
]]);

const KICK: CommandDefinition =
    CommandDefinition::new("kick", "Disconnects a player", |context, args| {
        let result = kick_player(context, args);
        report(context, result)
    })
    .permission(PermissionLevel::Operator)
    .overloads(chorus::overloads![[
        CommandParameter::new("player", ArgumentType::Target),
        CommandParameter::new("reason", ArgumentType::Message).optional(),
    ]]);

const COMMANDS: &[&CommandDefinition] = &[&GAMETIME, &POSITIONS, &TPP, &KICK];

fn register(mut registry: ResMut<CommandRegistry>) {
    registry.register_all(COMMANDS.iter().copied());
}

fn write_facts(config: Res<Config>) {
    let facts = json!({
        "build": build_info(),
        "threads_setting": config.server.threads,
        "threads": config.server.threads.count(),
        "available_parallelism": std::thread::available_parallelism().map_or(0, |count| count.get()),
        "compute_threads": ComputeTaskPool::get().thread_num(),
        "async_compute_threads": AsyncComputeTaskPool::get().thread_num(),
        "io_threads": IoTaskPool::get().thread_num(),
        "transport": config.network.transport,
        "compression": config.network.compression,
        "zlib": {"level": config.network.zlib.level, "threshold": config.network.zlib.threshold},
        "raknet_encryption": config.network.raknet.encryption,
        "max_view_distance": config.level.max_view_distance,
        "chunk_saving": config.level.chunk_saving,
        "autosave": config.level.autosave,
    });
    let text = serde_json::to_string_pretty(&facts).unwrap_or_default();
    if let Err(err) = std::fs::write(FACTS, text + "\n") {
        tracing::error!("cannot write {FACTS}: {err}");
    }
}

fn main() {
    match std::env::args().nth(1).as_deref() {
        None => {}
        Some("-version") => {
            println!("{}", build_info());
            return;
        }
        Some("-defaults") => {
            print!(
                "{}",
                toml::to_string(&Config::default()).expect("the default config serializes")
            );
            return;
        }
        Some(_) => {
            eprintln!("usage: chorusbench [-version | -defaults]");
            std::process::exit(2);
        }
    }
    println!("{}", banner());
    let mut app = Chorus::init();
    app.add_systems(
        Startup,
        (register.after(CommandRegistry::init), write_facts),
    );
    app.run();
}
