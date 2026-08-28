//! CLI entrypoint for `tbt-monitor-tui`.
//!
//! This module is intentionally thin: it parses command-line arguments, applies
//! command-specific config overrides, and dispatches to feature modules:
//! - `importer` for XML -> monitor config generation
//! - `monitor` for live stream monitoring/TUI updates
//! - `analyze` for synchronized spill analysis and batch workflows
//!
//! Design intent:
//! Keep orchestration and policy wiring here, while keeping computation and I/O
//! behavior in dedicated modules so changes remain local and testable.

mod analyze;
mod capture;
mod config;
mod importer;
mod monitor;
mod tui;

use std::path::PathBuf;

use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};

use analyze::{
    BatchOptions, BatchRecordFormat, DetailedArtifactsMode, FLASH_COUNT_MAX, ReferenceKey,
    SpillSourceMode, StudyOptions, run_analyze_captured_spill, run_analyze_captured_spills,
    run_analyze_spill, run_analyze_spills, run_analyze_study,
};
use capture::{run_assess, run_capture_spill, run_capture_spills, run_diagnose_captures};
use config::{MonitorConfig, load_monitor_config, save_monitor_config};
use importer::import_xml_config;
use tui::run_dashboard;

#[derive(Debug, Parser)]
#[command(name = "tbt-monitor-tui")]
#[command(version)]
#[command(
    about = "Monitor, capture, and analyze turn-by-turn beam-position data for the Fermilab Mu2e Delivery Ring"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Convert an ACNET XML device config into the monitor's native config file.
    Import {
        #[arg(long)]
        source: PathBuf,

        #[arg(long, default_value = "config/monitor.cfg")]
        output: PathBuf,

        #[arg(long, default_value_t = 1000)]
        xread_block_ms: u64,

        #[arg(long, default_value_t = 2000)]
        reconnect_initial_ms: u64,

        #[arg(long, default_value_t = 30000)]
        reconnect_max_ms: u64,

        #[arg(long, default_value_t = 1)]
        min_stream_values: usize,
    },

    /// Run the live TUI monitor.
    Monitor {
        #[arg(long, default_value = "config/monitor.cfg")]
        config: PathBuf,

        #[arg(long)]
        xread_block_ms: Option<u64>,

        #[arg(long)]
        reconnect_initial_ms: Option<u64>,

        #[arg(long)]
        reconnect_max_ms: Option<u64>,

        #[arg(long)]
        min_stream_values: Option<usize>,
    },

    /// Capture one synchronized raw spill bundle without running tune analysis.
    CaptureSpill {
        #[arg(long, default_value = "config/monitor.cfg")]
        config: PathBuf,

        #[arg(long, default_value = "/out")]
        out_dir: PathBuf,

        /// Override the same-spill millisecond delta for capture and diagnostics.
        #[arg(long)]
        same_spill_tolerance_ms: Option<u64>,

        /// Compatibility alias for --same-spill-tolerance-ms on capture commands.
        #[arg(long)]
        align_tolerance_ms: Option<u64>,

        /// Override the minimum aligned stream fraction before warnings are emitted.
        #[arg(long)]
        min_aligned_fraction: Option<f64>,
    },

    /// Capture synchronized raw spill bundles continuously with --free-run.
    CaptureSpills {
        #[arg(long, default_value = "config/monitor.cfg")]
        config: PathBuf,

        #[arg(long, default_value = "/out")]
        out_dir: PathBuf,

        /// Keep running continuously and save one raw bundle per unique global spill.
        #[arg(long, default_value_t = false)]
        free_run: bool,

        /// In free-run mode, stop after this many successful captures.
        #[arg(long)]
        count: Option<usize>,

        /// Override the same-spill millisecond delta for capture and diagnostics.
        #[arg(long)]
        same_spill_tolerance_ms: Option<u64>,

        /// Compatibility alias for --same-spill-tolerance-ms on capture commands.
        #[arg(long)]
        align_tolerance_ms: Option<u64>,

        /// Override the minimum aligned stream fraction before warnings are emitted.
        #[arg(long)]
        min_aligned_fraction: Option<f64>,
    },

    /// Regenerate capture timing diagnostics from existing captured-spill bundles.
    DiagnoseCaptures {
        /// Directory containing captured-spill bundle directories, one bundle, or one manifest.
        #[arg(long)]
        bundles_dir: PathBuf,

        #[arg(long, default_value = "/out")]
        out_dir: PathBuf,

        /// Override the same-spill millisecond delta used for regenerated diagnostics.
        #[arg(long)]
        same_spill_tolerance_ms: Option<u64>,
    },

    /// Assess configured TBT stream timing without collecting raw payload artifacts.
    Assess {
        #[arg(long, default_value = "config/monitor.cfg")]
        config: PathBuf,

        #[arg(long, default_value = "/out")]
        out_dir: PathBuf,

        /// Number of new machine events to watch after the initial latest-ID snapshot.
        #[arg(long, default_value_t = 1)]
        events: usize,

        /// Override the same-spill millisecond delta for preflight diagnostics.
        #[arg(long)]
        same_spill_tolerance_ms: Option<u64>,

        /// Compatibility alias for --same-spill-tolerance-ms.
        #[arg(long)]
        align_tolerance_ms: Option<u64>,
    },

    /// Run tune analysis for one spill, or continuously with --free-run.
    AnalyzeSpill {
        #[arg(long, default_value = "config/monitor.cfg")]
        config: PathBuf,

        #[arg(long, default_value = "/out")]
        out_dir: PathBuf,

        #[arg(long)]
        align_tolerance_ms: Option<u64>,

        #[arg(long)]
        min_aligned_fraction: Option<f64>,

        #[arg(long)]
        injection_start_turn: Option<usize>,

        #[arg(long)]
        injection_window_turns: Option<usize>,

        #[arg(long)]
        sliding_window_turns: Option<usize>,

        #[arg(long)]
        sliding_stride_turns: Option<usize>,

        /// Number of evenly-spaced flash sampling windows across a spill, or 'max'.
        #[arg(long, short = 'f', value_name = "N|max", value_parser = parse_flashes_arg)]
        flashes: Option<usize>,

        #[arg(long)]
        qx_band_min: Option<f64>,

        #[arg(long)]
        qx_band_max: Option<f64>,

        #[arg(long)]
        qy_band_min: Option<f64>,

        #[arg(long)]
        qy_band_max: Option<f64>,

        #[arg(long)]
        min_peak_confidence: Option<f64>,

        /// Plot time-domain axes in microseconds instead of turns.
        #[arg(long, default_value_t = false)]
        plot_time_axes_in_us: bool,

        /// Keep running continuously and save timestamped artifacts per global spill.
        #[arg(long, default_value_t = false)]
        free_run: bool,

        /// In free-run mode, stop after this many successful analyses.
        #[arg(long)]
        count: Option<usize>,

        /// Use historical stream buffers instead of waiting for new arrivals.
        #[arg(long, default_value_t = false)]
        no_beam: bool,

        /// Number of recent entries to scan per stream in no-beam mode.
        #[arg(long)]
        stale_depth: Option<usize>,
    },

    /// Run tune analysis from one captured-spill bundle without Redis connectivity.
    AnalyzeCapturedSpill {
        #[arg(long, default_value = "config/monitor.cfg")]
        config: PathBuf,

        /// Captured-spill bundle directory, or its manifest.json path.
        #[arg(long)]
        bundle: PathBuf,

        #[arg(long, default_value = "/out")]
        out_dir: PathBuf,

        #[arg(long)]
        align_tolerance_ms: Option<u64>,

        #[arg(long)]
        min_aligned_fraction: Option<f64>,

        #[arg(long)]
        injection_start_turn: Option<usize>,

        #[arg(long)]
        injection_window_turns: Option<usize>,

        #[arg(long)]
        sliding_window_turns: Option<usize>,

        #[arg(long)]
        sliding_stride_turns: Option<usize>,

        /// Number of evenly-spaced flash sampling windows across a spill, or 'max'.
        #[arg(long, short = 'f', value_name = "N|max", value_parser = parse_flashes_arg)]
        flashes: Option<usize>,

        #[arg(long)]
        qx_band_min: Option<f64>,

        #[arg(long)]
        qx_band_max: Option<f64>,

        #[arg(long)]
        qy_band_min: Option<f64>,

        #[arg(long)]
        qy_band_max: Option<f64>,

        #[arg(long)]
        min_peak_confidence: Option<f64>,

        /// Plot time-domain axes in microseconds instead of turns.
        #[arg(long, default_value_t = false)]
        plot_time_axes_in_us: bool,
    },

    /// Run batch tune analysis from captured-spill bundles without Redis connectivity.
    AnalyzeCapturedSpills {
        #[arg(long, default_value = "config/monitor.cfg")]
        config: PathBuf,

        /// Directory containing captured-spill bundle directories, or one bundle/manifest path.
        #[arg(long)]
        bundles_dir: PathBuf,

        #[arg(long, default_value = "/out")]
        out_dir: PathBuf,

        /// Number of successful unique captured spills to analyze.
        #[arg(long)]
        count: usize,

        #[arg(long)]
        align_tolerance_ms: Option<u64>,

        #[arg(long)]
        min_aligned_fraction: Option<f64>,

        #[arg(long)]
        injection_start_turn: Option<usize>,

        #[arg(long)]
        injection_window_turns: Option<usize>,

        #[arg(long)]
        sliding_window_turns: Option<usize>,

        #[arg(long)]
        sliding_stride_turns: Option<usize>,

        /// Number of evenly-spaced flash sampling windows across a spill, or 'max'.
        #[arg(long, short = 'f', value_name = "N|max", value_parser = parse_flashes_arg)]
        flashes: Option<usize>,

        #[arg(long)]
        qx_band_min: Option<f64>,

        #[arg(long)]
        qx_band_max: Option<f64>,

        #[arg(long)]
        qy_band_min: Option<f64>,

        #[arg(long)]
        qy_band_max: Option<f64>,

        #[arg(long)]
        min_peak_confidence: Option<f64>,

        /// Plot time-domain axes in microseconds instead of turns.
        #[arg(long, default_value_t = false)]
        plot_time_axes_in_us: bool,

        #[arg(long, default_value_t = 1.5)]
        min_confidence: f64,

        #[arg(long, default_value_t = 4)]
        min_aligned_bpm_count: usize,

        #[arg(long, default_value_t = 1)]
        min_per_plane_bpm: usize,

        #[arg(long, default_value_t = 0.005)]
        peak_edge_margin: f64,

        #[arg(long, default_value = "both")]
        record_format: String,

        #[arg(long, default_value = "all")]
        detailed_artifacts: String,

        #[arg(long)]
        reference_file: Option<PathBuf>,

        #[arg(long, default_value = "target_ms")]
        reference_key: String,

        #[arg(long, default_value_t = 1)]
        reference_match_tolerance_ms: u64,
    },

    /// Run robustness studies and method comparison artifacts for a single synchronized spill.
    AnalyzePhase {
        #[arg(long, default_value = "config/monitor.cfg")]
        config: PathBuf,

        #[arg(long, default_value = "/out")]
        out_dir: PathBuf,

        #[arg(long, default_value_t = 0)]
        window_start_min: usize,

        #[arg(long, default_value_t = 2048)]
        window_start_max: usize,

        #[arg(long, default_value_t = 128)]
        window_start_step: usize,

        #[arg(long, default_value_t = 512)]
        window_length_min: usize,

        #[arg(long, default_value_t = 2048)]
        window_length_max: usize,

        #[arg(long, default_value_t = 256)]
        window_length_step: usize,

        #[arg(long)]
        reference_start: Option<usize>,

        #[arg(long)]
        reference_length: Option<usize>,

        #[arg(long, default_value_t = 3)]
        svd_modes: usize,

        #[arg(long, default_value_t = true)]
        svd_normalize_bpm: bool,

        #[arg(long)]
        min_peak_confidence: Option<f64>,

        /// Plot time-domain axes in microseconds instead of turns.
        #[arg(long, default_value_t = false)]
        plot_time_axes_in_us: bool,

        #[arg(long, default_value = "findings_summary.md")]
        summary_file: String,

        /// Keep running continuously and save timestamped robustness-study artifacts per global spill.
        #[arg(long, default_value_t = false)]
        free_run: bool,

        /// In free-run mode, stop after this many successful analyses.
        #[arg(long)]
        count: Option<usize>,

        /// Use historical stream buffers instead of waiting for new arrivals.
        #[arg(long, default_value_t = false)]
        no_beam: bool,

        /// Number of recent entries to scan per stream in no-beam mode.
        #[arg(long)]
        stale_depth: Option<usize>,
    },

    /// Run batch tune analysis across multiple synchronized spills.
    AnalyzeSpills {
        #[arg(long, default_value = "config/monitor.cfg")]
        config: PathBuf,

        #[arg(long, default_value = "/out")]
        out_dir: PathBuf,

        /// Number of successful unique spills to analyze.
        #[arg(long)]
        count: usize,

        #[arg(long)]
        align_tolerance_ms: Option<u64>,

        #[arg(long)]
        min_aligned_fraction: Option<f64>,

        #[arg(long)]
        injection_start_turn: Option<usize>,

        #[arg(long)]
        injection_window_turns: Option<usize>,

        #[arg(long)]
        sliding_window_turns: Option<usize>,

        #[arg(long)]
        sliding_stride_turns: Option<usize>,

        /// Number of evenly-spaced flash sampling windows across a spill, or 'max'.
        #[arg(long, short = 'f', value_name = "N|max", value_parser = parse_flashes_arg)]
        flashes: Option<usize>,

        #[arg(long)]
        qx_band_min: Option<f64>,

        #[arg(long)]
        qx_band_max: Option<f64>,

        #[arg(long)]
        qy_band_min: Option<f64>,

        #[arg(long)]
        qy_band_max: Option<f64>,

        #[arg(long)]
        min_peak_confidence: Option<f64>,

        /// Plot time-domain axes in microseconds instead of turns.
        #[arg(long, default_value_t = false)]
        plot_time_axes_in_us: bool,

        #[arg(long, default_value_t = 1.5)]
        min_confidence: f64,

        #[arg(long, default_value_t = 4)]
        min_aligned_bpm_count: usize,

        #[arg(long, default_value_t = 1)]
        min_per_plane_bpm: usize,

        #[arg(long, default_value_t = 0.005)]
        peak_edge_margin: f64,

        #[arg(long, default_value = "both")]
        record_format: String,

        #[arg(long, default_value = "all")]
        detailed_artifacts: String,

        #[arg(long)]
        reference_file: Option<PathBuf>,

        #[arg(long, default_value = "target_ms")]
        reference_key: String,

        #[arg(long, default_value_t = 1)]
        reference_match_tolerance_ms: u64,

        /// Use historical stream buffers instead of waiting for wake events.
        #[arg(long, default_value_t = false)]
        no_beam: bool,

        /// Number of recent entries to scan per stream in no-beam mode.
        #[arg(long)]
        stale_depth: Option<usize>,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Command::Import {
            source,
            output,
            xread_block_ms,
            reconnect_initial_ms,
            reconnect_max_ms,
            min_stream_values,
        } => {
            let (mut config, report) = import_xml_config(&source)
                .with_context(|| format!("failed to parse XML at {}", source.display()))?;

            config.xread_block_ms = normalize_block_ms(xread_block_ms);
            config.reconnect_initial_ms = reconnect_initial_ms.max(250);
            config.reconnect_max_ms = reconnect_max_ms.max(config.reconnect_initial_ms);
            config.min_stream_values = min_stream_values.max(1);

            if let Some(parent) = output.parent() {
                std::fs::create_dir_all(parent).with_context(|| {
                    format!("failed to create parent directory {}", parent.display())
                })?;
            }

            save_monitor_config(&output, &config)
                .with_context(|| format!("failed to write {}", output.display()))?;

            println!(
                "wrote {} devices and {} stream keys to {}",
                report.device_count,
                report.stream_count,
                output.display()
            );
        }
        Command::Monitor {
            config,
            xread_block_ms,
            reconnect_initial_ms,
            reconnect_max_ms,
            min_stream_values,
        } => {
            let mut monitor_config = load_monitor_config(&config)
                .with_context(|| format!("failed to load {}", config.display()))?;

            if let Some(v) = xread_block_ms {
                monitor_config.xread_block_ms = normalize_block_ms(v);
            }
            if let Some(v) = reconnect_initial_ms {
                monitor_config.reconnect_initial_ms = v.max(250);
            }
            if let Some(v) = reconnect_max_ms {
                monitor_config.reconnect_max_ms = v.max(monitor_config.reconnect_initial_ms);
            }
            if let Some(v) = min_stream_values {
                monitor_config.min_stream_values = v.max(1);
            }

            run_dashboard(monitor_config)?;
        }
        Command::CaptureSpill {
            config,
            out_dir,
            same_spill_tolerance_ms,
            align_tolerance_ms,
            min_aligned_fraction,
        } => {
            let mut monitor_config = load_monitor_config(&config)
                .with_context(|| format!("failed to load {}", config.display()))?;

            apply_same_spill_override(
                &mut monitor_config,
                same_spill_tolerance_ms,
                align_tolerance_ms,
            );
            if let Some(v) = min_aligned_fraction {
                monitor_config.min_aligned_fraction = v;
            }

            monitor_config.validate()?;
            run_capture_spill(monitor_config, &out_dir)?;
        }
        Command::CaptureSpills {
            config,
            out_dir,
            free_run,
            count,
            same_spill_tolerance_ms,
            align_tolerance_ms,
            min_aligned_fraction,
        } => {
            if matches!(count, Some(0)) {
                bail!("--count must be >= 1 for capture-spills");
            }

            let mut monitor_config = load_monitor_config(&config)
                .with_context(|| format!("failed to load {}", config.display()))?;

            apply_same_spill_override(
                &mut monitor_config,
                same_spill_tolerance_ms,
                align_tolerance_ms,
            );
            if let Some(v) = min_aligned_fraction {
                monitor_config.min_aligned_fraction = v;
            }

            monitor_config.validate()?;
            run_capture_spills(monitor_config, &out_dir, free_run, count)?;
        }
        Command::DiagnoseCaptures {
            bundles_dir,
            out_dir,
            same_spill_tolerance_ms,
        } => {
            run_diagnose_captures(&bundles_dir, &out_dir, same_spill_tolerance_ms)?;
        }
        Command::Assess {
            config,
            out_dir,
            events,
            same_spill_tolerance_ms,
            align_tolerance_ms,
        } => {
            let mut monitor_config = load_monitor_config(&config)
                .with_context(|| format!("failed to load {}", config.display()))?;
            apply_same_spill_override(
                &mut monitor_config,
                same_spill_tolerance_ms,
                align_tolerance_ms,
            );
            monitor_config.validate()?;
            run_assess(monitor_config, &out_dir, Some(events))?;
        }
        Command::AnalyzeSpill {
            config,
            out_dir,
            align_tolerance_ms,
            min_aligned_fraction,
            injection_start_turn,
            injection_window_turns,
            sliding_window_turns,
            sliding_stride_turns,
            flashes,
            qx_band_min,
            qx_band_max,
            qy_band_min,
            qy_band_max,
            min_peak_confidence,
            plot_time_axes_in_us,
            free_run,
            count,
            no_beam,
            stale_depth,
        } => {
            if !free_run && count.is_some() {
                bail!("--count requires --free-run for analyze-spill");
            }
            if matches!(count, Some(0)) {
                bail!("--count must be >= 1 for analyze-spill");
            }
            if !no_beam && stale_depth.is_some() {
                eprintln!("[warn] analyze-spill: --stale-depth is ignored unless --no-beam is set");
            }
            if flashes.is_some() && sliding_stride_turns.is_some() {
                eprintln!(
                    "[warn] analyze-spill: --sliding-stride-turns is ignored when --flashes is set"
                );
            }
            if flashes.is_some() {
                eprintln!(
                    "[warn] analyze-spill: injection_window_turns is ignored when --flashes is set; using sliding_window_turns"
                );
            }

            let mut monitor_config = load_monitor_config(&config)
                .with_context(|| format!("failed to load {}", config.display()))?;

            if let Some(v) = align_tolerance_ms {
                monitor_config.align_tolerance_ms = v;
            }
            if let Some(v) = min_aligned_fraction {
                monitor_config.min_aligned_fraction = v;
            }
            if let Some(v) = injection_start_turn {
                monitor_config.injection_start_turn = v;
            }
            if let Some(v) = injection_window_turns {
                monitor_config.injection_window_turns = v.max(1);
            }
            if let Some(v) = sliding_window_turns {
                monitor_config.sliding_window_turns = v.max(1);
            }
            if let Some(v) = sliding_stride_turns {
                monitor_config.sliding_stride_turns = v.max(1);
            }
            if let Some(v) = qx_band_min {
                monitor_config.qx_band_min = v;
            }
            if let Some(v) = qx_band_max {
                monitor_config.qx_band_max = v;
            }
            if let Some(v) = qy_band_min {
                monitor_config.qy_band_min = v;
            }
            if let Some(v) = qy_band_max {
                monitor_config.qy_band_max = v;
            }
            if let Some(v) = min_peak_confidence {
                monitor_config.min_peak_confidence = v;
            }
            if plot_time_axes_in_us {
                monitor_config.plot_time_axes_in_us = true;
            }

            monitor_config.validate()?;
            let source_mode = if no_beam {
                SpillSourceMode::Historical {
                    stale_depth: stale_depth.unwrap_or(100).max(1),
                }
            } else {
                SpillSourceMode::LiveLatest
            };
            run_analyze_spill(
                monitor_config,
                &out_dir,
                free_run,
                count,
                source_mode,
                flashes,
            )?;
        }
        Command::AnalyzeCapturedSpill {
            config,
            bundle,
            out_dir,
            align_tolerance_ms,
            min_aligned_fraction,
            injection_start_turn,
            injection_window_turns,
            sliding_window_turns,
            sliding_stride_turns,
            flashes,
            qx_band_min,
            qx_band_max,
            qy_band_min,
            qy_band_max,
            min_peak_confidence,
            plot_time_axes_in_us,
        } => {
            if flashes.is_some() && sliding_stride_turns.is_some() {
                eprintln!(
                    "[warn] analyze-captured-spill: --sliding-stride-turns is ignored when --flashes is set"
                );
            }
            if flashes.is_some() {
                eprintln!(
                    "[warn] analyze-captured-spill: injection_window_turns is ignored when --flashes is set; using sliding_window_turns"
                );
            }

            let mut monitor_config = load_monitor_config(&config)
                .with_context(|| format!("failed to load {}", config.display()))?;

            if let Some(v) = align_tolerance_ms {
                monitor_config.align_tolerance_ms = v;
            }
            if let Some(v) = min_aligned_fraction {
                monitor_config.min_aligned_fraction = v;
            }
            if let Some(v) = injection_start_turn {
                monitor_config.injection_start_turn = v;
            }
            if let Some(v) = injection_window_turns {
                monitor_config.injection_window_turns = v.max(1);
            }
            if let Some(v) = sliding_window_turns {
                monitor_config.sliding_window_turns = v.max(1);
            }
            if let Some(v) = sliding_stride_turns {
                monitor_config.sliding_stride_turns = v.max(1);
            }
            if let Some(v) = qx_band_min {
                monitor_config.qx_band_min = v;
            }
            if let Some(v) = qx_band_max {
                monitor_config.qx_band_max = v;
            }
            if let Some(v) = qy_band_min {
                monitor_config.qy_band_min = v;
            }
            if let Some(v) = qy_band_max {
                monitor_config.qy_band_max = v;
            }
            if let Some(v) = min_peak_confidence {
                monitor_config.min_peak_confidence = v;
            }
            if plot_time_axes_in_us {
                monitor_config.plot_time_axes_in_us = true;
            }

            monitor_config.validate()?;
            run_analyze_captured_spill(monitor_config, &bundle, &out_dir, flashes)?;
        }
        Command::AnalyzeCapturedSpills {
            config,
            bundles_dir,
            out_dir,
            count,
            align_tolerance_ms,
            min_aligned_fraction,
            injection_start_turn,
            injection_window_turns,
            sliding_window_turns,
            sliding_stride_turns,
            flashes,
            qx_band_min,
            qx_band_max,
            qy_band_min,
            qy_band_max,
            min_peak_confidence,
            plot_time_axes_in_us,
            min_confidence,
            min_aligned_bpm_count,
            min_per_plane_bpm,
            peak_edge_margin,
            record_format,
            detailed_artifacts,
            reference_file,
            reference_key,
            reference_match_tolerance_ms,
        } => {
            if flashes.is_some() && sliding_stride_turns.is_some() {
                eprintln!(
                    "[warn] analyze-captured-spills: --sliding-stride-turns is ignored when --flashes is set"
                );
            }
            if flashes.is_some() {
                eprintln!(
                    "[warn] analyze-captured-spills: injection_window_turns is ignored when --flashes is set; using sliding_window_turns"
                );
            }
            let mut monitor_config = load_monitor_config(&config)
                .with_context(|| format!("failed to load {}", config.display()))?;

            if let Some(v) = align_tolerance_ms {
                monitor_config.align_tolerance_ms = v;
            }
            if let Some(v) = min_aligned_fraction {
                monitor_config.min_aligned_fraction = v;
            }
            if let Some(v) = injection_start_turn {
                monitor_config.injection_start_turn = v;
            }
            if let Some(v) = injection_window_turns {
                monitor_config.injection_window_turns = v.max(1);
            }
            if let Some(v) = sliding_window_turns {
                monitor_config.sliding_window_turns = v.max(1);
            }
            if let Some(v) = sliding_stride_turns {
                monitor_config.sliding_stride_turns = v.max(1);
            }
            if let Some(v) = qx_band_min {
                monitor_config.qx_band_min = v;
            }
            if let Some(v) = qx_band_max {
                monitor_config.qx_band_max = v;
            }
            if let Some(v) = qy_band_min {
                monitor_config.qy_band_min = v;
            }
            if let Some(v) = qy_band_max {
                monitor_config.qy_band_max = v;
            }
            if let Some(v) = min_peak_confidence {
                monitor_config.min_peak_confidence = v;
            }
            if plot_time_axes_in_us {
                monitor_config.plot_time_axes_in_us = true;
            }

            monitor_config.validate()?;

            let options = BatchOptions {
                count: count.max(1),
                min_confidence,
                min_aligned_bpm_count: min_aligned_bpm_count.max(1),
                min_per_plane_bpm: min_per_plane_bpm.max(1),
                peak_edge_margin,
                record_format: BatchRecordFormat::parse(&record_format)?,
                detailed_artifacts: DetailedArtifactsMode::parse(&detailed_artifacts)?,
                reference_file,
                reference_key: ReferenceKey::parse(&reference_key)?,
                reference_match_tolerance_ms,
                flash_count: flashes,
            };

            run_analyze_captured_spills(monitor_config, &bundles_dir, &out_dir, options)?;
        }
        Command::AnalyzePhase {
            config,
            out_dir,
            window_start_min,
            window_start_max,
            window_start_step,
            window_length_min,
            window_length_max,
            window_length_step,
            reference_start,
            reference_length,
            svd_modes,
            svd_normalize_bpm,
            min_peak_confidence,
            plot_time_axes_in_us,
            summary_file,
            free_run,
            count,
            no_beam,
            stale_depth,
        } => {
            if !free_run && count.is_some() {
                bail!("--count requires --free-run for analyze-phase");
            }
            if matches!(count, Some(0)) {
                bail!("--count must be >= 1 for analyze-phase");
            }
            if !no_beam && stale_depth.is_some() {
                eprintln!("[warn] analyze-phase: --stale-depth is ignored unless --no-beam is set");
            }

            let mut monitor_config = load_monitor_config(&config)
                .with_context(|| format!("failed to load {}", config.display()))?;
            if let Some(v) = min_peak_confidence {
                monitor_config.min_peak_confidence = v;
            }
            if plot_time_axes_in_us {
                monitor_config.plot_time_axes_in_us = true;
            }
            monitor_config.validate()?;

            let options = StudyOptions {
                window_start_min,
                window_start_max,
                window_start_step,
                window_length_min,
                window_length_max,
                window_length_step,
                reference_start,
                reference_length,
                svd_modes,
                svd_normalize_bpm,
                summary_file,
            };

            let source_mode = if no_beam {
                SpillSourceMode::Historical {
                    stale_depth: stale_depth.unwrap_or(100).max(1),
                }
            } else {
                SpillSourceMode::LiveLatest
            };

            run_analyze_study(
                monitor_config,
                &out_dir,
                options,
                free_run,
                count,
                source_mode,
            )?;
        }
        Command::AnalyzeSpills {
            config,
            out_dir,
            count,
            align_tolerance_ms,
            min_aligned_fraction,
            injection_start_turn,
            injection_window_turns,
            sliding_window_turns,
            sliding_stride_turns,
            flashes,
            qx_band_min,
            qx_band_max,
            qy_band_min,
            qy_band_max,
            min_peak_confidence,
            plot_time_axes_in_us,
            min_confidence,
            min_aligned_bpm_count,
            min_per_plane_bpm,
            peak_edge_margin,
            record_format,
            detailed_artifacts,
            reference_file,
            reference_key,
            reference_match_tolerance_ms,
            no_beam,
            stale_depth,
        } => {
            if !no_beam && stale_depth.is_some() {
                eprintln!(
                    "[warn] analyze-spills: --stale-depth is ignored unless --no-beam is set"
                );
            }
            if flashes.is_some() && sliding_stride_turns.is_some() {
                eprintln!(
                    "[warn] analyze-spills: --sliding-stride-turns is ignored when --flashes is set"
                );
            }
            if flashes.is_some() {
                eprintln!(
                    "[warn] analyze-spills: injection_window_turns is ignored when --flashes is set; using sliding_window_turns"
                );
            }
            let mut monitor_config = load_monitor_config(&config)
                .with_context(|| format!("failed to load {}", config.display()))?;

            if let Some(v) = align_tolerance_ms {
                monitor_config.align_tolerance_ms = v;
            }
            if let Some(v) = min_aligned_fraction {
                monitor_config.min_aligned_fraction = v;
            }
            if let Some(v) = injection_start_turn {
                monitor_config.injection_start_turn = v;
            }
            if let Some(v) = injection_window_turns {
                monitor_config.injection_window_turns = v.max(1);
            }
            if let Some(v) = sliding_window_turns {
                monitor_config.sliding_window_turns = v.max(1);
            }
            if let Some(v) = sliding_stride_turns {
                monitor_config.sliding_stride_turns = v.max(1);
            }
            if let Some(v) = qx_band_min {
                monitor_config.qx_band_min = v;
            }
            if let Some(v) = qx_band_max {
                monitor_config.qx_band_max = v;
            }
            if let Some(v) = qy_band_min {
                monitor_config.qy_band_min = v;
            }
            if let Some(v) = qy_band_max {
                monitor_config.qy_band_max = v;
            }
            if let Some(v) = min_peak_confidence {
                monitor_config.min_peak_confidence = v;
            }
            if plot_time_axes_in_us {
                monitor_config.plot_time_axes_in_us = true;
            }

            monitor_config.validate()?;

            let options = BatchOptions {
                count: count.max(1),
                min_confidence,
                min_aligned_bpm_count: min_aligned_bpm_count.max(1),
                min_per_plane_bpm: min_per_plane_bpm.max(1),
                peak_edge_margin,
                record_format: BatchRecordFormat::parse(&record_format)?,
                detailed_artifacts: DetailedArtifactsMode::parse(&detailed_artifacts)?,
                reference_file,
                reference_key: ReferenceKey::parse(&reference_key)?,
                reference_match_tolerance_ms,
                flash_count: flashes,
            };

            let source_mode = if no_beam {
                SpillSourceMode::Historical {
                    stale_depth: stale_depth.unwrap_or(100).max(1),
                }
            } else {
                SpillSourceMode::LiveLatest
            };

            run_analyze_spills(monitor_config, &out_dir, options, source_mode)?;
        }
    }

    Ok(())
}

fn normalize_block_ms(v: u64) -> u64 {
    if v == 0 { 0 } else { v.max(50) }
}

fn apply_same_spill_override(
    config: &mut MonitorConfig,
    same_spill_tolerance_ms: Option<u64>,
    align_tolerance_ms: Option<u64>,
) {
    if let Some(v) = align_tolerance_ms {
        config.align_tolerance_ms = v;
        config.same_spill_tolerance_ms = v;
    }
    if let Some(v) = same_spill_tolerance_ms {
        config.same_spill_tolerance_ms = v;
    }
}

fn parse_flashes_arg(raw: &str) -> std::result::Result<usize, String> {
    if raw.eq_ignore_ascii_case("max") {
        return Ok(FLASH_COUNT_MAX);
    }
    let value = raw.parse::<usize>().map_err(|_| {
        format!("invalid --flashes value '{raw}', expected positive integer or 'max'")
    })?;
    if value == 0 {
        return Err("--flashes must be >= 1".to_string());
    }
    Ok(value)
}
