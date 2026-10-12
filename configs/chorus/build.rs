// SPDX-License-Identifier: Apache-2.0
// Embeds the Chorus commit and version from Cargo.lock, and the compiler, for the banner and -version.

use std::env;
use std::fs;
use std::path::Path;
use std::process::Command;

fn main() {
    let lock = Path::new(&env::var("CARGO_MANIFEST_DIR").unwrap()).join("Cargo.lock");
    println!("cargo:rerun-if-changed={}", lock.display());
    let text = fs::read_to_string(&lock).expect("chorusbench is built with its Cargo.lock");
    let (version, commit) =
        locked_chorus(&text).expect("Cargo.lock has no chorus package from git");
    println!("cargo:rustc-env=CHORUS_VERSION={version}");
    println!("cargo:rustc-env=CHORUS_COMMIT={commit}");

    let rustc = env::var("RUSTC").unwrap_or_else(|_| String::from("rustc"));
    let output = Command::new(rustc)
        .arg("--version")
        .output()
        .expect("rustc --version");
    println!(
        "cargo:rustc-env=CHORUSBENCH_RUSTC={}",
        String::from_utf8_lossy(&output.stdout).trim()
    );
    println!(
        "cargo:rustc-env=CHORUSBENCH_TARGET={}",
        env::var("TARGET").unwrap()
    );
}

fn locked_chorus(lock: &str) -> Option<(String, String)> {
    for package in lock.split("[[package]]") {
        let field = |key: &str| {
            let prefix = format!("{key} = \"");
            package
                .lines()
                .find_map(|line| line.strip_prefix(prefix.as_str())?.strip_suffix('"'))
        };
        if field("name") == Some("chorus") {
            let commit = field("source")?.rsplit_once('#')?.1;
            return Some((field("version")?.to_owned(), commit.to_owned()));
        }
    }
    None
}
