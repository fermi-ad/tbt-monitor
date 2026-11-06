//! Headless Delivery Ring tune measurement service and offline replay interface.

use std::path::PathBuf;

use anyhow::Result;
use clap::{Parser, Subcommand};
use tbt_monitor::tune::{
    MeasurementSpec,
    runtime::{self, ServeOptions},
};

#[derive(Parser)]
#[command(
    name = "tbt-processor",
    version,
    about = "Delivery Ring tune processor"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Read raw node streams, process complete captures, and publish derived PV streams.
    Serve {
        #[arg(long)]
        sources: PathBuf,
        #[arg(long, default_value = "/state")]
        state_dir: PathBuf,
        #[arg(long, default_value = "redis://127.0.0.1:6391/")]
        redis_url: String,
    },
    /// Print the complete, versioned default measurement specification.
    Defaults,
    /// Print the measurement specification JSON schema, including defaults.
    Schema,
    /// Analyze a native SpillInput JSON or a captured-spill bundle without Redis.
    #[command(alias = "replay")]
    Analyze {
        #[arg(long, alias = "bundle")]
        input: PathBuf,
        #[arg(long)]
        sources: Option<PathBuf>,
        #[arg(long)]
        spec: Option<PathBuf>,
        #[arg(long)]
        output: Option<PathBuf>,
    },
    /// Verify a recent successfully published heartbeat in the state directory.
    Healthcheck {
        #[arg(long, default_value = "/state")]
        state_dir: PathBuf,
        #[arg(long, default_value_t = 5000)]
        max_age_ms: u64,
    },
}

fn main() -> Result<()> {
    match Cli::parse().command {
        Command::Serve {
            sources,
            state_dir,
            redis_url,
        } => runtime::serve(ServeOptions {
            sources,
            state_dir,
            redis_url,
        }),
        Command::Defaults => {
            println!(
                "{}",
                serde_json::to_string_pretty(&MeasurementSpec::default())?
            );
            Ok(())
        }
        Command::Schema => {
            println!(
                "{}",
                serde_json::to_string_pretty(&MeasurementSpec::schema())?
            );
            Ok(())
        }
        Command::Analyze {
            input,
            sources,
            spec,
            output,
        } => runtime::analyze_file(
            &input,
            sources.as_deref(),
            spec.as_deref(),
            output.as_deref(),
        ),
        Command::Healthcheck {
            state_dir,
            max_age_ms,
        } => runtime::healthcheck(&state_dir, max_age_ms),
    }
}
