//! Spill analysis and study workflows.
//!
//! Core responsibilities:
//! - Build synchronized spill snapshots from multi-device TbT streams.
//! - Extract injection and sliding-window tune estimates (`Qx`, `Qy`) with confidence.
//! - Run robustness studies and batch-quality summaries.
//! - Emit artifacts (plots, CSV/JSONL, markdown summaries) for operations and review.
//!
//! Important timing policy:
//! - Candidate/target timestamps use small adjacent-bucket clustering (currently ±1 ms)
//!   to tolerate realistic stream-id jitter across devices.
//! - Quality classification and warnings preserve partial/incomplete snapshots instead of
//!   dropping them, because diagnostic visibility is operationally important.

use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Sender};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, anyhow, bail};
use redis::streams::{StreamReadOptions, StreamReadReply};
use redis::{Commands, Connection};
use serde_json::{Map, Value};

use crate::config::{DeviceConfig, MonitorConfig, RedisConfig};

const DEFAULT_XRANGE_COUNT: usize = 128;
const FREE_RUN_SETTLE_RETRIES: usize = 3;
const FREE_RUN_SETTLE_DELAY_MS: u64 = 40;
const ADJACENT_BUCKET_TOLERANCE_MS: u64 = 1;
const DEFAULT_METHOD_WEAK_CONFIDENCE: f64 = 1.5;
const MIN_PEAK_SEARCH_BIN: usize = 3;
pub const FLASH_COUNT_MAX: usize = usize::MAX;
const BATCH_SUMMARY_LIMITATIONS: &str = "Current method is spill-by-spill FFT peak-pick (pre-SVD). Tune depends on selected window/band. \
Sliding-window tune variation may reflect algorithm sensitivity, low SNR, or real machine behavior. \
No reference monitor cross-check is applied unless reference data is provided.";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
enum Plane {
    Horizontal,
    Vertical,
}

impl Plane {
    fn label(self) -> &'static str {
        match self {
            Self::Horizontal => "H",
            Self::Vertical => "V",
        }
    }

    fn tune_band(self, config: &MonitorConfig) -> (f64, f64) {
        match self {
            Self::Horizontal => (config.qx_band_min, config.qx_band_max),
            Self::Vertical => (config.qy_band_min, config.qy_band_max),
        }
    }

    fn track_half_width(self, config: &MonitorConfig) -> f64 {
        match self {
            Self::Horizontal => config.qx_track_half_width,
            Self::Vertical => config.qy_track_half_width,
        }
    }
}

#[derive(Debug, Clone)]
struct TbtObservation {
    bpm_ip: String,
    stream_key: String,
    id: String,
    ms: u64,
    aligned: bool,
}

#[derive(Debug, Clone)]
struct StreamTrace {
    plane: Plane,
    bpm_ip: String,
    stream_key: String,
    samples: Vec<f64>,
}

#[derive(Debug, Clone)]
struct PeakResult {
    tune: f64,
    confidence: f64,
    peak_power: f64,
    median_power: f64,
    prominence: f64,
}

#[derive(Debug, Clone)]
struct SlidingPoint {
    center_turn: usize,
    raw_global_tune: Option<f64>,
    tracked_local_tune: Option<f64>,
    selected_tune: Option<f64>,
    raw_global_confidence: Option<f64>,
    selected_confidence: Option<f64>,
    used_global_fallback: bool,
    suspicious_step: bool,
    step_delta: Option<f64>,
}

#[derive(Debug, Clone)]
struct PlaneAnalysis {
    plane: Plane,
    traces_total: usize,
    traces_used: usize,
    consensus_turns: usize,
    participating_bpms: Vec<String>,
    best_bpm_stream: Option<String>,
    max_rms_bpm: Option<f64>,
    injection_spectrum: Vec<f64>,
    injection_peak: Option<PeakResult>,
    sliding: Vec<SlidingPoint>,
    sliding_spectra: Vec<Vec<f64>>,
    sliding_fallback_count: usize,
    sliding_suspicious_count: usize,
}

#[derive(Debug, Clone)]
struct DeviceEvent {
    id: String,
    ms: u64,
}

#[derive(Debug, Clone)]
struct FreeRunSignal {
    bpm_ip: String,
    event: DeviceEvent,
}

#[derive(Debug, Clone)]
struct SpillSnapshot {
    target_ms: u64,
    observations: Vec<TbtObservation>,
    h_analysis: Option<PlaneAnalysis>,
    v_analysis: Option<PlaneAnalysis>,
    warnings: Vec<String>,
}

#[derive(Debug, Clone)]
struct SpillOutputPaths {
    spectrum_h: PathBuf,
    spectrum_v: PathBuf,
    spectrogram_h: PathBuf,
    spectrogram_v: PathBuf,
    tune_vs_time: PathBuf,
    tune_validation: PathBuf,
    sliding_tune_csv: PathBuf,
}

#[derive(Debug, Clone)]
pub struct StudyOptions {
    pub window_start_min: usize,
    pub window_start_max: usize,
    pub window_start_step: usize,
    pub window_length_min: usize,
    pub window_length_max: usize,
    pub window_length_step: usize,
    pub reference_start: Option<usize>,
    pub reference_length: Option<usize>,
    pub svd_modes: usize,
    pub svd_normalize_bpm: bool,
    pub summary_file: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BatchRecordFormat {
    Csv,
    Jsonl,
    Both,
}

impl BatchRecordFormat {
    pub fn parse(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "csv" => Ok(Self::Csv),
            "jsonl" => Ok(Self::Jsonl),
            "both" => Ok(Self::Both),
            other => bail!("invalid record_format '{other}', expected csv|jsonl|both"),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DetailedArtifactsMode {
    All,
    Representative,
    None,
}

impl DetailedArtifactsMode {
    pub fn parse(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "all" => Ok(Self::All),
            "representative" => Ok(Self::Representative),
            "none" => Ok(Self::None),
            other => {
                bail!("invalid detailed_artifacts '{other}', expected all|representative|none")
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SpillSourceMode {
    LiveLatest,
    Historical { stale_depth: usize },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReferenceKey {
    TargetMs,
    SpillIndex,
}

impl ReferenceKey {
    pub fn parse(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "target_ms" => Ok(Self::TargetMs),
            "spill_index" => Ok(Self::SpillIndex),
            other => bail!("invalid reference_key '{other}', expected target_ms|spill_index"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct BatchOptions {
    pub count: usize,
    pub min_confidence: f64,
    pub min_aligned_bpm_count: usize,
    pub min_per_plane_bpm: usize,
    pub peak_edge_margin: f64,
    pub record_format: BatchRecordFormat,
    pub detailed_artifacts: DetailedArtifactsMode,
    pub reference_file: Option<PathBuf>,
    pub reference_key: ReferenceKey,
    pub reference_match_tolerance_ms: u64,
    pub flash_count: Option<usize>,
}

#[derive(Debug, Clone)]
struct StudyOutputPaths {
    tune_vs_window_start: PathBuf,
    tune_vs_window_length: PathBuf,
    bpm_quality_table: PathBuf,
    tune_by_bpm: PathBuf,
    confidence_by_bpm: PathBuf,
    method_comparison: PathBuf,
    findings_summary: PathBuf,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SpillQuality {
    Good,
    Marginal,
    Bad,
}

impl SpillQuality {
    fn label(self) -> &'static str {
        match self {
            Self::Good => "GOOD",
            Self::Marginal => "MARGINAL",
            Self::Bad => "BAD",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SpillStatus {
    Ok,
    Partial,
    Failed,
}

impl SpillStatus {
    fn label(self) -> &'static str {
        match self {
            Self::Ok => "OK",
            Self::Partial => "PARTIAL",
            Self::Failed => "FAILED",
        }
    }
}

#[derive(Debug, Clone)]
struct SlidingSummary {
    median: Option<f64>,
    stddev: Option<f64>,
    min: Option<f64>,
    max: Option<f64>,
}

#[derive(Debug, Clone)]
struct SlidingDiagnostics {
    fallback_count: usize,
    suspicious_count: usize,
    missing_seed_count: usize,
    total_windows: usize,
}

#[derive(Debug, Clone)]
struct SpillRecord {
    spill_index: usize,
    attempt_index: usize,
    spill_uid: u64,
    captured_at_utc: String,
    target_ms: u64,
    trigger_ms: u64,
    trigger_source: String,
    aligned_fraction: f64,
    aligned_streams: usize,
    requested_streams: usize,
    used_streams_total: usize,
    used_streams_h: usize,
    used_streams_v: usize,
    consensus_turns_h: Option<usize>,
    consensus_turns_v: Option<usize>,
    consensus_turns_global: Option<usize>,
    injection_start_turn: usize,
    injection_window_turns: usize,
    sliding_window_turns: usize,
    sliding_stride_turns: usize,
    flash_count: Option<usize>,
    qx_band_min: f64,
    qx_band_max: f64,
    qy_band_min: f64,
    qy_band_max: f64,
    qx_injection: Option<f64>,
    qy_injection: Option<f64>,
    confidence_h: Option<f64>,
    confidence_v: Option<f64>,
    median_qx: Option<f64>,
    median_qy: Option<f64>,
    std_qx: Option<f64>,
    std_qy: Option<f64>,
    min_qx: Option<f64>,
    max_qx: Option<f64>,
    min_qy: Option<f64>,
    max_qy: Option<f64>,
    median_qx_raw: Option<f64>,
    std_qx_raw: Option<f64>,
    min_qx_raw: Option<f64>,
    max_qx_raw: Option<f64>,
    median_qy_raw: Option<f64>,
    std_qy_raw: Option<f64>,
    min_qy_raw: Option<f64>,
    max_qy_raw: Option<f64>,
    median_qx_tracked: Option<f64>,
    std_qx_tracked: Option<f64>,
    min_qx_tracked: Option<f64>,
    max_qx_tracked: Option<f64>,
    median_qy_tracked: Option<f64>,
    std_qy_tracked: Option<f64>,
    min_qy_tracked: Option<f64>,
    max_qy_tracked: Option<f64>,
    sliding_fallback_count_h: usize,
    sliding_fallback_count_v: usize,
    sliding_suspicious_count_h: usize,
    sliding_suspicious_count_v: usize,
    max_rms_bpm_h: Option<f64>,
    max_rms_bpm_v: Option<f64>,
    quality_label: SpillQuality,
    status: SpillStatus,
    quality_flags: Vec<String>,
    warnings: Vec<String>,
    participating_bpms_h: Vec<String>,
    participating_bpms_v: Vec<String>,
    best_bpm_stream_h: Option<String>,
    best_bpm_stream_v: Option<String>,
    ref_qx: Option<f64>,
    ref_qy: Option<f64>,
    residual_qx: Option<f64>,
    residual_qy: Option<f64>,
}

#[derive(Debug, Clone)]
struct BatchSpillResult {
    record: SpillRecord,
    snapshot: SpillSnapshot,
}

#[derive(Debug, Clone)]
struct BatchReference {
    target_ms: Option<u64>,
    spill_index: Option<usize>,
    qx: Option<f64>,
    qy: Option<f64>,
}

#[derive(Debug, Clone)]
struct BatchRunCounters {
    unresolved_wakes: usize,
    duplicate_wakes: usize,
    stale_depth_scanned: Option<usize>,
    historical_candidates_discovered: usize,
    historical_candidates_attempted: usize,
    historical_candidates_skipped: usize,
}

#[derive(Debug, Clone)]
struct HistoricalCandidate {
    target_ms: u64,
    stream_coverage: usize,
    observation_count: usize,
}

#[derive(Debug, Clone)]
struct CapturedManifestStream {
    bpm_ip: String,
    stream_key: String,
    stream_id: String,
    stream_ms: u64,
    payload_file: Option<String>,
    payload_bytes: Option<usize>,
    sample_count: Option<usize>,
    checksum_fnv1a64: Option<String>,
}

#[derive(Debug, Clone)]
struct CapturedManifest {
    schema_version: u64,
    artifact_type: String,
    target_ms: u64,
    redis_timestamp_ms: Option<u64>,
    align_tolerance_ms: Option<u64>,
    same_spill_tolerance_ms: Option<u64>,
    requested_streams: Option<usize>,
    streams: Vec<CapturedManifestStream>,
    warnings: Vec<String>,
}

#[derive(Debug, Clone)]
struct CapturedBundleCandidate {
    bundle_dir: PathBuf,
    manifest_path: PathBuf,
    target_ms: u64,
}

#[derive(Debug, Clone, Copy)]
struct TimelinessStats {
    min_delta_ms: i64,
    max_delta_ms: i64,
    median_abs_delta_ms: f64,
    max_abs_delta_ms: u64,
}

#[derive(Debug, Clone)]
struct SweepPoint {
    x: usize,
    tune: Option<f64>,
    confidence: Option<f64>,
}

#[derive(Debug, Clone)]
struct BpmMetric {
    plane: Plane,
    bpm_ip: String,
    stream_key: String,
    rms: f64,
    tune: Option<f64>,
    confidence: Option<f64>,
    peak_power: Option<f64>,
    prominence: Option<f64>,
    noise_floor: Option<f64>,
    score: f64,
    flags: Vec<String>,
}

#[derive(Debug, Clone)]
struct MethodResult {
    plane: Plane,
    method: &'static str,
    tune: Option<f64>,
    confidence: Option<f64>,
    start_tune_std: Option<f64>,
    length_tune_std: Option<f64>,
}

pub fn run_analyze_spill(
    config: MonitorConfig,
    out_dir: &Path,
    free_run: bool,
    free_run_count: Option<usize>,
    source_mode: SpillSourceMode,
    flash_count: Option<usize>,
) -> Result<()> {
    config.validate()?;
    if !free_run && free_run_count.is_some() {
        bail!("analyze-spill: --count is only supported with --free-run");
    }
    if matches!(flash_count, Some(0)) {
        bail!("analyze-spill: --flashes must be >= 1 when provided");
    }
    fs::create_dir_all(out_dir)
        .with_context(|| format!("failed to create output directory {}", out_dir.display()))?;

    if let SpillSourceMode::Historical { stale_depth } = source_mode {
        return run_analyze_spill_historical(
            config,
            out_dir,
            free_run,
            free_run_count,
            stale_depth.max(1),
            flash_count,
        );
    }

    if free_run {
        return run_analyze_spill_free_run(config, out_dir, free_run_count, flash_count);
    }

    run_analyze_spill_once(config, out_dir, flash_count)
}

pub fn run_analyze_captured_spill(
    config: MonitorConfig,
    bundle_path: &Path,
    out_dir: &Path,
    flash_count: Option<usize>,
) -> Result<()> {
    config.validate()?;
    if matches!(flash_count, Some(0)) {
        bail!("analyze-captured-spill: --flashes must be >= 1 when provided");
    }
    fs::create_dir_all(out_dir)
        .with_context(|| format!("failed to create output directory {}", out_dir.display()))?;

    let snapshot = analyze_captured_spill_snapshot(&config, bundle_path, flash_count)?;
    let paths = write_spill_outputs(out_dir, None, &config, &snapshot, flash_count)?;

    let _ = print_summary(
        &config,
        &snapshot,
        &paths,
        "analyze-captured-spill summary",
        true,
    );

    Ok(())
}

pub fn run_analyze_captured_spills(
    config: MonitorConfig,
    bundles_dir: &Path,
    out_dir: &Path,
    options: BatchOptions,
) -> Result<()> {
    validate_batch_options(&options)?;
    config.validate()?;
    fs::create_dir_all(out_dir)
        .with_context(|| format!("failed to create output directory {}", out_dir.display()))?;

    let references = if let Some(path) = options.reference_file.as_ref() {
        load_reference_file(path)?
    } else {
        Vec::new()
    };

    let (candidates, skipped_discovery) = discover_captured_bundle_candidates(bundles_dir)?;
    if candidates.is_empty() {
        bail!(
            "no captured-spill bundles were discovered under {}",
            bundles_dir.display()
        );
    }

    println!(
        "analyze-captured-spills: target_count={} discovered_bundles={} source={}",
        options.count,
        candidates.len(),
        bundles_dir.display()
    );

    let mut counters = BatchRunCounters {
        unresolved_wakes: skipped_discovery,
        duplicate_wakes: 0,
        stale_depth_scanned: None,
        historical_candidates_discovered: 0,
        historical_candidates_attempted: 0,
        historical_candidates_skipped: 0,
    };
    let mut seen_target_ms = HashSet::<u64>::new();
    let mut results = Vec::<BatchSpillResult>::new();
    let dedupe_tolerance_ms = target_bucket_tolerance_ms(&config);

    for (attempt_idx, candidate) in candidates.iter().enumerate() {
        if results.len() >= options.count {
            break;
        }

        let attempt_index = attempt_idx + 1;
        if target_seen_within_tolerance(&seen_target_ms, candidate.target_ms, dedupe_tolerance_ms) {
            counters.duplicate_wakes += 1;
            continue;
        }

        let snapshot = match analyze_captured_spill_snapshot(
            &config,
            &candidate.bundle_dir,
            options.flash_count,
        ) {
            Ok(snapshot) => snapshot,
            Err(err) => {
                counters.unresolved_wakes += 1;
                eprintln!(
                    "[analyze-captured-spills] skipped {}: {}",
                    candidate.manifest_path.display(),
                    err
                );
                continue;
            }
        };
        seen_target_ms.insert(snapshot.target_ms);

        let spill_index = results.len() + 1;
        let mut record = build_spill_record(
            &config,
            &options,
            &snapshot,
            spill_index,
            attempt_index,
            snapshot.target_ms,
            "captured-spill".to_string(),
        )?;
        apply_reference_match(
            &mut record,
            &references,
            options.reference_key,
            options.reference_match_tolerance_ms,
        );

        let sliding_csv = out_dir.join(format!(
            "spill_{}_{}_sliding_tune.csv",
            spill_index, snapshot.target_ms
        ));
        write_spill_sliding_csv(
            &sliding_csv,
            snapshot
                .h_analysis
                .as_ref()
                .map(|analysis| analysis.sliding.as_slice())
                .unwrap_or(&[]),
            snapshot
                .v_analysis
                .as_ref()
                .map(|analysis| analysis.sliding.as_slice())
                .unwrap_or(&[]),
        )?;

        let timeliness = timeliness_stats(&snapshot.observations, snapshot.target_ms);
        println!(
            "[analyze-captured-spills] spill {}/{} target={} quality={} qx={} qy={} conf_h={} conf_v={} timeliness_med_abs_ms={} timeliness_max_abs_ms={}",
            spill_index,
            options.count,
            record.target_ms,
            record.quality_label.label(),
            opt_fmt(record.qx_injection),
            opt_fmt(record.qy_injection),
            opt_fmt(record.confidence_h),
            opt_fmt(record.confidence_v),
            opt_fmt(timeliness.map(|stats| stats.median_abs_delta_ms)),
            timeliness
                .map(|stats| stats.max_abs_delta_ms.to_string())
                .unwrap_or_else(|| "NA".to_string()),
        );

        results.push(BatchSpillResult { record, snapshot });
    }

    if results.is_empty() {
        bail!(
            "no captured-spill bundles produced usable batch analyses under {}",
            bundles_dir.display()
        );
    }

    if results.len() < options.count {
        println!(
            "analyze-captured-spills: exhausted captured bundles with {}/{} successful analyses",
            results.len(),
            options.count
        );
    }

    let has_reference_file = !references.is_empty();
    let has_reference_match = results
        .iter()
        .any(|r| r.record.residual_qx.is_some() || r.record.residual_qy.is_some());

    write_batch_records(out_dir, &results, options.record_format)?;
    write_batch_summary_plots(out_dir, &config, &results, &options, has_reference_match)?;
    write_composite_waterfall_plots(out_dir, &config, &results)?;
    write_batch_detailed_artifacts(
        out_dir,
        &config,
        &results,
        options.detailed_artifacts,
        options.flash_count,
    )?;

    let aggregate = summarize_batch(&results, &counters);
    print_batch_console_summary(&aggregate);
    write_batch_summary_markdown(out_dir, &aggregate, &options, has_reference_file)?;

    Ok(())
}

#[derive(Debug, Clone)]
struct PreparedPlaneData {
    plane: Plane,
    traces_total: usize,
    traces_used: usize,
    consensus_turns: usize,
    traces: Vec<StreamTrace>,
}

pub fn run_analyze_study(
    config: MonitorConfig,
    out_dir: &Path,
    options: StudyOptions,
    free_run: bool,
    free_run_count: Option<usize>,
    source_mode: SpillSourceMode,
) -> Result<()> {
    validate_study_options(&options)?;
    config.validate()?;
    if !free_run && free_run_count.is_some() {
        bail!("analyze-phase: --count is only supported with --free-run");
    }
    fs::create_dir_all(out_dir)
        .with_context(|| format!("failed to create output directory {}", out_dir.display()))?;

    if let SpillSourceMode::Historical { stale_depth } = source_mode {
        return run_analyze_study_historical(
            config,
            out_dir,
            options,
            free_run,
            free_run_count,
            stale_depth.max(1),
        );
    }

    if free_run {
        return run_analyze_study_free_run(config, out_dir, options, free_run_count);
    }

    let snapshot = analyze_spill_snapshot(&config, None)?;
    let _ = run_analyze_study_for_snapshot(
        &config,
        out_dir,
        &options,
        snapshot,
        None,
        "analyze-phase summary",
    )?;
    Ok(())
}

pub fn run_analyze_spills(
    config: MonitorConfig,
    out_dir: &Path,
    options: BatchOptions,
    source_mode: SpillSourceMode,
) -> Result<()> {
    validate_batch_options(&options)?;
    config.validate()?;
    fs::create_dir_all(out_dir)
        .with_context(|| format!("failed to create output directory {}", out_dir.display()))?;

    let references = if let Some(path) = options.reference_file.as_ref() {
        load_reference_file(path)?
    } else {
        Vec::new()
    };

    if let SpillSourceMode::Historical { stale_depth } = source_mode {
        return run_analyze_spills_historical(config, out_dir, options, references, stale_depth);
    }

    println!(
        "analyze-spills: collecting {} successful spills using existing wake/snapshot pipeline",
        options.count
    );
    println!("press Ctrl-C to stop");

    let (tx, rx) = mpsc::channel::<FreeRunSignal>();
    for device in config.devices.clone() {
        let tx_worker = tx.clone();
        let reconnect_initial_ms = config.reconnect_initial_ms;
        let reconnect_max_ms = config.reconnect_max_ms;
        thread::spawn(move || {
            if let Err(err) =
                run_free_run_watch_worker(device, reconnect_initial_ms, reconnect_max_ms, tx_worker)
            {
                eprintln!("free-run watch worker exited: {err}");
            }
        });
    }
    drop(tx);

    let mut counters = BatchRunCounters {
        unresolved_wakes: 0,
        duplicate_wakes: 0,
        stale_depth_scanned: None,
        historical_candidates_discovered: 0,
        historical_candidates_attempted: 0,
        historical_candidates_skipped: 0,
    };
    let mut attempt_index = 0usize;
    let mut seen_target_ms = HashSet::<u64>::new();
    let mut results = Vec::<BatchSpillResult>::new();
    let dedupe_tolerance_ms = target_bucket_tolerance_ms(&config);

    while results.len() < options.count {
        let signal = match rx.recv() {
            Ok(signal) => signal,
            Err(err) => bail!("batch event channel closed: {err}"),
        };
        attempt_index += 1;

        let snapshot = match analyze_spill_snapshot(&config, options.flash_count) {
            Ok(snapshot) => snapshot,
            Err(err) => {
                counters.unresolved_wakes += 1;
                eprintln!(
                    "[analyze-spills] unresolved wake from {} {} (ms {}): {}",
                    signal.bpm_ip, signal.event.id, signal.event.ms, err
                );
                continue;
            }
        };

        if target_seen_within_tolerance(&seen_target_ms, snapshot.target_ms, dedupe_tolerance_ms) {
            counters.duplicate_wakes += 1;
            continue;
        }
        seen_target_ms.insert(snapshot.target_ms);

        let spill_index = results.len() + 1;
        let mut trigger_warnings = Vec::new();
        let (trigger_ms, trigger_source) =
            resolve_trigger_timestamp(&config, snapshot.target_ms, &mut trigger_warnings)?;

        let mut record = build_spill_record(
            &config,
            &options,
            &snapshot,
            spill_index,
            attempt_index,
            trigger_ms,
            trigger_source.to_string(),
        )?;
        record.warnings.extend(trigger_warnings);
        apply_reference_match(
            &mut record,
            &references,
            options.reference_key,
            options.reference_match_tolerance_ms,
        );

        let sliding_csv = out_dir.join(format!(
            "spill_{}_{}_sliding_tune.csv",
            spill_index, snapshot.target_ms
        ));
        write_spill_sliding_csv(
            &sliding_csv,
            snapshot
                .h_analysis
                .as_ref()
                .map(|analysis| analysis.sliding.as_slice())
                .unwrap_or(&[]),
            snapshot
                .v_analysis
                .as_ref()
                .map(|analysis| analysis.sliding.as_slice())
                .unwrap_or(&[]),
        )?;

        let timeliness = timeliness_stats(&snapshot.observations, snapshot.target_ms);
        println!(
            "[analyze-spills] spill {}/{} target={} quality={} qx={} qy={} conf_h={} conf_v={} timeliness_med_abs_ms={} timeliness_max_abs_ms={}",
            spill_index,
            options.count,
            record.target_ms,
            record.quality_label.label(),
            opt_fmt(record.qx_injection),
            opt_fmt(record.qy_injection),
            opt_fmt(record.confidence_h),
            opt_fmt(record.confidence_v),
            opt_fmt(timeliness.map(|stats| stats.median_abs_delta_ms)),
            timeliness
                .map(|stats| stats.max_abs_delta_ms.to_string())
                .unwrap_or_else(|| "NA".to_string()),
        );

        results.push(BatchSpillResult { record, snapshot });
    }

    let has_reference_file = !references.is_empty();
    let has_reference_match = results
        .iter()
        .any(|r| r.record.residual_qx.is_some() || r.record.residual_qy.is_some());

    write_batch_records(out_dir, &results, options.record_format)?;
    write_batch_summary_plots(out_dir, &config, &results, &options, has_reference_match)?;
    write_composite_waterfall_plots(out_dir, &config, &results)?;
    write_batch_detailed_artifacts(
        out_dir,
        &config,
        &results,
        options.detailed_artifacts,
        options.flash_count,
    )?;

    let aggregate = summarize_batch(&results, &counters);
    print_batch_console_summary(&aggregate);
    write_batch_summary_markdown(out_dir, &aggregate, &options, has_reference_file)?;

    Ok(())
}

fn run_analyze_spills_historical(
    config: MonitorConfig,
    out_dir: &Path,
    options: BatchOptions,
    references: Vec<BatchReference>,
    stale_depth: usize,
) -> Result<()> {
    let candidates = discover_historical_candidates(&config, stale_depth)?;
    if candidates.is_empty() {
        bail!(
            "no historical TBT stream entries were discovered for analyze-spills (stale_depth={})",
            stale_depth
        );
    }

    println!(
        "analyze-spills no-beam: target_count={} stale_depth={} candidates={}",
        options.count,
        stale_depth,
        candidates.len()
    );

    let mut counters = BatchRunCounters {
        unresolved_wakes: 0,
        duplicate_wakes: 0,
        stale_depth_scanned: Some(stale_depth),
        historical_candidates_discovered: candidates.len(),
        historical_candidates_attempted: 0,
        historical_candidates_skipped: 0,
    };
    let mut seen_target_ms = HashSet::<u64>::new();
    let mut results = Vec::<BatchSpillResult>::new();
    let dedupe_tolerance_ms = target_bucket_tolerance_ms(&config);

    for candidate in &candidates {
        if results.len() >= options.count {
            break;
        }

        counters.historical_candidates_attempted += 1;
        if target_seen_within_tolerance(&seen_target_ms, candidate.target_ms, dedupe_tolerance_ms) {
            counters.duplicate_wakes += 1;
            continue;
        }
        seen_target_ms.insert(candidate.target_ms);

        let snapshot = match analyze_spill_snapshot_at_target(
            &config,
            candidate.target_ms,
            options.flash_count,
        ) {
            Ok(snapshot) => snapshot,
            Err(err) => {
                counters.unresolved_wakes += 1;
                counters.historical_candidates_skipped += 1;
                eprintln!(
                    "[analyze-spills no-beam] skipped {} (coverage={} obs={}): {}",
                    candidate.target_ms,
                    candidate.stream_coverage,
                    candidate.observation_count,
                    err
                );
                continue;
            }
        };

        let spill_index = results.len() + 1;
        let mut trigger_warnings = Vec::new();
        let (trigger_ms, trigger_source) =
            resolve_trigger_timestamp(&config, snapshot.target_ms, &mut trigger_warnings)?;

        let mut record = build_spill_record(
            &config,
            &options,
            &snapshot,
            spill_index,
            counters.historical_candidates_attempted,
            trigger_ms,
            trigger_source.to_string(),
        )?;
        record.warnings.extend(trigger_warnings);
        apply_reference_match(
            &mut record,
            &references,
            options.reference_key,
            options.reference_match_tolerance_ms,
        );

        let sliding_csv = out_dir.join(format!(
            "spill_{}_{}_sliding_tune.csv",
            spill_index, snapshot.target_ms
        ));
        write_spill_sliding_csv(
            &sliding_csv,
            snapshot
                .h_analysis
                .as_ref()
                .map(|analysis| analysis.sliding.as_slice())
                .unwrap_or(&[]),
            snapshot
                .v_analysis
                .as_ref()
                .map(|analysis| analysis.sliding.as_slice())
                .unwrap_or(&[]),
        )?;

        let timeliness = timeliness_stats(&snapshot.observations, snapshot.target_ms);
        println!(
            "[analyze-spills no-beam] spill {}/{} target={} coverage={} qx={} qy={} conf_h={} conf_v={} timeliness_med_abs_ms={} timeliness_max_abs_ms={}",
            spill_index,
            options.count,
            record.target_ms,
            candidate.stream_coverage,
            opt_fmt(record.qx_injection),
            opt_fmt(record.qy_injection),
            opt_fmt(record.confidence_h),
            opt_fmt(record.confidence_v),
            opt_fmt(timeliness.map(|stats| stats.median_abs_delta_ms)),
            timeliness
                .map(|stats| stats.max_abs_delta_ms.to_string())
                .unwrap_or_else(|| "NA".to_string()),
        );

        results.push(BatchSpillResult { record, snapshot });
    }

    if results.is_empty() {
        bail!(
            "no historical spill candidates produced usable batch analyses (attempted {}, stale_depth={})",
            counters.historical_candidates_attempted,
            stale_depth
        );
    }

    if results.len() < options.count {
        println!(
            "analyze-spills no-beam: exhausted historical candidates with {}/{} successful analyses",
            results.len(),
            options.count
        );
    }

    let has_reference_file = !references.is_empty();
    let has_reference_match = results
        .iter()
        .any(|r| r.record.residual_qx.is_some() || r.record.residual_qy.is_some());

    write_batch_records(out_dir, &results, options.record_format)?;
    write_batch_summary_plots(out_dir, &config, &results, &options, has_reference_match)?;
    write_composite_waterfall_plots(out_dir, &config, &results)?;
    write_batch_detailed_artifacts(
        out_dir,
        &config,
        &results,
        options.detailed_artifacts,
        options.flash_count,
    )?;

    let aggregate = summarize_batch(&results, &counters);
    print_batch_console_summary(&aggregate);
    write_batch_summary_markdown(out_dir, &aggregate, &options, has_reference_file)?;

    Ok(())
}

fn default_free_run_batch_options(count: usize, flash_count: Option<usize>) -> BatchOptions {
    BatchOptions {
        count: count.max(1),
        min_confidence: 1.5,
        min_aligned_bpm_count: 4,
        min_per_plane_bpm: 1,
        peak_edge_margin: 0.005,
        record_format: BatchRecordFormat::Both,
        detailed_artifacts: DetailedArtifactsMode::None,
        reference_file: None,
        reference_key: ReferenceKey::TargetMs,
        reference_match_tolerance_ms: 1,
        flash_count,
    }
}

fn synthesize_batch_outputs_from_captured_spills(
    out_dir: &Path,
    config: &MonitorConfig,
    captured: Vec<(usize, SpillSnapshot)>,
    counters: BatchRunCounters,
    context_label: &str,
    flash_count: Option<usize>,
) -> Result<()> {
    if captured.is_empty() {
        return Ok(());
    }

    let options = default_free_run_batch_options(captured.len(), flash_count);
    let mut results = Vec::<BatchSpillResult>::with_capacity(captured.len());

    for (spill_idx, (attempt_index, snapshot)) in captured.into_iter().enumerate() {
        let spill_index = spill_idx + 1;

        let mut trigger_warnings = Vec::new();
        let (trigger_ms, trigger_source) =
            resolve_trigger_timestamp(config, snapshot.target_ms, &mut trigger_warnings)?;

        let mut record = build_spill_record(
            config,
            &options,
            &snapshot,
            spill_index,
            attempt_index,
            trigger_ms,
            trigger_source.to_string(),
        )?;
        record.warnings.extend(trigger_warnings);

        let sliding_csv = out_dir.join(format!(
            "spill_{}_{}_sliding_tune.csv",
            spill_index, snapshot.target_ms
        ));
        write_spill_sliding_csv(
            &sliding_csv,
            snapshot
                .h_analysis
                .as_ref()
                .map(|analysis| analysis.sliding.as_slice())
                .unwrap_or(&[]),
            snapshot
                .v_analysis
                .as_ref()
                .map(|analysis| analysis.sliding.as_slice())
                .unwrap_or(&[]),
        )?;

        results.push(BatchSpillResult { record, snapshot });
    }

    println!(
        "[{}] synthesizing batch outputs for {} captured spills",
        context_label,
        results.len()
    );

    write_batch_records(out_dir, &results, options.record_format)?;
    write_batch_summary_plots(out_dir, config, &results, &options, false)?;
    write_composite_waterfall_plots(out_dir, config, &results)?;

    let aggregate = summarize_batch(&results, &counters);
    print_batch_console_summary(&aggregate);
    write_batch_summary_markdown(out_dir, &aggregate, &options, false)?;

    Ok(())
}

fn run_analyze_study_for_snapshot(
    config: &MonitorConfig,
    out_dir: &Path,
    options: &StudyOptions,
    snapshot: SpillSnapshot,
    stem: Option<&str>,
    title: &str,
) -> Result<Vec<String>> {
    let traces = collect_stream_traces(
        config,
        snapshot.target_ms,
        config.align_tolerance_ms,
        &mut Vec::new(),
    )?;
    if traces.is_empty() {
        bail!(
            "no stream traces available for study at target_ms {}",
            snapshot.target_ms
        );
    }

    let horizontal = traces
        .iter()
        .filter(|trace| trace.plane == Plane::Horizontal)
        .cloned()
        .collect::<Vec<_>>();
    let vertical = traces
        .iter()
        .filter(|trace| trace.plane == Plane::Vertical)
        .cloned()
        .collect::<Vec<_>>();

    let base_reference_start = options
        .reference_start
        .unwrap_or(config.injection_start_turn);
    let base_reference_length = options
        .reference_length
        .unwrap_or(config.injection_window_turns)
        .max(64);

    let mut warnings = snapshot.warnings.clone();
    let h_plane = prepare_plane_data(
        Plane::Horizontal,
        horizontal,
        base_reference_start
            .saturating_add(base_reference_length)
            .max(
                options
                    .window_start_max
                    .saturating_add(options.window_length_max),
            ),
        &mut warnings,
    );
    let v_plane = prepare_plane_data(
        Plane::Vertical,
        vertical,
        base_reference_start
            .saturating_add(base_reference_length)
            .max(
                options
                    .window_start_max
                    .saturating_add(options.window_length_max),
            ),
        &mut warnings,
    );

    warnings.push(format!(
        "SVD phase deferred for this run (svd_modes={}, svd_normalize_bpm={})",
        options.svd_modes, options.svd_normalize_bpm
    ));
    if let Some(plane) = h_plane.as_ref() {
        warnings.push(format!(
            "H plane study traces: used {}/{} consensus_n={}",
            plane.traces_used, plane.traces_total, plane.consensus_turns
        ));
    }
    if let Some(plane) = v_plane.as_ref() {
        warnings.push(format!(
            "V plane study traces: used {}/{} consensus_n={}",
            plane.traces_used, plane.traces_total, plane.consensus_turns
        ));
    }

    if h_plane.is_none() && v_plane.is_none() {
        bail!("neither H nor V plane had usable data for analysis study");
    }

    let global_consensus = [h_plane.as_ref(), v_plane.as_ref()]
        .into_iter()
        .flatten()
        .map(|p| p.consensus_turns)
        .min()
        .ok_or_else(|| anyhow!("failed to determine a global consensus turn length"))?;

    let reference_start = base_reference_start.min(global_consensus.saturating_sub(64));
    let reference_length =
        base_reference_length.min(global_consensus.saturating_sub(reference_start));
    if reference_length < 64 {
        bail!("reference_length resolved below 64 turns; insufficient data");
    }

    let start_values = build_sweep_values(
        options.window_start_min,
        options
            .window_start_max
            .min(global_consensus.saturating_sub(reference_length)),
        options.window_start_step,
    );
    let length_values = build_sweep_values(
        options.window_length_min.max(64),
        options
            .window_length_max
            .min(global_consensus.saturating_sub(reference_start)),
        options.window_length_step,
    );

    if start_values.is_empty() || length_values.is_empty() {
        bail!("window sweep values are empty after range/consensus constraints");
    }

    let h_start_sweep = h_plane
        .as_ref()
        .map(|plane| compute_window_start_sweep(plane, config, &start_values, reference_length))
        .transpose()?
        .unwrap_or_default();
    let v_start_sweep = v_plane
        .as_ref()
        .map(|plane| compute_window_start_sweep(plane, config, &start_values, reference_length))
        .transpose()?
        .unwrap_or_default();

    let h_length_sweep = h_plane
        .as_ref()
        .map(|plane| compute_window_length_sweep(plane, config, reference_start, &length_values))
        .transpose()?
        .unwrap_or_default();
    let v_length_sweep = v_plane
        .as_ref()
        .map(|plane| compute_window_length_sweep(plane, config, reference_start, &length_values))
        .transpose()?
        .unwrap_or_default();

    let output_paths = study_output_paths(out_dir, options, stem);
    ensure_study_output_dirs(&output_paths)?;

    write_window_sensitivity_png(
        &output_paths.tune_vs_window_start,
        "WINDOW START",
        &h_start_sweep,
        &v_start_sweep,
        config.tune_plot_y_min,
        config.tune_plot_y_max,
    )?;
    write_window_sensitivity_png(
        &output_paths.tune_vs_window_length,
        "WINDOW LENGTH",
        &h_length_sweep,
        &v_length_sweep,
        config.tune_plot_y_min,
        config.tune_plot_y_max,
    )?;

    let mut bpm_metrics = Vec::<BpmMetric>::new();
    if let Some(plane) = h_plane.as_ref() {
        bpm_metrics.extend(compute_bpm_metrics(
            plane,
            config,
            reference_start,
            reference_length,
        )?);
    }
    if let Some(plane) = v_plane.as_ref() {
        bpm_metrics.extend(compute_bpm_metrics(
            plane,
            config,
            reference_start,
            reference_length,
        )?);
    }

    write_bpm_quality_csv(&output_paths.bpm_quality_table, &bpm_metrics)?;
    write_bpm_tune_confidence_plots(
        &output_paths.tune_by_bpm,
        &output_paths.confidence_by_bpm,
        &bpm_metrics,
        config.tune_plot_y_min,
        config.tune_plot_y_max,
    )?;

    let mut method_results = Vec::<MethodResult>::new();
    if let Some(plane) = h_plane.as_ref() {
        method_results.extend(compute_method_results(
            plane,
            config,
            &bpm_metrics,
            reference_start,
            reference_length,
            &start_values,
            &length_values,
        )?);
    }
    if let Some(plane) = v_plane.as_ref() {
        method_results.extend(compute_method_results(
            plane,
            config,
            &bpm_metrics,
            reference_start,
            reference_length,
            &start_values,
            &length_values,
        )?);
    }
    write_method_comparison_png(
        &output_paths.method_comparison,
        &method_results,
        config.tune_plot_y_min,
        config.tune_plot_y_max,
    )?;

    write_findings_summary(
        &output_paths.findings_summary,
        snapshot.target_ms,
        config,
        &warnings,
        &h_start_sweep,
        &v_start_sweep,
        &h_length_sweep,
        &v_length_sweep,
        &bpm_metrics,
        &method_results,
    )?;

    Ok(print_study_summary(
        title,
        snapshot.target_ms,
        &output_paths,
        &warnings,
    ))
}

fn run_analyze_study_free_run(
    config: MonitorConfig,
    out_dir: &Path,
    options: StudyOptions,
    free_run_count: Option<usize>,
) -> Result<()> {
    if config.devices.is_empty() {
        bail!("config has no devices for free-run analyze-phase");
    }

    println!(
        "analyze-phase free-run mode: watching {} devices, running global all-stream snapshots",
        config.devices.len()
    );
    if let Some(count) = free_run_count {
        println!("free-run stop condition: {count} successful analyses");
        println!("press Ctrl-C to stop early");
    } else {
        println!("press Ctrl-C to stop");
    }

    let (tx, rx) = mpsc::channel::<FreeRunSignal>();

    for device in config.devices.clone() {
        let tx_worker = tx.clone();
        let reconnect_initial_ms = config.reconnect_initial_ms;
        let reconnect_max_ms = config.reconnect_max_ms;
        thread::spawn(move || {
            if let Err(err) =
                run_free_run_watch_worker(device, reconnect_initial_ms, reconnect_max_ms, tx_worker)
            {
                eprintln!("free-run watch worker exited: {err}");
            }
        });
    }
    drop(tx);

    let mut last_written_target_ms: Option<u64> = None;
    let dedupe_tolerance_ms = target_bucket_tolerance_ms(&config);
    let mut successful = 0usize;
    loop {
        let signal = match rx.recv() {
            Ok(signal) => signal,
            Err(err) => bail!("free-run event channel closed: {err}"),
        };

        let snapshot = match analyze_spill_snapshot_with_retries(&config, None) {
            Ok(snapshot) => snapshot,
            Err(err) => {
                eprintln!(
                    "[analyze-phase free-run] snapshot after {} {} failed: {}",
                    signal.bpm_ip, signal.event.id, err
                );
                continue;
            }
        };

        if last_written_target_ms
            .map(|last| abs_diff_u64(last, snapshot.target_ms) <= dedupe_tolerance_ms)
            .unwrap_or(false)
        {
            continue;
        }

        let target_ms = snapshot.target_ms;
        let stem = format!("spill_{target_ms}");
        match run_analyze_study_for_snapshot(
            &config,
            out_dir,
            &options,
            snapshot,
            Some(&stem),
            &format!(
                "[analyze-phase free-run] wake {} {} (ms {})",
                signal.bpm_ip, signal.event.id, signal.event.ms
            ),
        ) {
            Ok(lines) => {
                let summary_path = out_dir.join(format!("{stem}_analyze_phase_summary.txt"));
                if let Err(err) = write_summary_text(&summary_path, &lines) {
                    eprintln!(
                        "[analyze-phase free-run] failed writing metadata summary {}: {}",
                        summary_path.display(),
                        err
                    );
                }
                last_written_target_ms = Some(target_ms);
                successful += 1;
                if let Some(limit) = free_run_count {
                    println!(
                        "[analyze-phase free-run] successful analyses: {}/{}",
                        successful, limit
                    );
                    if successful >= limit {
                        println!(
                            "[analyze-phase free-run] reached requested count ({}), exiting",
                            limit
                        );
                        return Ok(());
                    }
                }
            }
            Err(err) => {
                eprintln!(
                    "[analyze-phase free-run] failed writing outputs for target {}: {}",
                    target_ms, err
                );
            }
        }
    }
}

fn study_output_paths(
    out_dir: &Path,
    options: &StudyOptions,
    stem: Option<&str>,
) -> StudyOutputPaths {
    StudyOutputPaths {
        tune_vs_window_start: artifact_path(out_dir, "tune_vs_window_start.png", stem),
        tune_vs_window_length: artifact_path(out_dir, "tune_vs_window_length.png", stem),
        bpm_quality_table: artifact_path(out_dir, "bpm_quality_table.csv", stem),
        tune_by_bpm: artifact_path(out_dir, "tune_by_bpm.png", stem),
        confidence_by_bpm: artifact_path(out_dir, "confidence_by_bpm.png", stem),
        method_comparison: artifact_path(out_dir, "method_comparison.png", stem),
        findings_summary: artifact_path(out_dir, options.summary_file.as_str(), stem),
    }
}

fn artifact_path(out_dir: &Path, relative: &str, stem: Option<&str>) -> PathBuf {
    if let Some(stem) = stem {
        let relative_path = Path::new(relative);
        let file_name = relative_path
            .file_name()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_else(|| "artifact".to_string());
        let prefixed_name = format!("{stem}_{file_name}");
        match relative_path.parent() {
            Some(parent) if !parent.as_os_str().is_empty() => {
                out_dir.join(parent).join(prefixed_name)
            }
            _ => out_dir.join(prefixed_name),
        }
    } else {
        out_dir.join(relative)
    }
}

fn ensure_study_output_dirs(paths: &StudyOutputPaths) -> Result<()> {
    for path in [
        &paths.tune_vs_window_start,
        &paths.tune_vs_window_length,
        &paths.bpm_quality_table,
        &paths.tune_by_bpm,
        &paths.confidence_by_bpm,
        &paths.method_comparison,
        &paths.findings_summary,
    ] {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).with_context(|| {
                format!("failed to create output directory {}", parent.display())
            })?;
        }
    }
    Ok(())
}

fn print_study_summary(
    title: &str,
    target_ms: u64,
    paths: &StudyOutputPaths,
    warnings: &[String],
) -> Vec<String> {
    let mut lines = Vec::<String>::new();
    lines.push(title.to_string());
    lines.push(format!("target_ms: {target_ms}"));
    lines.push("analyze-phase outputs:".to_string());
    lines.push(format!("  {}", paths.tune_vs_window_start.display()));
    lines.push(format!("  {}", paths.tune_vs_window_length.display()));
    lines.push(format!("  {}", paths.bpm_quality_table.display()));
    lines.push(format!("  {}", paths.tune_by_bpm.display()));
    lines.push(format!("  {}", paths.confidence_by_bpm.display()));
    lines.push(format!("  {}", paths.method_comparison.display()));
    lines.push(format!("  {}", paths.findings_summary.display()));

    if warnings.is_empty() {
        lines.push("warnings: none".to_string());
    } else {
        lines.push(format!("warnings ({}):", warnings.len()));
        for warning in warnings {
            lines.push(format!("  - {}", warning));
        }
    }

    for line in &lines {
        println!("{line}");
    }
    lines
}

fn validate_batch_options(options: &BatchOptions) -> Result<()> {
    if options.count == 0 {
        bail!("count must be >= 1");
    }
    if options.min_confidence <= 0.0 || !options.min_confidence.is_finite() {
        bail!("min_confidence must be a finite value > 0");
    }
    if options.min_aligned_bpm_count == 0 {
        bail!("min_aligned_bpm_count must be >= 1");
    }
    if options.min_per_plane_bpm == 0 {
        bail!("min_per_plane_bpm must be >= 1");
    }
    if !options.peak_edge_margin.is_finite() || options.peak_edge_margin < 0.0 {
        bail!("peak_edge_margin must be finite and >= 0");
    }
    if matches!(options.flash_count, Some(0)) {
        bail!("flash_count must be >= 1 when provided");
    }
    Ok(())
}

fn is_flash_max_request(value: usize) -> bool {
    value == FLASH_COUNT_MAX
}

fn resolved_flash_count(requested: usize, total_turns: usize, window_turns: usize) -> usize {
    if window_turns == 0 {
        return 0;
    }
    let max_flashes = total_turns / window_turns;
    if is_flash_max_request(requested) {
        return max_flashes.max(1);
    }
    requested.max(1).min(max_flashes.max(1))
}

fn effective_injection_window_turns(config: &MonitorConfig, flash_count: Option<usize>) -> usize {
    if flash_count.is_some() {
        config.sliding_window_turns
    } else {
        config.injection_window_turns
    }
}

fn time_axis_value_from_turn(center_turn: usize, config: &MonitorConfig) -> f64 {
    if config.plot_time_axes_in_us {
        center_turn as f64 * config.turn_period_us
    } else {
        center_turn as f64
    }
}

fn time_axis_label(config: &MonitorConfig) -> &'static str {
    if config.plot_time_axes_in_us {
        "TIME AFTER INJECTION [US]"
    } else {
        "CENTER TURN"
    }
}

fn time_axis_z_label(config: &MonitorConfig) -> &'static str {
    if config.plot_time_axes_in_us {
        "TIME [US] (Z)"
    } else {
        "TURN (Z)"
    }
}

fn now_utc_label() -> String {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(dur) => format!("epoch_ms:{}", dur.as_millis()),
        Err(_) => "epoch_ms:0".to_string(),
    }
}

fn count_requested_tbt_streams(config: &MonitorConfig) -> usize {
    config
        .devices
        .iter()
        .flat_map(|device| device.stream_keys.iter())
        .filter(|key| classify_plane(key).is_some())
        .count()
}

fn resolve_trigger_timestamp(
    config: &MonitorConfig,
    target_ms: u64,
    warnings: &mut Vec<String>,
) -> Result<(u64, &'static str)> {
    let mut matches = Vec::<(u64, usize)>::new();

    for device in &config.devices {
        let mut conn = match connect_device(&device.redis) {
            Ok(conn) => conn,
            Err(err) => {
                warnings.push(format!(
                    "{}: trigger read connect failed: {}",
                    device.bpm_ip, err
                ));
                continue;
            }
        };

        let mut found = false;
        for (idx, key) in std::iter::once(&device.trigger_key)
            .chain(device.trigger_fallback_keys.iter())
            .enumerate()
        {
            match fetch_latest_entry(&mut conn, key) {
                Ok(Some((id, _))) => {
                    if let Some((ms, _)) = parse_stream_id(&id) {
                        matches.push((ms, idx));
                        found = true;
                        break;
                    }
                }
                Ok(None) => {}
                Err(err) => {
                    warnings.push(format!(
                        "{}: trigger read failed for {}: {}",
                        device.bpm_ip, key, err
                    ));
                }
            }
        }
        if !found {
            warnings.push(format!(
                "{}: no trigger stream id available from configured trigger keys",
                device.bpm_ip
            ));
        }
    }

    if matches.is_empty() {
        return Ok((target_ms, "target_ms_fallback"));
    }

    let ms_values = matches.iter().map(|(ms, _)| *ms).collect::<Vec<_>>();
    let chosen_ms = choose_target_millisecond(&ms_values, target_bucket_tolerance_ms(config))
        .unwrap_or(target_ms);
    let source = if matches
        .iter()
        .any(|(ms, rank)| *ms == chosen_ms && *rank == 0usize)
    {
        "trigger_key"
    } else if matches
        .iter()
        .any(|(ms, rank)| *ms == chosen_ms && *rank > 0usize)
    {
        "trigger_fallback"
    } else {
        "target_ms_fallback"
    };

    Ok((chosen_ms, source))
}

fn sliding_summary_by<F>(points: &[SlidingPoint], mut selector: F) -> SlidingSummary
where
    F: FnMut(&SlidingPoint) -> Option<f64>,
{
    let mut values = points
        .iter()
        .filter_map(&mut selector)
        .filter(|v| v.is_finite())
        .collect::<Vec<_>>();
    if values.is_empty() {
        return SlidingSummary {
            median: None,
            stddev: None,
            min: None,
            max: None,
        };
    }
    values.sort_by(|a, b| a.partial_cmp(b).unwrap_or(Ordering::Equal));
    let median_v = if values.len() % 2 == 0 {
        (values[values.len() / 2 - 1] + values[values.len() / 2]) * 0.5
    } else {
        values[values.len() / 2]
    };
    let min_v = values.first().copied();
    let max_v = values.last().copied();
    let std_v = stddev(&values);
    SlidingSummary {
        median: Some(median_v),
        stddev: std_v,
        min: min_v,
        max: max_v,
    }
}

fn build_spill_record(
    config: &MonitorConfig,
    options: &BatchOptions,
    snapshot: &SpillSnapshot,
    spill_index: usize,
    attempt_index: usize,
    trigger_ms: u64,
    trigger_source: String,
) -> Result<SpillRecord> {
    let requested_streams = count_requested_tbt_streams(config);
    let aligned_streams = snapshot
        .observations
        .iter()
        .filter(|obs| obs.aligned)
        .count();
    let aligned_fraction = aligned_streams as f64 / requested_streams.max(1) as f64;

    let h = snapshot.h_analysis.as_ref();
    let v = snapshot.v_analysis.as_ref();
    let injection_window_turns = effective_injection_window_turns(config, options.flash_count);
    let effective_flash_count = options.flash_count.and_then(|_| {
        [h.map(|a| a.sliding.len()), v.map(|a| a.sliding.len())]
            .into_iter()
            .flatten()
            .max()
    });

    let used_streams_h = h.map(|a| a.traces_used).unwrap_or(0);
    let used_streams_v = v.map(|a| a.traces_used).unwrap_or(0);
    let used_streams_total = used_streams_h + used_streams_v;

    let consensus_turns_h = h.map(|a| a.consensus_turns);
    let consensus_turns_v = v.map(|a| a.consensus_turns);
    let consensus_turns_global = [consensus_turns_h, consensus_turns_v]
        .into_iter()
        .flatten()
        .min();

    let qx_injection = h.and_then(|a| a.injection_peak.as_ref().map(|p| p.tune));
    let qy_injection = v.and_then(|a| a.injection_peak.as_ref().map(|p| p.tune));
    let confidence_h = h.and_then(|a| a.injection_peak.as_ref().map(|p| p.confidence));
    let confidence_v = v.and_then(|a| a.injection_peak.as_ref().map(|p| p.confidence));

    let h_slide_raw = h
        .map(|a| sliding_summary_by(&a.sliding, |point| point.raw_global_tune))
        .unwrap_or(SlidingSummary {
            median: None,
            stddev: None,
            min: None,
            max: None,
        });
    let v_slide_raw = v
        .map(|a| sliding_summary_by(&a.sliding, |point| point.raw_global_tune))
        .unwrap_or(SlidingSummary {
            median: None,
            stddev: None,
            min: None,
            max: None,
        });
    let h_slide_tracked = h
        .map(|a| sliding_summary_by(&a.sliding, |point| point.selected_tune))
        .unwrap_or(SlidingSummary {
            median: None,
            stddev: None,
            min: None,
            max: None,
        });
    let v_slide_tracked = v
        .map(|a| sliding_summary_by(&a.sliding, |point| point.selected_tune))
        .unwrap_or(SlidingSummary {
            median: None,
            stddev: None,
            min: None,
            max: None,
        });

    let mut quality_flags = Vec::<String>::new();
    let mut bad = false;
    let mut marginal = false;

    if used_streams_total == 0 {
        quality_flags.push("NO_USABLE_STREAMS".to_string());
        bad = true;
    }
    if qx_injection.is_none() {
        quality_flags.push("NO_QX_IN_BAND".to_string());
        bad = true;
    }
    if qy_injection.is_none() {
        quality_flags.push("NO_QY_IN_BAND".to_string());
        bad = true;
    }
    for (name, value) in [("QX", qx_injection), ("QY", qy_injection)] {
        if let Some(vv) = value {
            if !vv.is_finite() {
                quality_flags.push(format!("{name}_NON_FINITE"));
                bad = true;
            }
        }
    }

    if aligned_fraction < config.min_aligned_fraction {
        quality_flags.push("LOW_ALIGNMENT_FRACTION".to_string());
        marginal = true;
    }
    // Missing configured streams is a data-quality issue, not an automatic hard
    // failure: keep the spill for diagnostics but mark as marginal.
    if snapshot.observations.len() < requested_streams {
        quality_flags.push("INCOMPLETE_TBT_POLL".to_string());
        marginal = true;
    }
    if used_streams_total < options.min_aligned_bpm_count {
        quality_flags.push("LOW_ALIGNED_BPM_COUNT".to_string());
        marginal = true;
    }
    if used_streams_h < options.min_per_plane_bpm {
        quality_flags.push("LOW_H_PLANE_BPM_COUNT".to_string());
        marginal = true;
    }
    if used_streams_v < options.min_per_plane_bpm {
        quality_flags.push("LOW_V_PLANE_BPM_COUNT".to_string());
        marginal = true;
    }
    if confidence_h.unwrap_or(0.0) < options.min_confidence {
        quality_flags.push("LOW_CONFIDENCE_H".to_string());
        marginal = true;
    }
    if confidence_v.unwrap_or(0.0) < options.min_confidence {
        quality_flags.push("LOW_CONFIDENCE_V".to_string());
        marginal = true;
    }
    if let Some(qx) = qx_injection {
        if qx < config.qx_band_min || qx > config.qx_band_max {
            quality_flags.push("QX_OUTSIDE_BAND".to_string());
            bad = true;
        } else if (qx - config.qx_band_min) < options.peak_edge_margin
            || (config.qx_band_max - qx) < options.peak_edge_margin
        {
            quality_flags.push("QX_NEAR_BAND_EDGE".to_string());
            marginal = true;
        }
    }
    if let Some(qy) = qy_injection {
        if qy < config.qy_band_min || qy > config.qy_band_max {
            quality_flags.push("QY_OUTSIDE_BAND".to_string());
            bad = true;
        } else if (qy - config.qy_band_min) < options.peak_edge_margin
            || (config.qy_band_max - qy) < options.peak_edge_margin
        {
            quality_flags.push("QY_NEAR_BAND_EDGE".to_string());
            marginal = true;
        }
    }

    let status = if bad {
        SpillStatus::Failed
    } else if qx_injection.is_some() && qy_injection.is_some() {
        SpillStatus::Ok
    } else {
        SpillStatus::Partial
    };

    let quality_label = if bad {
        SpillQuality::Bad
    } else if marginal {
        SpillQuality::Marginal
    } else {
        SpillQuality::Good
    };

    let record = SpillRecord {
        spill_index,
        attempt_index,
        spill_uid: snapshot.target_ms,
        captured_at_utc: now_utc_label(),
        target_ms: snapshot.target_ms,
        trigger_ms,
        trigger_source,
        aligned_fraction,
        aligned_streams,
        requested_streams,
        used_streams_total,
        used_streams_h,
        used_streams_v,
        consensus_turns_h,
        consensus_turns_v,
        consensus_turns_global,
        injection_start_turn: config.injection_start_turn,
        injection_window_turns,
        sliding_window_turns: config.sliding_window_turns,
        sliding_stride_turns: config.sliding_stride_turns,
        flash_count: effective_flash_count,
        qx_band_min: config.qx_band_min,
        qx_band_max: config.qx_band_max,
        qy_band_min: config.qy_band_min,
        qy_band_max: config.qy_band_max,
        qx_injection,
        qy_injection,
        confidence_h,
        confidence_v,
        median_qx: h_slide_tracked.median,
        median_qy: v_slide_tracked.median,
        std_qx: h_slide_tracked.stddev,
        std_qy: v_slide_tracked.stddev,
        min_qx: h_slide_tracked.min,
        max_qx: h_slide_tracked.max,
        min_qy: v_slide_tracked.min,
        max_qy: v_slide_tracked.max,
        median_qx_raw: h_slide_raw.median,
        std_qx_raw: h_slide_raw.stddev,
        min_qx_raw: h_slide_raw.min,
        max_qx_raw: h_slide_raw.max,
        median_qy_raw: v_slide_raw.median,
        std_qy_raw: v_slide_raw.stddev,
        min_qy_raw: v_slide_raw.min,
        max_qy_raw: v_slide_raw.max,
        median_qx_tracked: h_slide_tracked.median,
        std_qx_tracked: h_slide_tracked.stddev,
        min_qx_tracked: h_slide_tracked.min,
        max_qx_tracked: h_slide_tracked.max,
        median_qy_tracked: v_slide_tracked.median,
        std_qy_tracked: v_slide_tracked.stddev,
        min_qy_tracked: v_slide_tracked.min,
        max_qy_tracked: v_slide_tracked.max,
        sliding_fallback_count_h: h.map(|a| a.sliding_fallback_count).unwrap_or(0),
        sliding_fallback_count_v: v.map(|a| a.sliding_fallback_count).unwrap_or(0),
        sliding_suspicious_count_h: h.map(|a| a.sliding_suspicious_count).unwrap_or(0),
        sliding_suspicious_count_v: v.map(|a| a.sliding_suspicious_count).unwrap_or(0),
        max_rms_bpm_h: h.and_then(|a| a.max_rms_bpm),
        max_rms_bpm_v: v.and_then(|a| a.max_rms_bpm),
        quality_label,
        status,
        quality_flags,
        warnings: snapshot.warnings.clone(),
        participating_bpms_h: h.map(|a| a.participating_bpms.clone()).unwrap_or_default(),
        participating_bpms_v: v.map(|a| a.participating_bpms.clone()).unwrap_or_default(),
        best_bpm_stream_h: h.and_then(|a| a.best_bpm_stream.clone()),
        best_bpm_stream_v: v.and_then(|a| a.best_bpm_stream.clone()),
        ref_qx: None,
        ref_qy: None,
        residual_qx: None,
        residual_qy: None,
    };

    Ok(record)
}

fn load_reference_file(path: &Path) -> Result<Vec<BatchReference>> {
    let raw =
        fs::read_to_string(path).with_context(|| format!("failed to read {}", path.display()))?;
    let mut lines = raw
        .lines()
        .map(|line| line.trim())
        .filter(|line| !line.is_empty() && !line.starts_with('#'));

    let header = lines
        .next()
        .ok_or_else(|| anyhow!("reference file {} is empty", path.display()))?;
    let cols = header
        .split(',')
        .map(|c| c.trim().to_ascii_lowercase())
        .collect::<Vec<_>>();

    let idx_target_ms = cols.iter().position(|c| c == "target_ms");
    let idx_spill_index = cols.iter().position(|c| c == "spill_index");
    let idx_qx = cols
        .iter()
        .position(|c| c == "qx_ref" || c == "qx")
        .ok_or_else(|| anyhow!("reference file missing qx_ref (or qx) column"))?;
    let idx_qy = cols
        .iter()
        .position(|c| c == "qy_ref" || c == "qy")
        .ok_or_else(|| anyhow!("reference file missing qy_ref (or qy) column"))?;

    let mut out = Vec::<BatchReference>::new();
    for line in lines {
        let parts = line.split(',').map(|s| s.trim()).collect::<Vec<_>>();
        let target_ms = idx_target_ms
            .and_then(|idx| parts.get(idx))
            .and_then(|v| v.parse::<u64>().ok());
        let spill_index = idx_spill_index
            .and_then(|idx| parts.get(idx))
            .and_then(|v| v.parse::<usize>().ok());
        let qx = parts.get(idx_qx).and_then(|v| v.parse::<f64>().ok());
        let qy = parts.get(idx_qy).and_then(|v| v.parse::<f64>().ok());
        out.push(BatchReference {
            target_ms,
            spill_index,
            qx,
            qy,
        });
    }

    Ok(out)
}

fn apply_reference_match(
    record: &mut SpillRecord,
    references: &[BatchReference],
    key: ReferenceKey,
    tolerance_ms: u64,
) {
    if references.is_empty() {
        return;
    }

    let matched = match key {
        ReferenceKey::SpillIndex => references.iter().find(|entry| {
            entry
                .spill_index
                .map(|v| v == record.spill_index)
                .unwrap_or(false)
        }),
        ReferenceKey::TargetMs => references
            .iter()
            .filter_map(|entry| {
                entry
                    .target_ms
                    .map(|ms| (entry, abs_diff_u64(ms, record.target_ms)))
            })
            .filter(|(_, diff)| *diff <= tolerance_ms)
            .min_by(|(_, a), (_, b)| a.cmp(b))
            .map(|(entry, _)| entry),
    };

    if let Some(entry) = matched {
        record.ref_qx = entry.qx;
        record.ref_qy = entry.qy;
        record.residual_qx = match (record.qx_injection, entry.qx) {
            (Some(a), Some(b)) => Some(a - b),
            _ => None,
        };
        record.residual_qy = match (record.qy_injection, entry.qy) {
            (Some(a), Some(b)) => Some(a - b),
            _ => None,
        };
    }
}

fn write_batch_records(
    out_dir: &Path,
    results: &[BatchSpillResult],
    format: BatchRecordFormat,
) -> Result<()> {
    match format {
        BatchRecordFormat::Csv => {
            write_batch_records_csv(&out_dir.join("spills_summary.csv"), results)?
        }
        BatchRecordFormat::Jsonl => {
            write_batch_records_jsonl(&out_dir.join("spills_summary.jsonl"), results)?
        }
        BatchRecordFormat::Both => {
            write_batch_records_csv(&out_dir.join("spills_summary.csv"), results)?;
            write_batch_records_jsonl(&out_dir.join("spills_summary.jsonl"), results)?;
        }
    }
    Ok(())
}

fn write_batch_records_csv(path: &Path, results: &[BatchSpillResult]) -> Result<()> {
    let mut rows = Vec::<String>::new();
    rows.push(
        "spill_index,attempt_index,spill_uid,captured_at_utc,target_ms,trigger_ms,trigger_source,\
aligned_fraction,aligned_streams,requested_streams,used_streams_total,used_streams_h,used_streams_v,\
consensus_turns_h,consensus_turns_v,consensus_turns_global,injection_start_turn,injection_window_turns,\
sliding_window_turns,sliding_stride_turns,flash_count,qx_band_min,qx_band_max,qy_band_min,qy_band_max,\
qx_injection,qy_injection,confidence_h,confidence_v,median_qx,median_qy,std_qx,std_qy,min_qx,max_qx,min_qy,max_qy,\
median_qx_raw,std_qx_raw,min_qx_raw,max_qx_raw,median_qy_raw,std_qy_raw,min_qy_raw,max_qy_raw,\
median_qx_tracked,std_qx_tracked,min_qx_tracked,max_qx_tracked,median_qy_tracked,std_qy_tracked,min_qy_tracked,max_qy_tracked,\
sliding_fallback_count_h,sliding_fallback_count_v,sliding_suspicious_count_h,sliding_suspicious_count_v,\
max_rms_bpm_h,max_rms_bpm_v,quality_label,status,quality_flags,warnings,participating_bpms_h,participating_bpms_v,\
best_bpm_stream_h,best_bpm_stream_v,ref_qx,ref_qy,residual_qx,residual_qy"
            .to_string(),
    );

    for result in results {
        let r = &result.record;
        let fields = vec![
            r.spill_index.to_string(),
            r.attempt_index.to_string(),
            r.spill_uid.to_string(),
            csv_escape(&r.captured_at_utc),
            r.target_ms.to_string(),
            r.trigger_ms.to_string(),
            csv_escape(&r.trigger_source),
            format!("{:.6}", r.aligned_fraction),
            r.aligned_streams.to_string(),
            r.requested_streams.to_string(),
            r.used_streams_total.to_string(),
            r.used_streams_h.to_string(),
            r.used_streams_v.to_string(),
            opt_usize(r.consensus_turns_h),
            opt_usize(r.consensus_turns_v),
            opt_usize(r.consensus_turns_global),
            r.injection_start_turn.to_string(),
            r.injection_window_turns.to_string(),
            r.sliding_window_turns.to_string(),
            r.sliding_stride_turns.to_string(),
            opt_usize(r.flash_count),
            format!("{:.6}", r.qx_band_min),
            format!("{:.6}", r.qx_band_max),
            format!("{:.6}", r.qy_band_min),
            format!("{:.6}", r.qy_band_max),
            opt_fmt(r.qx_injection),
            opt_fmt(r.qy_injection),
            opt_fmt(r.confidence_h),
            opt_fmt(r.confidence_v),
            opt_fmt(r.median_qx),
            opt_fmt(r.median_qy),
            opt_fmt(r.std_qx),
            opt_fmt(r.std_qy),
            opt_fmt(r.min_qx),
            opt_fmt(r.max_qx),
            opt_fmt(r.min_qy),
            opt_fmt(r.max_qy),
            opt_fmt(r.median_qx_raw),
            opt_fmt(r.std_qx_raw),
            opt_fmt(r.min_qx_raw),
            opt_fmt(r.max_qx_raw),
            opt_fmt(r.median_qy_raw),
            opt_fmt(r.std_qy_raw),
            opt_fmt(r.min_qy_raw),
            opt_fmt(r.max_qy_raw),
            opt_fmt(r.median_qx_tracked),
            opt_fmt(r.std_qx_tracked),
            opt_fmt(r.min_qx_tracked),
            opt_fmt(r.max_qx_tracked),
            opt_fmt(r.median_qy_tracked),
            opt_fmt(r.std_qy_tracked),
            opt_fmt(r.min_qy_tracked),
            opt_fmt(r.max_qy_tracked),
            r.sliding_fallback_count_h.to_string(),
            r.sliding_fallback_count_v.to_string(),
            r.sliding_suspicious_count_h.to_string(),
            r.sliding_suspicious_count_v.to_string(),
            opt_fmt(r.max_rms_bpm_h),
            opt_fmt(r.max_rms_bpm_v),
            r.quality_label.label().to_string(),
            r.status.label().to_string(),
            csv_escape(&r.quality_flags.join("|")),
            csv_escape(&r.warnings.join("|")),
            csv_escape(&r.participating_bpms_h.join("|")),
            csv_escape(&r.participating_bpms_v.join("|")),
            csv_escape(&r.best_bpm_stream_h.clone().unwrap_or_default()),
            csv_escape(&r.best_bpm_stream_v.clone().unwrap_or_default()),
            opt_fmt(r.ref_qx),
            opt_fmt(r.ref_qy),
            opt_fmt(r.residual_qx),
            opt_fmt(r.residual_qy),
        ];
        rows.push(fields.join(","));
    }

    fs::write(path, rows.join("\n") + "\n")
        .with_context(|| format!("failed to write {}", path.display()))
}

fn write_batch_records_jsonl(path: &Path, results: &[BatchSpillResult]) -> Result<()> {
    let mut lines = Vec::<String>::new();
    for result in results {
        let r = &result.record;
        lines.push(format!(
            "{{\"spill_index\":{},\"attempt_index\":{},\"spill_uid\":{},\"captured_at_utc\":{},\"target_ms\":{},\"trigger_ms\":{},\"trigger_source\":{},\"aligned_fraction\":{:.6},\"aligned_streams\":{},\"requested_streams\":{},\"used_streams_total\":{},\"used_streams_h\":{},\"used_streams_v\":{},\"consensus_turns_h\":{},\"consensus_turns_v\":{},\"consensus_turns_global\":{},\"injection_start_turn\":{},\"injection_window_turns\":{},\"sliding_window_turns\":{},\"sliding_stride_turns\":{},\"flash_count\":{},\"qx_band_min\":{:.6},\"qx_band_max\":{:.6},\"qy_band_min\":{:.6},\"qy_band_max\":{:.6},\"qx_injection\":{},\"qy_injection\":{},\"confidence_h\":{},\"confidence_v\":{},\"median_qx\":{},\"median_qy\":{},\"std_qx\":{},\"std_qy\":{},\"min_qx\":{},\"max_qx\":{},\"min_qy\":{},\"max_qy\":{},\"median_qx_raw\":{},\"std_qx_raw\":{},\"min_qx_raw\":{},\"max_qx_raw\":{},\"median_qy_raw\":{},\"std_qy_raw\":{},\"min_qy_raw\":{},\"max_qy_raw\":{},\"median_qx_tracked\":{},\"std_qx_tracked\":{},\"min_qx_tracked\":{},\"max_qx_tracked\":{},\"median_qy_tracked\":{},\"std_qy_tracked\":{},\"min_qy_tracked\":{},\"max_qy_tracked\":{},\"sliding_fallback_count_h\":{},\"sliding_fallback_count_v\":{},\"sliding_suspicious_count_h\":{},\"sliding_suspicious_count_v\":{},\"max_rms_bpm_h\":{},\"max_rms_bpm_v\":{},\"quality_label\":{},\"status\":{},\"quality_flags\":{},\"warnings\":{},\"participating_bpms_h\":{},\"participating_bpms_v\":{},\"best_bpm_stream_h\":{},\"best_bpm_stream_v\":{},\"ref_qx\":{},\"ref_qy\":{},\"residual_qx\":{},\"residual_qy\":{}}}",
            r.spill_index,
            r.attempt_index,
            r.spill_uid,
            json_string(&r.captured_at_utc),
            r.target_ms,
            r.trigger_ms,
            json_string(&r.trigger_source),
            r.aligned_fraction,
            r.aligned_streams,
            r.requested_streams,
            r.used_streams_total,
            r.used_streams_h,
            r.used_streams_v,
            json_opt_usize(r.consensus_turns_h),
            json_opt_usize(r.consensus_turns_v),
            json_opt_usize(r.consensus_turns_global),
            r.injection_start_turn,
            r.injection_window_turns,
            r.sliding_window_turns,
            r.sliding_stride_turns,
            json_opt_usize(r.flash_count),
            r.qx_band_min,
            r.qx_band_max,
            r.qy_band_min,
            r.qy_band_max,
            json_opt_f64(r.qx_injection),
            json_opt_f64(r.qy_injection),
            json_opt_f64(r.confidence_h),
            json_opt_f64(r.confidence_v),
            json_opt_f64(r.median_qx),
            json_opt_f64(r.median_qy),
            json_opt_f64(r.std_qx),
            json_opt_f64(r.std_qy),
            json_opt_f64(r.min_qx),
            json_opt_f64(r.max_qx),
            json_opt_f64(r.min_qy),
            json_opt_f64(r.max_qy),
            json_opt_f64(r.median_qx_raw),
            json_opt_f64(r.std_qx_raw),
            json_opt_f64(r.min_qx_raw),
            json_opt_f64(r.max_qx_raw),
            json_opt_f64(r.median_qy_raw),
            json_opt_f64(r.std_qy_raw),
            json_opt_f64(r.min_qy_raw),
            json_opt_f64(r.max_qy_raw),
            json_opt_f64(r.median_qx_tracked),
            json_opt_f64(r.std_qx_tracked),
            json_opt_f64(r.min_qx_tracked),
            json_opt_f64(r.max_qx_tracked),
            json_opt_f64(r.median_qy_tracked),
            json_opt_f64(r.std_qy_tracked),
            json_opt_f64(r.min_qy_tracked),
            json_opt_f64(r.max_qy_tracked),
            r.sliding_fallback_count_h,
            r.sliding_fallback_count_v,
            r.sliding_suspicious_count_h,
            r.sliding_suspicious_count_v,
            json_opt_f64(r.max_rms_bpm_h),
            json_opt_f64(r.max_rms_bpm_v),
            json_string(r.quality_label.label()),
            json_string(r.status.label()),
            json_string_array(&r.quality_flags),
            json_string_array(&r.warnings),
            json_string_array(&r.participating_bpms_h),
            json_string_array(&r.participating_bpms_v),
            json_opt_string(r.best_bpm_stream_h.as_deref()),
            json_opt_string(r.best_bpm_stream_v.as_deref()),
            json_opt_f64(r.ref_qx),
            json_opt_f64(r.ref_qy),
            json_opt_f64(r.residual_qx),
            json_opt_f64(r.residual_qy),
        ));
    }

    fs::write(path, lines.join("\n") + "\n")
        .with_context(|| format!("failed to write {}", path.display()))
}

#[derive(Debug, Clone)]
struct BatchAggregate {
    spills_analyzed: usize,
    good_count: usize,
    marginal_count: usize,
    bad_count: usize,
    failed_analysis_count: usize,
    unresolved_wake_count: usize,
    duplicate_wake_count: usize,
    median_qx_good: Option<f64>,
    median_qy_good: Option<f64>,
    std_qx_good: Option<f64>,
    std_qy_good: Option<f64>,
    median_conf_h_good: Option<f64>,
    median_conf_v_good: Option<f64>,
    median_aligned_fraction: Option<f64>,
    median_timeliness_median_abs_ms: Option<f64>,
    median_timeliness_max_abs_ms: Option<f64>,
    worst_timeliness_max_abs_ms: Option<f64>,
    stale_depth_scanned: Option<usize>,
    historical_candidates_discovered: usize,
    historical_candidates_attempted: usize,
    historical_candidates_skipped: usize,
}

fn summarize_batch(results: &[BatchSpillResult], counters: &BatchRunCounters) -> BatchAggregate {
    let spills_analyzed = results.len();
    let good_count = results
        .iter()
        .filter(|r| r.record.quality_label == SpillQuality::Good)
        .count();
    let marginal_count = results
        .iter()
        .filter(|r| r.record.quality_label == SpillQuality::Marginal)
        .count();
    let bad_count = results
        .iter()
        .filter(|r| r.record.quality_label == SpillQuality::Bad)
        .count();
    let failed_analysis_count = results
        .iter()
        .filter(|r| r.record.status == SpillStatus::Failed)
        .count();

    let good_records = results
        .iter()
        .filter(|r| r.record.quality_label == SpillQuality::Good)
        .map(|r| &r.record)
        .collect::<Vec<_>>();

    let qx_good = good_records
        .iter()
        .filter_map(|r| r.qx_injection)
        .collect::<Vec<_>>();
    let qy_good = good_records
        .iter()
        .filter_map(|r| r.qy_injection)
        .collect::<Vec<_>>();
    let conf_h_good = good_records
        .iter()
        .filter_map(|r| r.confidence_h)
        .collect::<Vec<_>>();
    let conf_v_good = good_records
        .iter()
        .filter_map(|r| r.confidence_v)
        .collect::<Vec<_>>();
    let aligned = results
        .iter()
        .map(|r| r.record.aligned_fraction)
        .collect::<Vec<_>>();
    let timeliness_median_abs = results
        .iter()
        .filter_map(|r| timeliness_stats(&r.snapshot.observations, r.snapshot.target_ms))
        .map(|stats| stats.median_abs_delta_ms)
        .collect::<Vec<_>>();
    let timeliness_max_abs = results
        .iter()
        .filter_map(|r| timeliness_stats(&r.snapshot.observations, r.snapshot.target_ms))
        .map(|stats| stats.max_abs_delta_ms as f64)
        .collect::<Vec<_>>();

    BatchAggregate {
        spills_analyzed,
        good_count,
        marginal_count,
        bad_count,
        failed_analysis_count,
        unresolved_wake_count: counters.unresolved_wakes,
        duplicate_wake_count: counters.duplicate_wakes,
        median_qx_good: median(&qx_good),
        median_qy_good: median(&qy_good),
        std_qx_good: stddev(&qx_good),
        std_qy_good: stddev(&qy_good),
        median_conf_h_good: median(&conf_h_good),
        median_conf_v_good: median(&conf_v_good),
        median_aligned_fraction: median(&aligned),
        median_timeliness_median_abs_ms: median(&timeliness_median_abs),
        median_timeliness_max_abs_ms: median(&timeliness_max_abs),
        worst_timeliness_max_abs_ms: timeliness_max_abs.iter().copied().reduce(f64::max),
        stale_depth_scanned: counters.stale_depth_scanned,
        historical_candidates_discovered: counters.historical_candidates_discovered,
        historical_candidates_attempted: counters.historical_candidates_attempted,
        historical_candidates_skipped: counters.historical_candidates_skipped,
    }
}

fn print_batch_console_summary(aggregate: &BatchAggregate) {
    println!("batch summary:");
    println!("  spills analyzed: {}", aggregate.spills_analyzed);
    println!(
        "  quality counts: GOOD={} MARGINAL={} BAD={}",
        aggregate.good_count, aggregate.marginal_count, aggregate.bad_count
    );
    println!(
        "  failed-analysis count: {}",
        aggregate.failed_analysis_count
    );
    println!(
        "  unresolved wake count: {}",
        aggregate.unresolved_wake_count
    );
    println!("  duplicate wake count: {}", aggregate.duplicate_wake_count);
    println!(
        "  median Qx/Qy (GOOD): {} / {}",
        opt_fmt(aggregate.median_qx_good),
        opt_fmt(aggregate.median_qy_good)
    );
    println!(
        "  stddev Qx/Qy (GOOD): {} / {}",
        opt_fmt(aggregate.std_qx_good),
        opt_fmt(aggregate.std_qy_good)
    );
    println!(
        "  median confidence H/V (GOOD): {} / {}",
        opt_fmt(aggregate.median_conf_h_good),
        opt_fmt(aggregate.median_conf_v_good)
    );
    println!(
        "  median aligned fraction: {}",
        opt_fmt(aggregate.median_aligned_fraction)
    );
    println!(
        "  timeliness median|delta| ms: {}",
        opt_fmt(aggregate.median_timeliness_median_abs_ms)
    );
    println!(
        "  timeliness median max|delta| ms: {}",
        opt_fmt(aggregate.median_timeliness_max_abs_ms)
    );
    println!(
        "  timeliness worst max|delta| ms: {}",
        opt_fmt(aggregate.worst_timeliness_max_abs_ms)
    );
    if let Some(stale_depth) = aggregate.stale_depth_scanned {
        println!("  stale_depth scanned: {}", stale_depth);
        println!(
            "  historical candidates discovered (merged target windows): {}",
            aggregate.historical_candidates_discovered
        );
        println!(
            "  historical candidates attempted: {}",
            aggregate.historical_candidates_attempted
        );
        println!(
            "  historical candidates skipped unresolved: {}",
            aggregate.historical_candidates_skipped
        );
        println!("  successful analyses: {}", aggregate.spills_analyzed);
    }
}

fn write_batch_summary_markdown(
    out_dir: &Path,
    aggregate: &BatchAggregate,
    options: &BatchOptions,
    has_reference: bool,
) -> Result<()> {
    let mut lines = Vec::<String>::new();
    lines.push("# Batch Summary".to_string());
    lines.push(String::new());
    lines.push("## Aggregate".to_string());
    lines.push(format!("- spills analyzed: {}", aggregate.spills_analyzed));
    lines.push(format!(
        "- quality counts: GOOD={} MARGINAL={} BAD={}",
        aggregate.good_count, aggregate.marginal_count, aggregate.bad_count
    ));
    lines.push(format!(
        "- failed-analysis count: {}",
        aggregate.failed_analysis_count
    ));
    lines.push(format!(
        "- unresolved wake count: {}",
        aggregate.unresolved_wake_count
    ));
    lines.push(format!(
        "- duplicate wake count: {}",
        aggregate.duplicate_wake_count
    ));
    lines.push(format!(
        "- median Qx/Qy (GOOD): {} / {}",
        opt_fmt(aggregate.median_qx_good),
        opt_fmt(aggregate.median_qy_good)
    ));
    lines.push(format!(
        "- stddev Qx/Qy (GOOD): {} / {}",
        opt_fmt(aggregate.std_qx_good),
        opt_fmt(aggregate.std_qy_good)
    ));
    lines.push(format!(
        "- median confidence H/V (GOOD): {} / {}",
        opt_fmt(aggregate.median_conf_h_good),
        opt_fmt(aggregate.median_conf_v_good)
    ));
    lines.push(format!(
        "- median aligned fraction: {}",
        opt_fmt(aggregate.median_aligned_fraction)
    ));
    lines.push(format!(
        "- timeliness median|delta| ms: {}",
        opt_fmt(aggregate.median_timeliness_median_abs_ms)
    ));
    lines.push(format!(
        "- timeliness median max|delta| ms: {}",
        opt_fmt(aggregate.median_timeliness_max_abs_ms)
    ));
    lines.push(format!(
        "- timeliness worst max|delta| ms: {}",
        opt_fmt(aggregate.worst_timeliness_max_abs_ms)
    ));
    if let Some(stale_depth) = aggregate.stale_depth_scanned {
        lines.push(String::new());
        lines.push("## Historical Source Diagnostics".to_string());
        lines.push(format!("- stale_depth scanned: {}", stale_depth));
        lines.push(format!(
            "- historical candidates discovered (merged target windows): {}",
            aggregate.historical_candidates_discovered
        ));
        lines.push(format!(
            "- historical candidates attempted: {}",
            aggregate.historical_candidates_attempted
        ));
        lines.push(format!(
            "- historical candidates skipped unresolved: {}",
            aggregate.historical_candidates_skipped
        ));
        lines.push(format!(
            "- successful analyses: {}",
            aggregate.spills_analyzed
        ));
    }
    lines.push(String::new());
    lines.push("## Confidence Definition".to_string());
    lines.push("- confidence = peak_power / median_band_power".to_string());
    lines.push(String::new());
    lines.push("## Reference Hook".to_string());
    lines.push(format!(
        "- reference provided: {}",
        if has_reference { "yes" } else { "no" }
    ));
    lines.push(format!(
        "- reference key mode: {}",
        match options.reference_key {
            ReferenceKey::TargetMs => "target_ms",
            ReferenceKey::SpillIndex => "spill_index",
        }
    ));
    lines.push(String::new());
    lines.push("## Limitations".to_string());
    lines.push(format!("- {}", BATCH_SUMMARY_LIMITATIONS));

    fs::write(out_dir.join("batch_summary.md"), lines.join("\n") + "\n").with_context(|| {
        format!(
            "failed to write {}",
            out_dir.join("batch_summary.md").display()
        )
    })
}

fn write_batch_detailed_artifacts(
    out_dir: &Path,
    config: &MonitorConfig,
    results: &[BatchSpillResult],
    mode: DetailedArtifactsMode,
    flash_count: Option<usize>,
) -> Result<()> {
    let selected = select_detailed_spill_indices(results, mode);
    for idx in selected {
        let entry = &results[idx];
        let stem = format!(
            "spill_{}_{}",
            entry.record.spill_index, entry.record.target_ms
        );
        let paths =
            write_spill_outputs(out_dir, Some(&stem), config, &entry.snapshot, flash_count)?;
        let lines = compose_spill_summary_lines(
            config,
            &entry.snapshot,
            &paths,
            &format!(
                "batch spill summary {} [{}]",
                entry.record.spill_index,
                entry.record.quality_label.label()
            ),
            false,
        );
        write_summary_text(&out_dir.join(format!("{stem}_summary.txt")), &lines)?;
    }
    Ok(())
}

fn select_detailed_spill_indices(
    results: &[BatchSpillResult],
    mode: DetailedArtifactsMode,
) -> Vec<usize> {
    match mode {
        DetailedArtifactsMode::None => Vec::new(),
        DetailedArtifactsMode::All => (0..results.len()).collect::<Vec<_>>(),
        DetailedArtifactsMode::Representative => {
            if results.is_empty() {
                return Vec::new();
            }
            let mut selected = HashSet::<usize>::new();
            selected.insert(0usize);

            if let Some((idx, _)) = results.iter().enumerate().max_by(|(_, a), (_, b)| {
                combined_confidence(&a.record)
                    .partial_cmp(&combined_confidence(&b.record))
                    .unwrap_or(Ordering::Equal)
            }) {
                selected.insert(idx);
            }
            if let Some((idx, _)) = results.iter().enumerate().min_by(|(_, a), (_, b)| {
                combined_confidence(&a.record)
                    .partial_cmp(&combined_confidence(&b.record))
                    .unwrap_or(Ordering::Equal)
            }) {
                selected.insert(idx);
            }
            if let Some((idx, _)) = results.iter().enumerate().min_by(|(_, a), (_, b)| {
                a.record
                    .aligned_fraction
                    .partial_cmp(&b.record.aligned_fraction)
                    .unwrap_or(Ordering::Equal)
            }) {
                selected.insert(idx);
            }

            for (idx, entry) in results.iter().enumerate() {
                if entry.record.quality_label == SpillQuality::Bad {
                    selected.insert(idx);
                }
            }

            let mut out = selected.into_iter().collect::<Vec<_>>();
            out.sort_unstable();
            out
        }
    }
}

fn combined_confidence(record: &SpillRecord) -> f64 {
    median(
        &[
            record.confidence_h.unwrap_or(0.0),
            record.confidence_v.unwrap_or(0.0),
        ]
        .into_iter()
        .filter(|v| v.is_finite())
        .collect::<Vec<_>>(),
    )
    .unwrap_or(0.0)
}

fn opt_usize(value: Option<usize>) -> String {
    value
        .map(|v| v.to_string())
        .unwrap_or_else(|| "NA".to_string())
}

fn csv_escape(value: &str) -> String {
    if value.contains(',') || value.contains('"') || value.contains('\n') {
        format!("\"{}\"", value.replace('"', "\"\""))
    } else {
        value.to_string()
    }
}

fn json_escape(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
        .replace('\r', "\\r")
        .replace('\t', "\\t")
}

fn json_string(value: &str) -> String {
    format!("\"{}\"", json_escape(value))
}

fn json_opt_string(value: Option<&str>) -> String {
    value.map(json_string).unwrap_or_else(|| "null".to_string())
}

fn json_opt_f64(value: Option<f64>) -> String {
    value
        .filter(|v| v.is_finite())
        .map(|v| format!("{v:.6}"))
        .unwrap_or_else(|| "null".to_string())
}

fn json_opt_usize(value: Option<usize>) -> String {
    value
        .map(|v| v.to_string())
        .unwrap_or_else(|| "null".to_string())
}

fn json_string_array(values: &[String]) -> String {
    let parts = values.iter().map(|v| json_string(v)).collect::<Vec<_>>();
    format!("[{}]", parts.join(","))
}

fn required_value<'a>(obj: &'a Map<String, Value>, key: &str, context: &str) -> Result<&'a Value> {
    obj.get(key)
        .ok_or_else(|| anyhow!("{context} is missing required field '{key}'"))
}

fn required_string(obj: &Map<String, Value>, key: &str, context: &str) -> Result<String> {
    required_value(obj, key, context)?
        .as_str()
        .map(|value| value.to_string())
        .ok_or_else(|| anyhow!("{context}.{key} must be a string"))
}

fn optional_string(obj: &Map<String, Value>, key: &str, context: &str) -> Result<Option<String>> {
    match obj.get(key) {
        Some(Value::Null) | None => Ok(None),
        Some(value) => value
            .as_str()
            .map(|value| Some(value.to_string()))
            .ok_or_else(|| anyhow!("{context}.{key} must be a string or null")),
    }
}

fn required_u64(obj: &Map<String, Value>, key: &str, context: &str) -> Result<u64> {
    required_value(obj, key, context)?
        .as_u64()
        .ok_or_else(|| anyhow!("{context}.{key} must be an unsigned integer"))
}

fn optional_u64(obj: &Map<String, Value>, key: &str, context: &str) -> Result<Option<u64>> {
    match obj.get(key) {
        Some(Value::Null) | None => Ok(None),
        Some(value) => value
            .as_u64()
            .map(Some)
            .ok_or_else(|| anyhow!("{context}.{key} must be an unsigned integer or null")),
    }
}

fn optional_usize(obj: &Map<String, Value>, key: &str, context: &str) -> Result<Option<usize>> {
    match optional_u64(obj, key, context)? {
        Some(value) if value > usize::MAX as u64 => {
            bail!("{context}.{key} value {value} exceeds usize::MAX")
        }
        Some(value) => Ok(Some(value as usize)),
        None => Ok(None),
    }
}

fn required_array<'a>(
    obj: &'a Map<String, Value>,
    key: &str,
    context: &str,
) -> Result<&'a [Value]> {
    required_value(obj, key, context)?
        .as_array()
        .map(|values| values.as_slice())
        .ok_or_else(|| anyhow!("{context}.{key} must be an array"))
}

fn optional_string_array(
    obj: &Map<String, Value>,
    key: &str,
    context: &str,
) -> Result<Vec<String>> {
    match obj.get(key) {
        Some(Value::Null) | None => Ok(Vec::new()),
        Some(Value::Array(values)) => values
            .iter()
            .enumerate()
            .map(|(idx, value)| {
                value
                    .as_str()
                    .map(|value| value.to_string())
                    .ok_or_else(|| anyhow!("{context}.{key}[{idx}] must be a string"))
            })
            .collect::<Result<Vec<_>>>(),
        Some(_) => bail!("{context}.{key} must be an array or null"),
    }
}

fn write_batch_summary_plots(
    out_dir: &Path,
    config: &MonitorConfig,
    results: &[BatchSpillResult],
    options: &BatchOptions,
    has_reference: bool,
) -> Result<()> {
    write_tune_vs_spill_png(
        &out_dir.join("tune_vs_spill.png"),
        results,
        config.tune_plot_y_min,
        config.tune_plot_y_max,
    )?;
    if let Some(flash_count) = options.flash_count {
        write_tune_vs_spill_flash_plots(
            out_dir,
            results,
            config.tune_plot_y_min,
            config.tune_plot_y_max,
            flash_count,
        )?;
        write_tune_histogram_flash_plots(out_dir, results, flash_count)?;
    }
    write_confidence_vs_spill_png(
        &out_dir.join("confidence_vs_spill.png"),
        results,
        options.min_confidence,
    )?;
    write_alignment_vs_spill_png(&out_dir.join("alignment_vs_spill.png"), results)?;
    write_tune_scatter_png(
        &out_dir.join("tune_scatter_qx_qy.png"),
        results,
        config.tune_plot_y_min,
        config.tune_plot_y_max,
    )?;
    write_tune_histogram_png(&out_dir.join("tune_histogram.png"), results)?;
    if has_reference {
        write_tune_residuals_png(&out_dir.join("tune_residuals.png"), results)?;
    }
    Ok(())
}

fn write_composite_waterfall_plots(
    out_dir: &Path,
    config: &MonitorConfig,
    results: &[BatchSpillResult],
) -> Result<()> {
    write_composite_waterfall_png(
        &out_dir.join("composite_waterfall_h.png"),
        Plane::Horizontal,
        config,
        results,
    )?;
    write_composite_waterfall_png(
        &out_dir.join("composite_waterfall_v.png"),
        Plane::Vertical,
        config,
        results,
    )?;
    Ok(())
}

fn write_composite_waterfall_png(
    path: &Path,
    plane: Plane,
    config: &MonitorConfig,
    results: &[BatchSpillResult],
) -> Result<()> {
    let mut image = RgbImage::new(1600, 1000);
    image.fill([255, 255, 255]);

    let origin_x = 120.0f64;
    let origin_y = image.height as f64 - 120.0;
    let axis_x_len = 860.0f64; // spill index axis
    let axis_y_len = 520.0f64; // tune axis
    let axis_z_dx = 420.0f64; // projected time (Z) axis x-offset
    let axis_z_dy = 260.0f64; // projected time (Z) axis y-offset
    let axis_color = [0, 0, 0];
    let frame_color = [200, 200, 200];

    let project = |x_norm: f64, y_norm: f64, z_norm: f64| -> (i32, i32) {
        let x = origin_x + x_norm * axis_x_len + z_norm * axis_z_dx;
        let y = origin_y - y_norm * axis_y_len - z_norm * axis_z_dy;
        (x.round() as i32, y.round() as i32)
    };

    let p000 = project(0.0, 0.0, 0.0);
    let p100 = project(1.0, 0.0, 0.0);
    let p010 = project(0.0, 1.0, 0.0);
    let p001 = project(0.0, 0.0, 1.0);
    let p110 = project(1.0, 1.0, 0.0);
    let p101 = project(1.0, 0.0, 1.0);
    let p011 = project(0.0, 1.0, 1.0);
    let p111 = project(1.0, 1.0, 1.0);

    // 3D bounding frame.
    image.draw_line(p000.0, p000.1, p100.0, p100.1, axis_color);
    image.draw_line(p000.0, p000.1, p010.0, p010.1, axis_color);
    image.draw_line(p000.0, p000.1, p001.0, p001.1, axis_color);
    image.draw_line(p100.0, p100.1, p110.0, p110.1, frame_color);
    image.draw_line(p100.0, p100.1, p101.0, p101.1, frame_color);
    image.draw_line(p010.0, p010.1, p110.0, p110.1, frame_color);
    image.draw_line(p010.0, p010.1, p011.0, p011.1, frame_color);
    image.draw_line(p001.0, p001.1, p101.0, p101.1, frame_color);
    image.draw_line(p001.0, p001.1, p011.0, p011.1, frame_color);
    image.draw_line(p110.0, p110.1, p111.0, p111.1, frame_color);
    image.draw_line(p101.0, p101.1, p111.0, p111.1, frame_color);
    image.draw_line(p011.0, p011.1, p111.0, p111.1, frame_color);

    // Axes labels.
    draw_text_small(
        &mut image,
        p100.0 - 40,
        p100.1 + 18,
        "SPILL ORDER",
        [0, 0, 0],
        2,
    );
    draw_text_small(&mut image, p010.0 - 30, p010.1 - 18, "TUNE", [0, 0, 0], 2);
    draw_text_small(
        &mut image,
        p001.0 + 8,
        p001.1 - 10,
        time_axis_z_label(config),
        [0, 0, 0],
        2,
    );

    let z_max_axis = results
        .iter()
        .filter_map(|result| match plane {
            Plane::Horizontal => result.snapshot.h_analysis.as_ref(),
            Plane::Vertical => result.snapshot.v_analysis.as_ref(),
        })
        .flat_map(|analysis| {
            analysis
                .sliding
                .iter()
                .map(|point| time_axis_value_from_turn(point.center_turn, config))
        })
        .fold(1.0f64, f64::max);

    // Y-axis tune ticks.
    let y_min = config.tune_plot_y_min;
    let y_max = config.tune_plot_y_max;
    let y_span = (y_max - y_min).max(1e-12);
    let mut y_tick = (y_min / 0.02).ceil() * 0.02;
    while y_tick <= y_max + 1e-9 {
        let yn = ((y_tick - y_min) / y_span).clamp(0.0, 1.0);
        let a = project(0.0, yn, 0.0);
        let b = project(0.0, yn, 1.0);
        image.draw_line(a.0, a.1, b.0, b.1, [235, 235, 235]);
        let label = format!("{y_tick:.2}");
        draw_text_small(&mut image, a.0 - 42, a.1 - 6, &label, [0, 0, 0], 2);
        y_tick += 0.02;
    }

    // Z-axis ticks.
    for i in 0..=5 {
        let zn = i as f64 / 5.0;
        let p = project(0.0, 0.0, zn);
        let q = project(1.0, 0.0, zn);
        image.draw_line(p.0, p.1, q.0, q.1, [240, 240, 240]);
        let label = format_number_label(z_max_axis * zn);
        draw_text_small(&mut image, p.0 - 12, p.1 + 12, &label, [0, 0, 0], 2);
    }

    // X-axis ticks (spill sequence index).
    let spill_count = results.len().max(1);
    for i in 0..=5 {
        let xn = i as f64 / 5.0;
        let p = project(xn, 0.0, 0.0);
        let q = project(xn, 0.0, 1.0);
        image.draw_line(p.0, p.1, q.0, q.1, [240, 240, 240]);
        let spill_label = ((spill_count as f64 - 1.0) * xn + 1.0).round() as i64;
        draw_text_small(
            &mut image,
            p.0 - 6,
            p.1 + 18,
            &spill_label.to_string(),
            [0, 0, 0],
            2,
        );
    }

    let mut plotted_any = false;
    let x_den = (spill_count.saturating_sub(1)).max(1) as f64;
    for (spill_idx, result) in results.iter().enumerate() {
        let analysis = match plane {
            Plane::Horizontal => result.snapshot.h_analysis.as_ref(),
            Plane::Vertical => result.snapshot.v_analysis.as_ref(),
        };
        let Some(analysis) = analysis else {
            continue;
        };

        let points = analysis
            .sliding
            .iter()
            .filter_map(|point| {
                point
                    .selected_tune
                    .map(|tune| (time_axis_value_from_turn(point.center_turn, config), tune))
            })
            .filter(|(_, tune)| tune.is_finite())
            .collect::<Vec<_>>();
        if points.is_empty() {
            continue;
        }
        plotted_any = true;

        let spill_norm = spill_idx as f64 / x_den;
        let base_color = if plane == Plane::Horizontal {
            [0, 70, 220]
        } else {
            [220, 0, 0]
        };
        let color = scale_color(base_color, 0.45 + 0.55 * spill_norm);
        let bar_color = scale_color(color, 0.7);

        // Waterfall "histogram bars" per time sample.
        let step = (points.len() / 64).max(1);
        for (idx, (turn, tune)) in points.iter().enumerate() {
            if idx % step != 0 {
                continue;
            }
            let z_norm = (turn / z_max_axis).clamp(0.0, 1.0);
            let y_norm = ((tune - y_min) / y_span).clamp(0.0, 1.0);
            let floor = project(spill_norm, 0.0, z_norm);
            let top = project(spill_norm, y_norm, z_norm);
            image.draw_line(floor.0, floor.1, top.0, top.1, bar_color);
        }

        // Tune trajectory per spill in (Y,T) at fixed spill index.
        for window in points.windows(2) {
            let (t0, q0) = window[0];
            let (t1, q1) = window[1];
            let z0 = (t0 / z_max_axis).clamp(0.0, 1.0);
            let z1 = (t1 / z_max_axis).clamp(0.0, 1.0);
            let y0 = ((q0 - y_min) / y_span).clamp(0.0, 1.0);
            let y1 = ((q1 - y_min) / y_span).clamp(0.0, 1.0);
            let p0 = project(spill_norm, y0, z0);
            let p1 = project(spill_norm, y1, z1);
            image.draw_line(p0.0, p0.1, p1.0, p1.1, color);
        }
    }

    let title = match plane {
        Plane::Horizontal => "COMPOSITE WATERFALL H (TUNE VS TIME)",
        Plane::Vertical => "COMPOSITE WATERFALL V (TUNE VS TIME)",
    };
    draw_text_small(&mut image, 20, 18, title, [0, 0, 0], 3);
    if !plotted_any {
        draw_text_small(&mut image, 20, 54, "NO DATA", [140, 0, 0], 3);
    }

    write_png_rgb(path, &image)
}

fn write_tune_vs_spill_png(
    path: &Path,
    results: &[BatchSpillResult],
    tune_y_min: f64,
    tune_y_max: f64,
) -> Result<()> {
    let mut image = RgbImage::new(1280, 720);
    image.fill([255, 255, 255]);
    let bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 30,
        bottom: 70,
    };
    draw_axes(&mut image, bounds, [0, 0, 0]);

    let x_max = results.len().max(1) as f64;
    draw_xy_ticks(&mut image, bounds, 1.0, x_max, tune_y_min, tune_y_max);
    let qx_points = results
        .iter()
        .filter_map(|r| {
            r.record
                .qx_injection
                .map(|q| (r.record.spill_index as f64, q))
        })
        .collect::<Vec<_>>();
    let qy_points = results
        .iter()
        .filter_map(|r| {
            r.record
                .qy_injection
                .map(|q| (r.record.spill_index as f64, q))
        })
        .collect::<Vec<_>>();
    draw_polyline_xy(
        &mut image,
        bounds,
        &qx_points,
        1.0,
        x_max,
        tune_y_min,
        tune_y_max,
        [0, 70, 220],
    );
    draw_polyline_xy(
        &mut image,
        bounds,
        &qy_points,
        1.0,
        x_max,
        tune_y_min,
        tune_y_max,
        [220, 0, 0],
    );

    for result in results {
        if let Some(qx) = result.record.qx_injection {
            draw_point_xy(
                &mut image,
                bounds,
                result.record.spill_index as f64,
                qx,
                1.0,
                x_max,
                tune_y_min,
                tune_y_max,
                quality_color(result.record.quality_label),
            );
        }
        if let Some(qy) = result.record.qy_injection {
            draw_point_xy(
                &mut image,
                bounds,
                result.record.spill_index as f64,
                qy,
                1.0,
                x_max,
                tune_y_min,
                tune_y_max,
                quality_color(result.record.quality_label),
            );
        }
    }

    let legend_x = image.width as i32 - bounds.right as i32 - 230;
    draw_line_legend(
        &mut image,
        (legend_x, bounds.top as i32 + 8),
        &[
            ([0, 70, 220], "Qx"),
            ([220, 0, 0], "Qy"),
            ([0, 140, 0], "Quality Marker"),
        ],
    );
    draw_text_small(
        &mut image,
        bounds.left as i32 + 4,
        8,
        "TUNE VS SPILL (INJECTION)",
        [0, 0, 0],
        2,
    );

    write_png_rgb(path, &image)
}

fn resolved_flash_plot_count(results: &[BatchSpillResult], requested_flash_count: usize) -> usize {
    let available_max = results
        .iter()
        .flat_map(|result| {
            [
                result
                    .snapshot
                    .h_analysis
                    .as_ref()
                    .map(|analysis| analysis.sliding.len())
                    .unwrap_or(0),
                result
                    .snapshot
                    .v_analysis
                    .as_ref()
                    .map(|analysis| analysis.sliding.len())
                    .unwrap_or(0),
            ]
        })
        .max()
        .unwrap_or(0);
    requested_flash_count.min(available_max)
}

fn write_tune_vs_spill_flash_plots(
    out_dir: &Path,
    results: &[BatchSpillResult],
    tune_y_min: f64,
    tune_y_max: f64,
    requested_flash_count: usize,
) -> Result<()> {
    if requested_flash_count == 0 {
        return Ok(());
    }
    let flash_count = resolved_flash_plot_count(results, requested_flash_count);
    for flash_idx in 0..flash_count {
        let path = out_dir.join(format!("tune_vs_spill_flash_{:02}.png", flash_idx + 1));
        write_tune_vs_spill_flash_png(path.as_path(), results, tune_y_min, tune_y_max, flash_idx)?;
    }
    Ok(())
}

fn write_tune_histogram_flash_plots(
    out_dir: &Path,
    results: &[BatchSpillResult],
    requested_flash_count: usize,
) -> Result<()> {
    if requested_flash_count == 0 {
        return Ok(());
    }
    let flash_count = resolved_flash_plot_count(results, requested_flash_count);
    for flash_idx in 0..flash_count {
        let path = out_dir.join(format!("tune_histogram_flash_{:02}.png", flash_idx + 1));
        write_tune_histogram_flash_png(path.as_path(), results, flash_idx)?;
    }
    Ok(())
}

fn write_tune_vs_spill_flash_png(
    path: &Path,
    results: &[BatchSpillResult],
    tune_y_min: f64,
    tune_y_max: f64,
    flash_index: usize,
) -> Result<()> {
    let mut image = RgbImage::new(1280, 720);
    image.fill([255, 255, 255]);
    let bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 30,
        bottom: 70,
    };
    draw_axes(&mut image, bounds, [0, 0, 0]);

    let x_max = results.len().max(1) as f64;
    draw_xy_ticks(&mut image, bounds, 1.0, x_max, tune_y_min, tune_y_max);

    let mut qx_points = Vec::<(f64, f64)>::new();
    let mut qy_points = Vec::<(f64, f64)>::new();
    let mut center_turns = Vec::<usize>::new();

    for result in results {
        if let Some(point) = result
            .snapshot
            .h_analysis
            .as_ref()
            .and_then(|analysis| analysis.sliding.get(flash_index))
        {
            if let Some(tune) = point.selected_tune {
                qx_points.push((result.record.spill_index as f64, tune));
            }
            center_turns.push(point.center_turn);
        }
        if let Some(point) = result
            .snapshot
            .v_analysis
            .as_ref()
            .and_then(|analysis| analysis.sliding.get(flash_index))
        {
            if let Some(tune) = point.selected_tune {
                qy_points.push((result.record.spill_index as f64, tune));
            }
            center_turns.push(point.center_turn);
        }
    }

    draw_polyline_xy(
        &mut image,
        bounds,
        &qx_points,
        1.0,
        x_max,
        tune_y_min,
        tune_y_max,
        [0, 70, 220],
    );
    draw_polyline_xy(
        &mut image,
        bounds,
        &qy_points,
        1.0,
        x_max,
        tune_y_min,
        tune_y_max,
        [220, 0, 0],
    );

    for result in results {
        if let Some(qx) = result
            .snapshot
            .h_analysis
            .as_ref()
            .and_then(|analysis| analysis.sliding.get(flash_index))
            .and_then(|point| point.selected_tune)
        {
            draw_point_xy(
                &mut image,
                bounds,
                result.record.spill_index as f64,
                qx,
                1.0,
                x_max,
                tune_y_min,
                tune_y_max,
                quality_color(result.record.quality_label),
            );
        }
        if let Some(qy) = result
            .snapshot
            .v_analysis
            .as_ref()
            .and_then(|analysis| analysis.sliding.get(flash_index))
            .and_then(|point| point.selected_tune)
        {
            draw_point_xy(
                &mut image,
                bounds,
                result.record.spill_index as f64,
                qy,
                1.0,
                x_max,
                tune_y_min,
                tune_y_max,
                quality_color(result.record.quality_label),
            );
        }
    }

    let legend_x = image.width as i32 - bounds.right as i32 - 230;
    draw_line_legend(
        &mut image,
        (legend_x, bounds.top as i32 + 8),
        &[
            ([0, 70, 220], "Qx"),
            ([220, 0, 0], "Qy"),
            ([0, 140, 0], "Quality Marker"),
        ],
    );

    let center_turn = if center_turns.is_empty() {
        "NA".to_string()
    } else {
        let avg = center_turns.iter().sum::<usize>() / center_turns.len();
        avg.to_string()
    };
    draw_text_small(
        &mut image,
        bounds.left as i32 + 4,
        8,
        &format!(
            "TUNE VS SPILL FLASH {:02} (CENTER TURN ~{})",
            flash_index + 1,
            center_turn
        ),
        [0, 0, 0],
        2,
    );

    if qx_points.is_empty() && qy_points.is_empty() {
        let no_data_y = (image.height as i32 - bounds.bottom as i32) - 24;
        draw_text_small(
            &mut image,
            bounds.left as i32 + 4,
            no_data_y,
            "NO FLASH TUNE DATA",
            [140, 0, 0],
            2,
        );
    }

    write_png_rgb(path, &image)
}

fn write_confidence_vs_spill_png(
    path: &Path,
    results: &[BatchSpillResult],
    min_confidence: f64,
) -> Result<()> {
    let mut image = RgbImage::new(1280, 720);
    image.fill([255, 255, 255]);
    let bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 30,
        bottom: 70,
    };
    draw_axes(&mut image, bounds, [0, 0, 0]);

    let x_max = results.len().max(1) as f64;
    let conf_values = results
        .iter()
        .flat_map(|r| [r.record.confidence_h, r.record.confidence_v])
        .flatten()
        .filter(|v| v.is_finite())
        .collect::<Vec<_>>();
    let conf_max = conf_values
        .iter()
        .copied()
        .fold(min_confidence.max(1.0), f64::max)
        * 1.1;

    draw_xy_ticks(&mut image, bounds, 1.0, x_max, 0.0, conf_max);

    let h_points = results
        .iter()
        .filter_map(|r| {
            r.record
                .confidence_h
                .map(|v| (r.record.spill_index as f64, v))
        })
        .collect::<Vec<_>>();
    let v_points = results
        .iter()
        .filter_map(|r| {
            r.record
                .confidence_v
                .map(|v| (r.record.spill_index as f64, v))
        })
        .collect::<Vec<_>>();
    draw_polyline_xy(
        &mut image,
        bounds,
        &h_points,
        1.0,
        x_max,
        0.0,
        conf_max,
        [0, 70, 220],
    );
    draw_polyline_xy(
        &mut image,
        bounds,
        &v_points,
        1.0,
        x_max,
        0.0,
        conf_max,
        [220, 0, 0],
    );
    draw_horizontal_xy(
        &mut image,
        bounds,
        min_confidence,
        1.0,
        x_max,
        0.0,
        conf_max,
        [0, 140, 0],
    );

    let legend_x = image.width as i32 - bounds.right as i32 - 230;
    draw_line_legend(
        &mut image,
        (legend_x, bounds.top as i32 + 8),
        &[
            ([0, 70, 220], "Conf H"),
            ([220, 0, 0], "Conf V"),
            ([0, 140, 0], "Threshold"),
        ],
    );
    draw_text_small(
        &mut image,
        bounds.left as i32 + 4,
        8,
        "CONFIDENCE VS SPILL",
        [0, 0, 0],
        2,
    );

    write_png_rgb(path, &image)
}

fn write_alignment_vs_spill_png(path: &Path, results: &[BatchSpillResult]) -> Result<()> {
    let mut image = RgbImage::new(1280, 900);
    image.fill([255, 255, 255]);
    let top_bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 40,
        bottom: 490,
    };
    let bottom_bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 520,
        bottom: 70,
    };
    draw_axes(&mut image, top_bounds, [0, 0, 0]);
    draw_axes(&mut image, bottom_bounds, [0, 0, 0]);

    let x_max = results.len().max(1) as f64;
    draw_xy_ticks(&mut image, top_bounds, 1.0, x_max, 0.0, 1.0);

    let max_count = results
        .iter()
        .map(|r| r.record.requested_streams.max(r.record.used_streams_total))
        .max()
        .unwrap_or(1) as f64;
    draw_xy_ticks(
        &mut image,
        bottom_bounds,
        1.0,
        x_max,
        0.0,
        max_count.max(1.0),
    );

    let aligned_fraction = results
        .iter()
        .map(|r| (r.record.spill_index as f64, r.record.aligned_fraction))
        .collect::<Vec<_>>();
    draw_polyline_xy(
        &mut image,
        top_bounds,
        &aligned_fraction,
        1.0,
        x_max,
        0.0,
        1.0,
        [0, 140, 0],
    );

    let used = results
        .iter()
        .map(|r| {
            (
                r.record.spill_index as f64,
                r.record.used_streams_total as f64,
            )
        })
        .collect::<Vec<_>>();
    let requested = results
        .iter()
        .map(|r| {
            (
                r.record.spill_index as f64,
                r.record.requested_streams as f64,
            )
        })
        .collect::<Vec<_>>();
    draw_polyline_xy(
        &mut image,
        bottom_bounds,
        &used,
        1.0,
        x_max,
        0.0,
        max_count.max(1.0),
        [0, 70, 220],
    );
    draw_polyline_xy(
        &mut image,
        bottom_bounds,
        &requested,
        1.0,
        x_max,
        0.0,
        max_count.max(1.0),
        [120, 120, 120],
    );

    let legend_x = image.width as i32 - top_bounds.right as i32 - 260;
    draw_line_legend(
        &mut image,
        (legend_x, top_bounds.top as i32 + 8),
        &[
            ([0, 140, 0], "Aligned Fraction"),
            ([0, 70, 220], "Used Streams"),
            ([120, 120, 120], "Requested Streams"),
        ],
    );
    draw_text_small(
        &mut image,
        top_bounds.left as i32 + 4,
        10,
        "ALIGNMENT VS SPILL",
        [0, 0, 0],
        2,
    );

    write_png_rgb(path, &image)
}

fn write_tune_scatter_png(
    path: &Path,
    results: &[BatchSpillResult],
    tune_y_min: f64,
    tune_y_max: f64,
) -> Result<()> {
    let mut image = RgbImage::new(900, 900);
    image.fill([255, 255, 255]);
    let bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 30,
        bottom: 70,
    };
    draw_axes(&mut image, bounds, [0, 0, 0]);

    let points = results
        .iter()
        .filter_map(|r| match (r.record.qx_injection, r.record.qy_injection) {
            (Some(qx), Some(qy)) => Some((qx, qy, r.record.quality_label)),
            _ => None,
        })
        .collect::<Vec<_>>();

    let mut qx_vals = points.iter().map(|(qx, _, _)| *qx).collect::<Vec<_>>();
    if qx_vals.is_empty() {
        qx_vals.extend([0.0, 1.0]);
    }
    let x_min = qx_vals.iter().copied().fold(f64::INFINITY, f64::min);
    let x_max = qx_vals.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let x_pad = ((x_max - x_min) * 0.1).max(0.01);
    let x0 = (x_min - x_pad).clamp(0.0, 1.0);
    let x1 = (x_max + x_pad).clamp(0.0, 1.0);
    let y0 = tune_y_min;
    let y1 = tune_y_max;

    draw_xy_ticks(&mut image, bounds, x0, x1, y0, y1);
    for (qx, qy, quality) in points {
        draw_point_xy(
            &mut image,
            bounds,
            qx,
            qy,
            x0,
            x1,
            y0,
            y1,
            quality_color(quality),
        );
    }

    draw_text_small(
        &mut image,
        bounds.left as i32 + 4,
        8,
        "TUNE SCATTER QX/QY",
        [0, 0, 0],
        2,
    );

    write_png_rgb(path, &image)
}

fn write_tune_histogram_png(path: &Path, results: &[BatchSpillResult]) -> Result<()> {
    let good = results
        .iter()
        .filter(|r| r.record.quality_label == SpillQuality::Good)
        .collect::<Vec<_>>();
    let qx = good
        .iter()
        .filter_map(|r| r.record.qx_injection)
        .collect::<Vec<_>>();
    let qy = good
        .iter()
        .filter_map(|r| r.record.qy_injection)
        .collect::<Vec<_>>();

    let mut image = RgbImage::new(1280, 900);
    image.fill([255, 255, 255]);
    let top_bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 40,
        bottom: 490,
    };
    let bottom_bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 520,
        bottom: 70,
    };
    draw_axes(&mut image, top_bounds, [0, 0, 0]);
    draw_axes(&mut image, bottom_bounds, [0, 0, 0]);

    draw_histogram_panel(&mut image, top_bounds, &qx, [0, 70, 220], "QX HIST (GOOD)");
    draw_histogram_panel(
        &mut image,
        bottom_bounds,
        &qy,
        [220, 0, 0],
        "QY HIST (GOOD)",
    );
    draw_text_small(
        &mut image,
        top_bounds.left as i32 + 4,
        10,
        "TUNE HISTOGRAM",
        [0, 0, 0],
        2,
    );

    write_png_rgb(path, &image)
}

fn write_tune_histogram_flash_png(
    path: &Path,
    results: &[BatchSpillResult],
    flash_index: usize,
) -> Result<()> {
    let good = results
        .iter()
        .filter(|r| r.record.quality_label == SpillQuality::Good);
    let mut qx = Vec::<f64>::new();
    let mut qy = Vec::<f64>::new();
    let mut center_turns = Vec::<usize>::new();

    for result in good {
        if let Some(point) = result
            .snapshot
            .h_analysis
            .as_ref()
            .and_then(|analysis| analysis.sliding.get(flash_index))
        {
            if let Some(tune) = point.selected_tune.filter(|value| value.is_finite()) {
                qx.push(tune);
            }
            center_turns.push(point.center_turn);
        }
        if let Some(point) = result
            .snapshot
            .v_analysis
            .as_ref()
            .and_then(|analysis| analysis.sliding.get(flash_index))
        {
            if let Some(tune) = point.selected_tune.filter(|value| value.is_finite()) {
                qy.push(tune);
            }
            center_turns.push(point.center_turn);
        }
    }

    let mut image = RgbImage::new(1280, 900);
    image.fill([255, 255, 255]);
    let top_bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 40,
        bottom: 490,
    };
    let bottom_bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 520,
        bottom: 70,
    };
    draw_axes(&mut image, top_bounds, [0, 0, 0]);
    draw_axes(&mut image, bottom_bounds, [0, 0, 0]);

    let title_qx = format!("QX HIST FLASH {:02} (GOOD)", flash_index + 1);
    let title_qy = format!("QY HIST FLASH {:02} (GOOD)", flash_index + 1);
    draw_histogram_panel(&mut image, top_bounds, &qx, [0, 70, 220], &title_qx);
    draw_histogram_panel(&mut image, bottom_bounds, &qy, [220, 0, 0], &title_qy);

    let center_turn = if center_turns.is_empty() {
        "NA".to_string()
    } else {
        let avg = center_turns.iter().sum::<usize>() / center_turns.len();
        avg.to_string()
    };
    let title = format!(
        "TUNE HISTOGRAM FLASH {:02} (CENTER TURN ~{})",
        flash_index + 1,
        center_turn
    );
    draw_text_small(
        &mut image,
        top_bounds.left as i32 + 4,
        10,
        &title,
        [0, 0, 0],
        2,
    );

    write_png_rgb(path, &image)
}

fn write_tune_residuals_png(path: &Path, results: &[BatchSpillResult]) -> Result<()> {
    let mut image = RgbImage::new(1280, 720);
    image.fill([255, 255, 255]);
    let bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 30,
        bottom: 70,
    };
    draw_axes(&mut image, bounds, [0, 0, 0]);

    let x_max = results.len().max(1) as f64;
    let values = results
        .iter()
        .flat_map(|r| [r.record.residual_qx, r.record.residual_qy])
        .flatten()
        .collect::<Vec<_>>();
    let y_abs = values.iter().copied().map(f64::abs).fold(0.01f64, f64::max) * 1.1;
    draw_xy_ticks(&mut image, bounds, 1.0, x_max, -y_abs, y_abs);

    let qx_points = results
        .iter()
        .filter_map(|r| {
            r.record
                .residual_qx
                .map(|v| (r.record.spill_index as f64, v))
        })
        .collect::<Vec<_>>();
    let qy_points = results
        .iter()
        .filter_map(|r| {
            r.record
                .residual_qy
                .map(|v| (r.record.spill_index as f64, v))
        })
        .collect::<Vec<_>>();
    draw_polyline_xy(
        &mut image,
        bounds,
        &qx_points,
        1.0,
        x_max,
        -y_abs,
        y_abs,
        [0, 70, 220],
    );
    draw_polyline_xy(
        &mut image,
        bounds,
        &qy_points,
        1.0,
        x_max,
        -y_abs,
        y_abs,
        [220, 0, 0],
    );
    draw_horizontal_xy(
        &mut image,
        bounds,
        0.0,
        1.0,
        x_max,
        -y_abs,
        y_abs,
        [0, 140, 0],
    );
    draw_text_small(
        &mut image,
        bounds.left as i32 + 4,
        8,
        "TUNE RESIDUALS",
        [0, 0, 0],
        2,
    );

    write_png_rgb(path, &image)
}

fn draw_histogram_panel(
    image: &mut RgbImage,
    bounds: PlotBounds,
    values: &[f64],
    color: [u8; 3],
    title: &str,
) {
    if values.is_empty() {
        draw_text_small(
            image,
            bounds.left as i32 + 8,
            bounds.top as i32 + 8,
            "NO DATA",
            [120, 0, 0],
            2,
        );
        return;
    }

    let bins = 20usize;
    let min_v = values.iter().copied().fold(f64::INFINITY, f64::min);
    let max_v = values.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let range = (max_v - min_v).max(1e-9);
    let mut counts = vec![0usize; bins];
    for value in values {
        let mut idx = (((*value - min_v) / range) * bins as f64).floor() as isize;
        if idx < 0 {
            idx = 0;
        }
        if idx as usize >= bins {
            idx = bins as isize - 1;
        }
        counts[idx as usize] += 1;
    }
    let max_count = counts.iter().copied().max().unwrap_or(1) as f64;
    draw_xy_ticks(image, bounds, min_v, max_v, 0.0, max_count);
    draw_text_small(
        image,
        bounds.left as i32 + 4,
        bounds.top as i32 + 8,
        title,
        [0, 0, 0],
        2,
    );

    for (idx, count) in counts.iter().enumerate() {
        let x0 = min_v + (idx as f64 / bins as f64) * range;
        let x1 = min_v + ((idx + 1) as f64 / bins as f64) * range;
        let y = *count as f64;
        draw_vertical_bar_xy(
            image, bounds, x0, x1, y, min_v, max_v, 0.0, max_count, color,
        );
    }
}

fn draw_vertical_bar_xy(
    image: &mut RgbImage,
    bounds: PlotBounds,
    x0: f64,
    x1: f64,
    y: f64,
    x_min: f64,
    x_max: f64,
    y_min: f64,
    y_max: f64,
    color: [u8; 3],
) {
    let left = ((x0 - x_min) / (x_max - x_min).max(1e-12)).clamp(0.0, 1.0);
    let right = ((x1 - x_min) / (x_max - x_min).max(1e-12)).clamp(0.0, 1.0);
    let height = ((y - y_min) / (y_max - y_min).max(1e-12)).clamp(0.0, 1.0);
    let (x0_px, y_base) = map_point(image, bounds, (left, 0.0));
    let (x1_px, y_top) = map_point(image, bounds, (right, height));
    let x_start = x0_px.min(x1_px);
    let x_end = x0_px.max(x1_px);
    let y_start = y_top.min(y_base);
    let y_end = y_top.max(y_base);

    for x in x_start..=x_end {
        for yy in y_start..=y_end {
            image.set_pixel(x, yy, color);
        }
    }
}

fn draw_point_xy(
    image: &mut RgbImage,
    bounds: PlotBounds,
    x: f64,
    y: f64,
    x_min: f64,
    x_max: f64,
    y_min: f64,
    y_max: f64,
    color: [u8; 3],
) {
    let xn = ((x - x_min) / (x_max - x_min).max(1e-12)).clamp(0.0, 1.0);
    let yn = ((y - y_min) / (y_max - y_min).max(1e-12)).clamp(0.0, 1.0);
    let (x_px, y_px) = map_point(image, bounds, (xn, yn));
    for dx in -2..=2 {
        for dy in -2..=2 {
            image.set_pixel(x_px + dx, y_px + dy, color);
        }
    }
}

fn draw_horizontal_xy(
    image: &mut RgbImage,
    bounds: PlotBounds,
    y: f64,
    x_min: f64,
    x_max: f64,
    y_min: f64,
    y_max: f64,
    color: [u8; 3],
) {
    let yn = ((y - y_min) / (y_max - y_min).max(1e-12)).clamp(0.0, 1.0);
    let y_px = (image.height as i32 - bounds.bottom as i32)
        - ((image.height - bounds.top - bounds.bottom) as f64 * yn) as i32;
    let x0 = bounds.left as i32;
    let x1 = (image.width - bounds.right) as i32;
    let _ = (x_min, x_max); // unused by design; caller provides axis context.
    image.draw_line(x0, y_px, x1, y_px, color);
}

fn quality_color(quality: SpillQuality) -> [u8; 3] {
    match quality {
        SpillQuality::Good => [0, 150, 0],
        SpillQuality::Marginal => [220, 140, 0],
        SpillQuality::Bad => [220, 0, 0],
    }
}

fn scale_color(color: [u8; 3], factor: f64) -> [u8; 3] {
    let factor = factor.clamp(0.0, 1.0);
    [
        (color[0] as f64 * factor).round() as u8,
        (color[1] as f64 * factor).round() as u8,
        (color[2] as f64 * factor).round() as u8,
    ]
}

fn heatmap_color(norm: f64) -> [u8; 3] {
    let t = norm.clamp(0.0, 1.0);
    let stops = [
        (0.0, [20u8, 35u8, 90u8]),
        (0.35, [30u8, 180u8, 230u8]),
        (0.70, [110u8, 220u8, 120u8]),
        (1.0, [255u8, 220u8, 40u8]),
    ];
    for idx in 0..(stops.len() - 1) {
        let (t0, c0) = stops[idx];
        let (t1, c1) = stops[idx + 1];
        if t >= t0 && t <= t1 {
            let local = if t1 > t0 { (t - t0) / (t1 - t0) } else { 0.0 };
            return [
                (c0[0] as f64 + (c1[0] as f64 - c0[0] as f64) * local).round() as u8,
                (c0[1] as f64 + (c1[1] as f64 - c0[1] as f64) * local).round() as u8,
                (c0[2] as f64 + (c1[2] as f64 - c0[2] as f64) * local).round() as u8,
            ];
        }
    }
    stops[stops.len() - 1].1
}

fn validate_study_options(options: &StudyOptions) -> Result<()> {
    if options.window_start_step == 0 {
        bail!("window_start_step must be >= 1");
    }
    if options.window_length_step == 0 {
        bail!("window_length_step must be >= 1");
    }
    if options.window_start_max < options.window_start_min {
        bail!("window_start_max must be >= window_start_min");
    }
    if options.window_length_max < options.window_length_min {
        bail!("window_length_max must be >= window_length_min");
    }
    if options.svd_modes == 0 {
        bail!("svd_modes must be >= 1");
    }
    if options.summary_file.trim().is_empty() {
        bail!("summary_file cannot be empty");
    }
    Ok(())
}

fn prepare_plane_data(
    plane: Plane,
    traces: Vec<StreamTrace>,
    required_turns: usize,
    warnings: &mut Vec<String>,
) -> Option<PreparedPlaneData> {
    if traces.is_empty() {
        return None;
    }

    let traces_total = traces.len();
    let consensus_turns = consensus_length(&traces)?;
    if consensus_turns < required_turns {
        warnings.push(format!(
            "plane {} consensus turns {} < required {} for requested studies",
            plane.label(),
            consensus_turns,
            required_turns
        ));
    }

    let traces = traces
        .into_iter()
        .filter(|trace| trace.samples.len() == consensus_turns)
        .collect::<Vec<_>>();

    if traces.is_empty() {
        warnings.push(format!(
            "plane {} had no traces left after consensus filtering",
            plane.label()
        ));
        return None;
    }

    Some(PreparedPlaneData {
        plane,
        traces_total,
        traces_used: traces.len(),
        consensus_turns,
        traces,
    })
}

fn build_sweep_values(min: usize, max: usize, step: usize) -> Vec<usize> {
    if step == 0 || max < min {
        return Vec::new();
    }
    let mut out = Vec::new();
    let mut value = min;
    while value <= max {
        out.push(value);
        match value.checked_add(step) {
            Some(next) => value = next,
            None => break,
        }
    }
    if out.last().copied() != Some(max) {
        out.push(max);
    }
    out.sort_unstable();
    out.dedup();
    out
}

fn compute_window_start_sweep(
    plane: &PreparedPlaneData,
    config: &MonitorConfig,
    starts: &[usize],
    fixed_length: usize,
) -> Result<Vec<SweepPoint>> {
    let mut out = Vec::new();
    for &start in starts {
        let point = if start + fixed_length > plane.consensus_turns {
            SweepPoint {
                x: start,
                tune: None,
                confidence: None,
            }
        } else {
            let spectrum = average_spectrum(&plane.traces, start, fixed_length)?;
            let peak = pick_peak_in_band(
                &spectrum,
                plane.plane.tune_band(config),
                config.min_peak_confidence,
            );
            SweepPoint {
                x: start,
                tune: peak.as_ref().map(|p| p.tune),
                confidence: peak.as_ref().map(|p| p.confidence),
            }
        };
        out.push(point);
    }
    Ok(out)
}

fn compute_window_length_sweep(
    plane: &PreparedPlaneData,
    config: &MonitorConfig,
    fixed_start: usize,
    lengths: &[usize],
) -> Result<Vec<SweepPoint>> {
    let mut out = Vec::new();
    for &length in lengths {
        let point = if fixed_start + length > plane.consensus_turns || length == 0 {
            SweepPoint {
                x: length,
                tune: None,
                confidence: None,
            }
        } else {
            let spectrum = average_spectrum(&plane.traces, fixed_start, length)?;
            let peak = pick_peak_in_band(
                &spectrum,
                plane.plane.tune_band(config),
                config.min_peak_confidence,
            );
            SweepPoint {
                x: length,
                tune: peak.as_ref().map(|p| p.tune),
                confidence: peak.as_ref().map(|p| p.confidence),
            }
        };
        out.push(point);
    }
    Ok(out)
}

fn trace_window_signal(trace: &StreamTrace, start: usize, window_turns: usize) -> Option<Vec<f64>> {
    if window_turns == 0 || start + window_turns > trace.samples.len() {
        return None;
    }
    let window = &trace.samples[start..start + window_turns];
    let mean = window.iter().sum::<f64>() / window_turns as f64;
    let hann = hann_window(window_turns);
    let mut signal = Vec::with_capacity(window_turns);
    for (idx, value) in window.iter().enumerate() {
        signal.push((value - mean) * hann[idx]);
    }
    Some(signal)
}

fn trace_window_rms(trace: &StreamTrace, start: usize, window_turns: usize) -> Option<f64> {
    if window_turns == 0 || start + window_turns > trace.samples.len() {
        return None;
    }
    let window = &trace.samples[start..start + window_turns];
    let mean = window.iter().sum::<f64>() / window_turns as f64;
    let var = window
        .iter()
        .map(|v| {
            let d = *v - mean;
            d * d
        })
        .sum::<f64>()
        / window_turns as f64;
    Some(var.sqrt())
}

fn compute_trace_spectrum(
    trace: &StreamTrace,
    start: usize,
    window_turns: usize,
) -> Option<Vec<f64>> {
    trace_window_signal(trace, start, window_turns).map(|signal| spectrum_power(&signal))
}

fn average_weighted_spectra(
    spectra: &[(String, Vec<f64>)],
    weights: &HashMap<String, f64>,
) -> Option<Vec<f64>> {
    let n = spectra.first()?.1.len();
    if n == 0 {
        return None;
    }

    let mut accum = vec![0.0f64; n];
    let mut sum_w = 0.0f64;

    for (key, spectrum) in spectra {
        if spectrum.len() != n {
            continue;
        }
        let w = *weights.get(key).unwrap_or(&0.0);
        if !w.is_finite() || w <= 0.0 {
            continue;
        }
        for (idx, power) in spectrum.iter().enumerate() {
            accum[idx] += *power * w;
        }
        sum_w += w;
    }

    if sum_w <= 0.0 {
        return None;
    }
    for value in &mut accum {
        *value /= sum_w;
    }
    Some(accum)
}

fn compute_bpm_metrics(
    plane: &PreparedPlaneData,
    config: &MonitorConfig,
    reference_start: usize,
    reference_length: usize,
) -> Result<Vec<BpmMetric>> {
    let mut metrics = Vec::<BpmMetric>::new();

    for trace in &plane.traces {
        let rms = trace_window_rms(trace, reference_start, reference_length).unwrap_or(0.0);
        let spectrum = compute_trace_spectrum(trace, reference_start, reference_length);
        let peak = spectrum.as_ref().and_then(|s| {
            pick_peak_in_band(s, plane.plane.tune_band(config), config.min_peak_confidence)
        });

        metrics.push(BpmMetric {
            plane: plane.plane,
            bpm_ip: trace.bpm_ip.clone(),
            stream_key: trace.stream_key.clone(),
            rms,
            tune: peak.as_ref().map(|p| p.tune),
            confidence: peak.as_ref().map(|p| p.confidence),
            peak_power: peak.as_ref().map(|p| p.peak_power),
            prominence: peak.as_ref().map(|p| p.prominence),
            noise_floor: peak.as_ref().map(|p| p.median_power),
            score: 0.0,
            flags: Vec::new(),
        });
    }

    let rms_values = metrics
        .iter()
        .map(|m| m.rms)
        .filter(|v| v.is_finite())
        .collect::<Vec<_>>();
    let rms_ref = median(&rms_values).unwrap_or(1.0).max(1e-12);

    for metric in &mut metrics {
        let confidence = metric.confidence.unwrap_or(0.0).max(0.0);
        metric.score = confidence * (metric.rms / rms_ref);

        if metric.rms < 0.2 * rms_ref {
            metric.flags.push("WEAK_RMS".to_string());
        }
        if confidence < DEFAULT_METHOD_WEAK_CONFIDENCE {
            metric.flags.push("LOW_CONF".to_string());
        }
        if metric.tune.is_none() {
            metric.flags.push("NO_PEAK".to_string());
        }
        if metric.flags.is_empty() {
            metric.flags.push("OK".to_string());
        }
    }

    metrics.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(Ordering::Equal)
            .then_with(|| a.stream_key.cmp(&b.stream_key))
    });

    Ok(metrics)
}

fn write_bpm_quality_csv(path: &Path, metrics: &[BpmMetric]) -> Result<()> {
    let mut grouped = HashMap::<Plane, Vec<&BpmMetric>>::new();
    for metric in metrics {
        grouped.entry(metric.plane).or_default().push(metric);
    }

    let mut rows = Vec::<String>::new();
    rows.push(
        "plane,rank,bpm_ip,stream_key,rms,tune,confidence,peak_power,prominence,noise_floor,score,flags"
            .to_string(),
    );

    for plane in [Plane::Horizontal, Plane::Vertical] {
        let mut plane_rows = grouped.remove(&plane).unwrap_or_default();
        plane_rows.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(Ordering::Equal)
                .then_with(|| a.stream_key.cmp(&b.stream_key))
        });
        for (idx, metric) in plane_rows.iter().enumerate() {
            rows.push(format!(
                "{},{},{},{},{:.6},{},{},{},{},{},{:.6},{}",
                plane.label(),
                idx + 1,
                metric.bpm_ip,
                metric.stream_key,
                metric.rms,
                opt_fmt(metric.tune),
                opt_fmt(metric.confidence),
                opt_fmt(metric.peak_power),
                opt_fmt(metric.prominence),
                opt_fmt(metric.noise_floor),
                metric.score,
                metric.flags.join("|")
            ));
        }
    }

    fs::write(path, rows.join("\n") + "\n")
        .with_context(|| format!("failed to write {}", path.display()))
}

fn write_bpm_tune_confidence_plots(
    tune_path: &Path,
    confidence_path: &Path,
    metrics: &[BpmMetric],
    tune_y_min: f64,
    tune_y_max: f64,
) -> Result<()> {
    write_metric_by_bpm_png(
        tune_path,
        metrics,
        |metric| metric.tune,
        "TUNE BY BPM",
        Some((tune_y_min, tune_y_max)),
    )?;
    write_metric_by_bpm_png(
        confidence_path,
        metrics,
        |metric| metric.confidence,
        "CONF BY BPM",
        None,
    )?;
    Ok(())
}

fn write_metric_by_bpm_png(
    path: &Path,
    metrics: &[BpmMetric],
    value_fn: impl Fn(&BpmMetric) -> Option<f64>,
    title: &str,
    fixed_y_range: Option<(f64, f64)>,
) -> Result<()> {
    let mut image = RgbImage::new(1280, 720);
    image.fill([255, 255, 255]);

    let bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 30,
        bottom: 70,
    };
    draw_axes(&mut image, bounds, [0, 0, 0]);

    let mut h_rows = metrics
        .iter()
        .filter(|m| m.plane == Plane::Horizontal)
        .collect::<Vec<_>>();
    h_rows.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(Ordering::Equal)
            .then_with(|| a.stream_key.cmp(&b.stream_key))
    });

    let mut v_rows = metrics
        .iter()
        .filter(|m| m.plane == Plane::Vertical)
        .collect::<Vec<_>>();
    v_rows.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(Ordering::Equal)
            .then_with(|| a.stream_key.cmp(&b.stream_key))
    });

    let h_points = h_rows
        .iter()
        .enumerate()
        .filter_map(|(idx, metric)| value_fn(metric).map(|v| (idx as f64 + 1.0, v)))
        .collect::<Vec<_>>();
    let v_points = v_rows
        .iter()
        .enumerate()
        .filter_map(|(idx, metric)| value_fn(metric).map(|v| (idx as f64 + 1.0, v)))
        .collect::<Vec<_>>();

    let x_max = h_rows.len().max(v_rows.len()).max(1) as f64;
    let mut values = h_points
        .iter()
        .chain(v_points.iter())
        .map(|(_, y)| *y)
        .filter(|v| v.is_finite())
        .collect::<Vec<_>>();
    if values.is_empty() {
        values.push(0.0);
        values.push(1.0);
    }
    let (y0, y1) = if let Some((min, max)) = fixed_y_range {
        (min, max)
    } else {
        let y_min = values
            .iter()
            .copied()
            .fold(f64::INFINITY, f64::min)
            .min(0.0);
        let y_max = values
            .iter()
            .copied()
            .fold(f64::NEG_INFINITY, f64::max)
            .max(1.0);
        let pad = ((y_max - y_min) * 0.1).max(0.01);
        (
            (y_min - pad).clamp(-1.0e9, 1.0e9),
            (y_max + pad).clamp(-1.0e9, 1.0e9),
        )
    };

    draw_xy_ticks(&mut image, bounds, 1.0, x_max, y0, y1);
    draw_polyline_xy(
        &mut image,
        bounds,
        &h_points,
        1.0,
        x_max,
        y0,
        y1,
        [0, 70, 220],
    );
    draw_polyline_xy(
        &mut image,
        bounds,
        &v_points,
        1.0,
        x_max,
        y0,
        y1,
        [220, 0, 0],
    );
    let legend_x = image.width as i32 - bounds.right as i32 - 180;
    draw_line_legend(
        &mut image,
        (legend_x, bounds.top as i32 + 8),
        &[([0, 70, 220], "H"), ([220, 0, 0], "V")],
    );
    draw_text_small(&mut image, bounds.left as i32 + 4, 8, title, [0, 0, 0], 2);

    write_png_rgb(path, &image)
}

fn compute_method_results(
    plane: &PreparedPlaneData,
    config: &MonitorConfig,
    all_metrics: &[BpmMetric],
    reference_start: usize,
    reference_length: usize,
    start_values: &[usize],
    length_values: &[usize],
) -> Result<Vec<MethodResult>> {
    const METHODS: [&str; 3] = ["BEST_SINGLE", "UNWEIGHTED", "WEIGHTED"];
    let plane_metrics = all_metrics
        .iter()
        .filter(|m| m.plane == plane.plane)
        .collect::<Vec<_>>();

    let best_stream_key = plane_metrics
        .iter()
        .max_by(|a, b| {
            a.score
                .partial_cmp(&b.score)
                .unwrap_or(Ordering::Equal)
                .then_with(|| a.stream_key.cmp(&b.stream_key))
        })
        .map(|m| m.stream_key.clone());

    let mut weights = HashMap::<String, f64>::new();
    for metric in plane_metrics {
        weights.insert(metric.stream_key.clone(), metric.score.max(0.0));
    }

    let mut results = Vec::<MethodResult>::new();
    for method in METHODS {
        let ref_peak = method_peak_for_window(
            plane,
            method,
            reference_start,
            reference_length,
            config,
            best_stream_key.as_deref(),
            &weights,
        );

        let start_tunes = start_values
            .iter()
            .filter_map(|start| {
                method_peak_for_window(
                    plane,
                    method,
                    *start,
                    reference_length,
                    config,
                    best_stream_key.as_deref(),
                    &weights,
                )
                .map(|p| p.tune)
            })
            .collect::<Vec<_>>();
        let length_tunes = length_values
            .iter()
            .filter_map(|length| {
                method_peak_for_window(
                    plane,
                    method,
                    reference_start,
                    *length,
                    config,
                    best_stream_key.as_deref(),
                    &weights,
                )
                .map(|p| p.tune)
            })
            .collect::<Vec<_>>();

        results.push(MethodResult {
            plane: plane.plane,
            method,
            tune: ref_peak.as_ref().map(|p| p.tune),
            confidence: ref_peak.as_ref().map(|p| p.confidence),
            start_tune_std: stddev(&start_tunes),
            length_tune_std: stddev(&length_tunes),
        });
    }

    Ok(results)
}

fn method_peak_for_window(
    plane: &PreparedPlaneData,
    method: &str,
    start: usize,
    length: usize,
    config: &MonitorConfig,
    best_stream_key: Option<&str>,
    weights: &HashMap<String, f64>,
) -> Option<PeakResult> {
    if start + length > plane.consensus_turns || length < 8 {
        return None;
    }

    let band = plane.plane.tune_band(config);
    match method {
        "BEST_SINGLE" => {
            let key = best_stream_key?;
            let trace = plane.traces.iter().find(|t| t.stream_key == key)?;
            let spectrum = compute_trace_spectrum(trace, start, length)?;
            pick_peak_in_band(&spectrum, band, config.min_peak_confidence)
        }
        "UNWEIGHTED" => {
            let spectrum = average_spectrum(&plane.traces, start, length).ok()?;
            pick_peak_in_band(&spectrum, band, config.min_peak_confidence)
        }
        "WEIGHTED" => {
            let mut spectra = Vec::<(String, Vec<f64>)>::new();
            for trace in &plane.traces {
                if let Some(spectrum) = compute_trace_spectrum(trace, start, length) {
                    spectra.push((trace.stream_key.clone(), spectrum));
                }
            }
            let spectrum = average_weighted_spectra(&spectra, weights)?;
            pick_peak_in_band(&spectrum, band, config.min_peak_confidence)
        }
        _ => None,
    }
}

fn write_method_comparison_png(
    path: &Path,
    methods: &[MethodResult],
    tune_y_min: f64,
    tune_y_max: f64,
) -> Result<()> {
    let mut image = RgbImage::new(1280, 900);
    image.fill([255, 255, 255]);

    let tune_bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 40,
        bottom: 490,
    };
    let conf_bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 520,
        bottom: 70,
    };

    draw_axes(&mut image, tune_bounds, [0, 0, 0]);
    draw_axes(&mut image, conf_bounds, [0, 0, 0]);

    let method_order = ["BEST_SINGLE", "UNWEIGHTED", "WEIGHTED"];
    let h_rows = method_order
        .iter()
        .filter_map(|name| {
            methods
                .iter()
                .find(|m| m.plane == Plane::Horizontal && m.method == *name)
        })
        .collect::<Vec<_>>();
    let v_rows = method_order
        .iter()
        .filter_map(|name| {
            methods
                .iter()
                .find(|m| m.plane == Plane::Vertical && m.method == *name)
        })
        .collect::<Vec<_>>();

    let h_tune = h_rows
        .iter()
        .enumerate()
        .filter_map(|(idx, row)| row.tune.map(|v| (idx as f64 + 1.0, v)))
        .collect::<Vec<_>>();
    let v_tune = v_rows
        .iter()
        .enumerate()
        .filter_map(|(idx, row)| row.tune.map(|v| (idx as f64 + 1.0, v)))
        .collect::<Vec<_>>();

    let h_conf = h_rows
        .iter()
        .enumerate()
        .filter_map(|(idx, row)| row.confidence.map(|v| (idx as f64 + 1.0, v)))
        .collect::<Vec<_>>();
    let v_conf = v_rows
        .iter()
        .enumerate()
        .filter_map(|(idx, row)| row.confidence.map(|v| (idx as f64 + 1.0, v)))
        .collect::<Vec<_>>();

    draw_xy_ticks(&mut image, tune_bounds, 1.0, 3.0, tune_y_min, tune_y_max);
    draw_xy_ticks(
        &mut image,
        conf_bounds,
        1.0,
        3.0,
        0.0,
        (h_conf
            .iter()
            .chain(v_conf.iter())
            .map(|(_, y)| *y)
            .fold(1.0f64, f64::max)
            * 1.1)
            .max(1.0),
    );

    draw_polyline_xy(
        &mut image,
        tune_bounds,
        &h_tune,
        1.0,
        3.0,
        tune_y_min,
        tune_y_max,
        [0, 70, 220],
    );
    draw_polyline_xy(
        &mut image,
        tune_bounds,
        &v_tune,
        1.0,
        3.0,
        tune_y_min,
        tune_y_max,
        [220, 0, 0],
    );
    let conf_max = (h_conf
        .iter()
        .chain(v_conf.iter())
        .map(|(_, y)| *y)
        .fold(1.0f64, f64::max)
        * 1.1)
        .max(1.0);
    draw_polyline_xy(
        &mut image,
        conf_bounds,
        &h_conf,
        1.0,
        3.0,
        0.0,
        conf_max,
        [0, 70, 220],
    );
    draw_polyline_xy(
        &mut image,
        conf_bounds,
        &v_conf,
        1.0,
        3.0,
        0.0,
        conf_max,
        [220, 0, 0],
    );

    let legend_x = image.width as i32 - tune_bounds.right as i32 - 180;
    draw_line_legend(
        &mut image,
        (legend_x, tune_bounds.top as i32 + 8),
        &[([0, 70, 220], "H"), ([220, 0, 0], "V")],
    );
    draw_text_small(
        &mut image,
        tune_bounds.left as i32 + 4,
        10,
        "METHOD TUNE / CONF",
        [0, 0, 0],
        2,
    );
    let method_label_y = (image.height - conf_bounds.bottom + 8) as i32;
    draw_text_small(
        &mut image,
        tune_bounds.left as i32 + 4,
        method_label_y,
        "M1=BEST M2=UNW M3=WGT",
        [0, 0, 0],
        2,
    );

    write_png_rgb(path, &image)
}

fn write_window_sensitivity_png(
    path: &Path,
    title: &str,
    horizontal: &[SweepPoint],
    vertical: &[SweepPoint],
    tune_y_min: f64,
    tune_y_max: f64,
) -> Result<()> {
    let mut image = RgbImage::new(1280, 900);
    image.fill([255, 255, 255]);

    let tune_bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 40,
        bottom: 490,
    };
    let conf_bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 520,
        bottom: 70,
    };

    draw_axes(&mut image, tune_bounds, [0, 0, 0]);
    draw_axes(&mut image, conf_bounds, [0, 0, 0]);

    let x_values = horizontal
        .iter()
        .chain(vertical.iter())
        .map(|p| p.x as f64)
        .collect::<Vec<_>>();
    let x_min = x_values
        .iter()
        .copied()
        .fold(f64::INFINITY, f64::min)
        .max(0.0);
    let x_max = x_values
        .iter()
        .copied()
        .fold(f64::NEG_INFINITY, f64::max)
        .max(x_min + 1.0);

    let h_tune = horizontal
        .iter()
        .filter_map(|p| p.tune.map(|v| (p.x as f64, v)))
        .collect::<Vec<_>>();
    let v_tune = vertical
        .iter()
        .filter_map(|p| p.tune.map(|v| (p.x as f64, v)))
        .collect::<Vec<_>>();
    let h_conf = horizontal
        .iter()
        .filter_map(|p| p.confidence.map(|v| (p.x as f64, v)))
        .collect::<Vec<_>>();
    let v_conf = vertical
        .iter()
        .filter_map(|p| p.confidence.map(|v| (p.x as f64, v)))
        .collect::<Vec<_>>();

    draw_xy_ticks(
        &mut image,
        tune_bounds,
        x_min,
        x_max,
        tune_y_min,
        tune_y_max,
    );
    let conf_max = h_conf
        .iter()
        .chain(v_conf.iter())
        .map(|(_, y)| *y)
        .fold(1.0f64, f64::max)
        .max(1.0);
    draw_xy_ticks(&mut image, conf_bounds, x_min, x_max, 0.0, conf_max * 1.1);

    draw_polyline_xy(
        &mut image,
        tune_bounds,
        &h_tune,
        x_min,
        x_max,
        tune_y_min,
        tune_y_max,
        [0, 70, 220],
    );
    draw_polyline_xy(
        &mut image,
        tune_bounds,
        &v_tune,
        x_min,
        x_max,
        tune_y_min,
        tune_y_max,
        [220, 0, 0],
    );
    draw_polyline_xy(
        &mut image,
        conf_bounds,
        &h_conf,
        x_min,
        x_max,
        0.0,
        conf_max * 1.1,
        [0, 70, 220],
    );
    draw_polyline_xy(
        &mut image,
        conf_bounds,
        &v_conf,
        x_min,
        x_max,
        0.0,
        conf_max * 1.1,
        [220, 0, 0],
    );

    let legend_x = image.width as i32 - tune_bounds.right as i32 - 180;
    draw_line_legend(
        &mut image,
        (legend_x, tune_bounds.top as i32 + 8),
        &[([0, 70, 220], "H"), ([220, 0, 0], "V")],
    );
    draw_text_small(
        &mut image,
        tune_bounds.left as i32 + 4,
        10,
        title,
        [0, 0, 0],
        2,
    );
    draw_text_small(
        &mut image,
        tune_bounds.left as i32 + 4,
        26,
        "TOP:TUNE  BOT:CONF",
        [0, 0, 0],
        2,
    );

    write_png_rgb(path, &image)
}

fn write_findings_summary(
    path: &Path,
    target_ms: u64,
    config: &MonitorConfig,
    warnings: &[String],
    h_start: &[SweepPoint],
    v_start: &[SweepPoint],
    h_length: &[SweepPoint],
    v_length: &[SweepPoint],
    metrics: &[BpmMetric],
    methods: &[MethodResult],
) -> Result<()> {
    let h_start_std = stddev(&h_start.iter().filter_map(|p| p.tune).collect::<Vec<_>>());
    let v_start_std = stddev(&v_start.iter().filter_map(|p| p.tune).collect::<Vec<_>>());
    let h_len_std = stddev(&h_length.iter().filter_map(|p| p.tune).collect::<Vec<_>>());
    let v_len_std = stddev(&v_length.iter().filter_map(|p| p.tune).collect::<Vec<_>>());

    let flagged = metrics
        .iter()
        .filter(|m| !m.flags.iter().any(|f| f == "OK"))
        .collect::<Vec<_>>();
    let recommendation = choose_default_method(methods);

    let mut lines = Vec::<String>::new();
    lines.push("# Tune Analysis Findings".to_string());
    lines.push(String::new());
    lines.push(format!("- target_ms: `{target_ms}`"));
    lines.push(format!(
        "- configured digitizers: `{}`",
        config.devices.len()
    ));
    lines.push(format!(
        "- alignment tolerance: `±{} ms`",
        config.align_tolerance_ms
    ));
    lines.push(String::new());
    lines.push("## Window Stability".to_string());
    lines.push(format!(
        "- H tune std vs start: `{}` | vs length: `{}`",
        opt_fmt(h_start_std),
        opt_fmt(h_len_std)
    ));
    lines.push(format!(
        "- V tune std vs start: `{}` | vs length: `{}`",
        opt_fmt(v_start_std),
        opt_fmt(v_len_std)
    ));
    lines.push(String::new());
    lines.push("## BPM Quality".to_string());
    lines.push(format!("- total BPM metrics: `{}`", metrics.len()));
    lines.push(format!("- flagged BPM channels: `{}`", flagged.len()));
    for metric in flagged.iter().take(10) {
        lines.push(format!(
            "  - {} {} score={:.3} flags={}",
            metric.plane.label(),
            metric.stream_key,
            metric.score,
            metric.flags.join("|")
        ));
    }
    lines.push(String::new());
    lines.push("## Method Comparison (Phase 1-3)".to_string());
    for plane in [Plane::Horizontal, Plane::Vertical] {
        lines.push(format!("- Plane `{}`", plane.label()));
        for method in ["BEST_SINGLE", "UNWEIGHTED", "WEIGHTED"] {
            if let Some(row) = methods
                .iter()
                .find(|m| m.plane == plane && m.method == method)
            {
                lines.push(format!(
                    "  - {} tune={} conf={} start_std={} length_std={}",
                    method,
                    opt_fmt(row.tune),
                    opt_fmt(row.confidence),
                    opt_fmt(row.start_tune_std),
                    opt_fmt(row.length_tune_std)
                ));
            }
        }
    }
    lines.push(String::new());
    lines.push("## Recommendation".to_string());
    lines.push(format!("- default method: `{}`", recommendation));
    lines.push("- SVD status: deferred for next phase after baseline validation".to_string());

    if warnings.is_empty() {
        lines.push("- warnings: none".to_string());
    } else {
        lines.push(format!("- warnings: `{}`", warnings.len()));
        for warning in warnings.iter().take(20) {
            lines.push(format!("  - {}", warning));
        }
    }

    fs::write(path, lines.join("\n") + "\n")
        .with_context(|| format!("failed to write {}", path.display()))
}

fn choose_default_method(methods: &[MethodResult]) -> &'static str {
    let mut method_scores = Vec::<(&'static str, f64)>::new();
    for method in ["BEST_SINGLE", "WEIGHTED", "UNWEIGHTED"] {
        let rows = methods
            .iter()
            .filter(|m| m.method == method)
            .collect::<Vec<_>>();
        if rows.is_empty() {
            continue;
        }
        let conf = rows.iter().filter_map(|r| r.confidence).collect::<Vec<_>>();
        let avg_conf = if conf.is_empty() {
            0.0
        } else {
            conf.iter().sum::<f64>() / conf.len() as f64
        };
        let stability_terms = rows
            .iter()
            .filter_map(|r| match (r.start_tune_std, r.length_tune_std) {
                (Some(a), Some(b)) => Some((a + b) / 2.0),
                (Some(a), None) => Some(a),
                (None, Some(b)) => Some(b),
                (None, None) => None,
            })
            .collect::<Vec<_>>();
        let avg_stability = if stability_terms.is_empty() {
            1.0
        } else {
            stability_terms.iter().sum::<f64>() / stability_terms.len() as f64
        };
        let score = avg_conf / (avg_stability + 1e-6);
        method_scores.push((method, score));
    }

    method_scores.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(Ordering::Equal));
    method_scores
        .first()
        .map(|(method, _)| *method)
        .unwrap_or("UNWEIGHTED")
}

fn median(values: &[f64]) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(Ordering::Equal));
    let mid = sorted.len() / 2;
    if sorted.len() % 2 == 0 {
        Some((sorted[mid - 1] + sorted[mid]) * 0.5)
    } else {
        Some(sorted[mid])
    }
}

fn stddev(values: &[f64]) -> Option<f64> {
    if values.len() < 2 {
        return None;
    }
    let mean = values.iter().sum::<f64>() / values.len() as f64;
    let var = values
        .iter()
        .map(|v| {
            let d = *v - mean;
            d * d
        })
        .sum::<f64>()
        / values.len() as f64;
    Some(var.sqrt())
}

fn opt_fmt(value: Option<f64>) -> String {
    value
        .map(|v| format!("{v:.6}"))
        .unwrap_or_else(|| "NA".to_string())
}

fn run_analyze_spill_historical(
    config: MonitorConfig,
    out_dir: &Path,
    free_run: bool,
    free_run_count: Option<usize>,
    stale_depth: usize,
    flash_count: Option<usize>,
) -> Result<()> {
    let candidates = discover_historical_candidates(&config, stale_depth)?;
    if candidates.is_empty() {
        bail!(
            "no historical TBT stream entries were discovered (stale_depth={})",
            stale_depth
        );
    }

    println!(
        "analyze-spill no-beam: stale_depth={} candidates={}",
        stale_depth,
        candidates.len()
    );

    if !free_run {
        let mut attempted = 0usize;
        let mut skipped = 0usize;
        for candidate in &candidates {
            attempted += 1;
            let snapshot =
                match analyze_spill_snapshot_at_target(&config, candidate.target_ms, flash_count) {
                    Ok(snapshot) => snapshot,
                    Err(err) => {
                        skipped += 1;
                        eprintln!(
                            "[no-beam analyze-spill] skipped {} (coverage={} obs={}): {}",
                            candidate.target_ms,
                            candidate.stream_coverage,
                            candidate.observation_count,
                            err
                        );
                        continue;
                    }
                };

            let paths = write_spill_outputs(out_dir, None, &config, &snapshot, flash_count)?;
            let _ = print_summary(
                &config,
                &snapshot,
                &paths,
                &format!(
                    "analyze-spill no-beam summary target={} coverage={} observations={} attempted={}",
                    snapshot.target_ms,
                    candidate.stream_coverage,
                    candidate.observation_count,
                    attempted
                ),
                true,
            );
            println!(
                "no-beam summary: stale_depth={} candidates_discovered={} candidates_attempted={} candidates_skipped_unresolved={} successful_analyses=1",
                stale_depth,
                candidates.len(),
                attempted,
                skipped
            );
            return Ok(());
        }

        bail!(
            "no historical spill candidates produced usable analysis outputs (attempted {}, stale_depth={})",
            attempted,
            stale_depth
        );
    }

    let mut attempted = 0usize;
    let mut skipped = 0usize;
    let mut successful = 0usize;
    let mut captured = Vec::<(usize, SpillSnapshot)>::new();
    let max_successes = free_run_count.unwrap_or(usize::MAX);
    if let Some(limit) = free_run_count {
        println!(
            "no-beam free-run stop condition: {} successful analyses",
            limit
        );
    }
    for candidate in &candidates {
        if successful >= max_successes {
            break;
        }
        attempted += 1;
        let snapshot =
            match analyze_spill_snapshot_at_target(&config, candidate.target_ms, flash_count) {
                Ok(snapshot) => snapshot,
                Err(err) => {
                    skipped += 1;
                    eprintln!(
                        "[no-beam free-run] skipped {} (coverage={} obs={}): {}",
                        candidate.target_ms,
                        candidate.stream_coverage,
                        candidate.observation_count,
                        err
                    );
                    continue;
                }
            };

        let stem = format!("spill_{}", snapshot.target_ms);
        match write_spill_outputs(out_dir, Some(&stem), &config, &snapshot, flash_count) {
            Ok(paths) => {
                let lines = print_summary(
                    &config,
                    &snapshot,
                    &paths,
                    &format!(
                        "[no-beam free-run] target {} coverage={} obs={}",
                        snapshot.target_ms, candidate.stream_coverage, candidate.observation_count
                    ),
                    false,
                );
                let summary_path = out_dir.join(format!("{stem}_summary.txt"));
                if let Err(err) = write_summary_text(&summary_path, &lines) {
                    eprintln!(
                        "[no-beam free-run] failed writing metadata summary {}: {}",
                        summary_path.display(),
                        err
                    );
                }
                successful += 1;
                captured.push((attempted, snapshot));
            }
            Err(err) => {
                skipped += 1;
                eprintln!(
                    "[no-beam free-run] failed writing outputs for target {}: {}",
                    snapshot.target_ms, err
                );
            }
        }
    }

    println!("no-beam sweep summary:");
    println!("  stale_depth scanned: {}", stale_depth);
    println!(
        "  historical candidates discovered (merged target windows): {}",
        candidates.len()
    );
    println!("  candidates attempted: {}", attempted);
    println!("  candidates skipped unresolved: {}", skipped);
    println!("  successful analyses: {}", successful);

    if free_run_count.is_some() && !captured.is_empty() {
        let counters = BatchRunCounters {
            unresolved_wakes: skipped,
            duplicate_wakes: 0,
            stale_depth_scanned: Some(stale_depth),
            historical_candidates_discovered: candidates.len(),
            historical_candidates_attempted: attempted,
            historical_candidates_skipped: skipped,
        };
        synthesize_batch_outputs_from_captured_spills(
            out_dir,
            &config,
            captured,
            counters,
            "analyze-spill no-beam free-run",
            flash_count,
        )?;
    }

    if let Some(limit) = free_run_count {
        if successful < limit {
            bail!(
                "no-beam free-run requested {} successful analyses but only {} succeeded (stale_depth={}, candidates_discovered={})",
                limit,
                successful,
                stale_depth,
                candidates.len()
            );
        }
    }

    if successful == 0 {
        bail!(
            "no historical spill candidates produced usable analysis outputs (attempted {}, stale_depth={})",
            attempted,
            stale_depth
        );
    }

    Ok(())
}

fn run_analyze_study_historical(
    config: MonitorConfig,
    out_dir: &Path,
    options: StudyOptions,
    free_run: bool,
    free_run_count: Option<usize>,
    stale_depth: usize,
) -> Result<()> {
    let candidates = discover_historical_candidates(&config, stale_depth)?;
    if candidates.is_empty() {
        bail!(
            "no historical TBT stream entries were discovered for analyze-phase (stale_depth={})",
            stale_depth
        );
    }

    println!(
        "analyze-phase no-beam: stale_depth={} candidates={}",
        stale_depth,
        candidates.len()
    );

    if !free_run {
        let mut attempted = 0usize;
        let mut skipped = 0usize;
        for candidate in &candidates {
            attempted += 1;
            let snapshot =
                match analyze_spill_snapshot_at_target(&config, candidate.target_ms, None) {
                    Ok(snapshot) => snapshot,
                    Err(err) => {
                        skipped += 1;
                        eprintln!(
                            "[no-beam analyze-phase] skipped {} (coverage={} obs={}): {}",
                            candidate.target_ms,
                            candidate.stream_coverage,
                            candidate.observation_count,
                            err
                        );
                        continue;
                    }
                };

            let _ = run_analyze_study_for_snapshot(
                &config,
                out_dir,
                &options,
                snapshot,
                None,
                &format!(
                    "analyze-phase no-beam summary coverage={} observations={} attempted={}",
                    candidate.stream_coverage, candidate.observation_count, attempted
                ),
            )?;
            println!(
                "no-beam analyze-phase summary: stale_depth={} candidates_discovered={} candidates_attempted={} candidates_skipped_unresolved={} successful_analyses=1",
                stale_depth,
                candidates.len(),
                attempted,
                skipped
            );
            return Ok(());
        }

        bail!(
            "no historical spill candidates produced usable analyze-phase outputs (attempted {}, stale_depth={})",
            attempted,
            stale_depth
        );
    }

    let mut attempted = 0usize;
    let mut skipped = 0usize;
    let mut successful = 0usize;
    let max_successes = free_run_count.unwrap_or(usize::MAX);
    if let Some(limit) = free_run_count {
        println!(
            "no-beam analyze-phase free-run stop condition: {} successful analyses",
            limit
        );
    }
    for candidate in &candidates {
        if successful >= max_successes {
            break;
        }
        attempted += 1;
        let snapshot = match analyze_spill_snapshot_at_target(&config, candidate.target_ms, None) {
            Ok(snapshot) => snapshot,
            Err(err) => {
                skipped += 1;
                eprintln!(
                    "[no-beam analyze-phase free-run] skipped {} (coverage={} obs={}): {}",
                    candidate.target_ms,
                    candidate.stream_coverage,
                    candidate.observation_count,
                    err
                );
                continue;
            }
        };

        let target_ms = snapshot.target_ms;
        let stem = format!("spill_{target_ms}");
        match run_analyze_study_for_snapshot(
            &config,
            out_dir,
            &options,
            snapshot,
            Some(&stem),
            &format!(
                "[no-beam analyze-phase free-run] target {} coverage={} obs={}",
                target_ms, candidate.stream_coverage, candidate.observation_count
            ),
        ) {
            Ok(lines) => {
                let summary_path = out_dir.join(format!("{stem}_analyze_phase_summary.txt"));
                if let Err(err) = write_summary_text(&summary_path, &lines) {
                    eprintln!(
                        "[no-beam analyze-phase free-run] failed writing metadata summary {}: {}",
                        summary_path.display(),
                        err
                    );
                }
                successful += 1;
            }
            Err(err) => {
                skipped += 1;
                eprintln!(
                    "[no-beam analyze-phase free-run] failed writing outputs for target {}: {}",
                    target_ms, err
                );
            }
        }
    }

    println!("no-beam analyze-phase sweep summary:");
    println!("  stale_depth scanned: {}", stale_depth);
    println!(
        "  historical candidates discovered (merged target windows): {}",
        candidates.len()
    );
    println!("  candidates attempted: {}", attempted);
    println!("  candidates skipped unresolved: {}", skipped);
    println!("  successful analyses: {}", successful);

    if let Some(limit) = free_run_count {
        if successful < limit {
            bail!(
                "no-beam analyze-phase free-run requested {} successful analyses but only {} succeeded (stale_depth={}, candidates_discovered={})",
                limit,
                successful,
                stale_depth,
                candidates.len()
            );
        }
    }

    if successful == 0 {
        bail!(
            "no historical spill candidates produced usable analyze-phase outputs (attempted {}, stale_depth={})",
            attempted,
            stale_depth
        );
    }

    Ok(())
}

fn run_analyze_spill_once(
    config: MonitorConfig,
    out_dir: &Path,
    flash_count: Option<usize>,
) -> Result<()> {
    let snapshot = analyze_spill_snapshot(&config, flash_count)?;
    let paths = write_spill_outputs(out_dir, None, &config, &snapshot, flash_count)?;

    let _ = print_summary(&config, &snapshot, &paths, "analyze-spill summary", true);

    Ok(())
}

fn run_analyze_spill_free_run(
    config: MonitorConfig,
    out_dir: &Path,
    free_run_count: Option<usize>,
    flash_count: Option<usize>,
) -> Result<()> {
    if config.devices.is_empty() {
        bail!("config has no devices for free-run analyze-spill");
    }

    println!(
        "analyze-spill free-run mode: watching {} devices, running global all-stream snapshots",
        config.devices.len()
    );
    if let Some(count) = free_run_count {
        println!("free-run stop condition: {count} successful analyses");
        println!("press Ctrl-C to stop early");
    } else {
        println!("press Ctrl-C to stop");
    }

    let (tx, rx) = mpsc::channel::<FreeRunSignal>();

    for device in config.devices.clone() {
        let tx_worker = tx.clone();
        let reconnect_initial_ms = config.reconnect_initial_ms;
        let reconnect_max_ms = config.reconnect_max_ms;
        thread::spawn(move || {
            if let Err(err) =
                run_free_run_watch_worker(device, reconnect_initial_ms, reconnect_max_ms, tx_worker)
            {
                eprintln!("free-run watch worker exited: {err}");
            }
        });
    }
    drop(tx);

    let mut last_written_target_ms: Option<u64> = None;
    let dedupe_tolerance_ms = target_bucket_tolerance_ms(&config);
    let mut successful = 0usize;
    let mut attempt_index = 0usize;
    let mut counters = BatchRunCounters {
        unresolved_wakes: 0,
        duplicate_wakes: 0,
        stale_depth_scanned: None,
        historical_candidates_discovered: 0,
        historical_candidates_attempted: 0,
        historical_candidates_skipped: 0,
    };
    let mut captured = Vec::<(usize, SpillSnapshot)>::new();
    loop {
        let signal = match rx.recv() {
            Ok(signal) => signal,
            Err(err) => bail!("free-run event channel closed: {err}"),
        };
        attempt_index += 1;

        let snapshot = match analyze_spill_snapshot_with_retries(&config, flash_count) {
            Ok(snapshot) => snapshot,
            Err(err) => {
                counters.unresolved_wakes += 1;
                eprintln!(
                    "[free-run] snapshot after {} {} failed: {}",
                    signal.bpm_ip, signal.event.id, err
                );
                continue;
            }
        };

        if last_written_target_ms
            .map(|last| abs_diff_u64(last, snapshot.target_ms) <= dedupe_tolerance_ms)
            .unwrap_or(false)
        {
            counters.duplicate_wakes += 1;
            continue;
        }

        let stem = format!("spill_{}", snapshot.target_ms);
        match write_spill_outputs(out_dir, Some(&stem), &config, &snapshot, flash_count) {
            Ok(paths) => {
                let lines = print_summary(
                    &config,
                    &snapshot,
                    &paths,
                    &format!(
                        "[free-run] wake {} {} (ms {}) -> target {}",
                        signal.bpm_ip, signal.event.id, signal.event.ms, snapshot.target_ms
                    ),
                    false,
                );
                let summary_path = out_dir.join(format!("{stem}_summary.txt"));
                if let Err(err) = write_summary_text(&summary_path, &lines) {
                    eprintln!(
                        "[free-run] failed writing metadata summary {}: {}",
                        summary_path.display(),
                        err
                    );
                }
                last_written_target_ms = Some(snapshot.target_ms);
                successful += 1;
                captured.push((attempt_index, snapshot));
                if let Some(limit) = free_run_count {
                    println!("[free-run] successful analyses: {}/{}", successful, limit);
                    if successful >= limit {
                        synthesize_batch_outputs_from_captured_spills(
                            out_dir,
                            &config,
                            captured,
                            counters,
                            "analyze-spill free-run",
                            flash_count,
                        )?;
                        println!("[free-run] reached requested count ({}), exiting", limit);
                        return Ok(());
                    }
                }
            }
            Err(err) => {
                eprintln!(
                    "[free-run] failed writing outputs for target {}: {}",
                    snapshot.target_ms, err
                );
            }
        }
    }
}

fn run_free_run_watch_worker(
    device: DeviceConfig,
    reconnect_initial_ms: u64,
    reconnect_max_ms: u64,
    tx: Sender<FreeRunSignal>,
) -> Result<()> {
    let tbt_keys = collect_tbt_stream_keys(&device);
    if tbt_keys.is_empty() {
        bail!("{} has no TBT_POSITION_SCALED stream keys", device.bpm_ip);
    }

    let mut reconnect_delay_ms = reconnect_initial_ms.max(250);
    let reconnect_cap_ms = reconnect_max_ms.max(reconnect_delay_ms);

    loop {
        let mut conn = match connect_device(&device.redis) {
            Ok(conn) => {
                reconnect_delay_ms = reconnect_initial_ms.max(250);
                conn
            }
            Err(err) => {
                eprintln!(
                    "[free-run {}] connect failed: {} (retry in {} ms)",
                    device.bpm_ip, err, reconnect_delay_ms
                );
                thread::sleep(Duration::from_millis(reconnect_delay_ms));
                reconnect_delay_ms = (reconnect_delay_ms.saturating_mul(2)).min(reconnect_cap_ms);
                continue;
            }
        };

        let mut read_ids = initialize_read_ids(&mut conn, &tbt_keys, &device.bpm_ip);
        if read_ids.iter().all(|id| id == "$") {
            eprintln!(
                "[free-run {}] no baseline IDs found yet, waiting for first entries",
                device.bpm_ip
            );
        }

        loop {
            match wait_for_next_device_event(&mut conn, &tbt_keys, &mut read_ids) {
                Ok(Some(event)) => {
                    if tx
                        .send(FreeRunSignal {
                            bpm_ip: device.bpm_ip.clone(),
                            event,
                        })
                        .is_err()
                    {
                        return Ok(());
                    }
                }
                Ok(None) => continue,
                Err(err) => {
                    eprintln!(
                        "[free-run {}] read failed: {} (reconnect in {} ms)",
                        device.bpm_ip, err, reconnect_delay_ms
                    );
                    thread::sleep(Duration::from_millis(reconnect_delay_ms));
                    reconnect_delay_ms =
                        (reconnect_delay_ms.saturating_mul(2)).min(reconnect_cap_ms);
                    break;
                }
            }
        }
    }
}

fn analyze_spill_snapshot_with_retries(
    config: &MonitorConfig,
    flash_count: Option<usize>,
) -> Result<SpillSnapshot> {
    let mut last_error: Option<anyhow::Error> = None;
    for attempt in 0..=FREE_RUN_SETTLE_RETRIES {
        match analyze_spill_snapshot(config, flash_count) {
            Ok(snapshot) => return Ok(snapshot),
            Err(err) => {
                last_error = Some(err);
                if attempt == FREE_RUN_SETTLE_RETRIES {
                    break;
                }
                thread::sleep(Duration::from_millis(FREE_RUN_SETTLE_DELAY_MS));
            }
        }
    }

    Err(last_error.unwrap_or_else(|| anyhow!("spill analysis failed without explicit error")))
}

fn analyze_spill_snapshot(
    config: &MonitorConfig,
    flash_count: Option<usize>,
) -> Result<SpillSnapshot> {
    let mut warnings = Vec::<String>::new();
    let requested_streams = count_requested_tbt_streams(config);

    let mut tbt_observations = collect_latest_tbt_observations(config, &mut warnings)?;
    if tbt_observations.is_empty() {
        bail!("no latest TBT observations were available from any configured device");
    }
    if tbt_observations.len() < requested_streams {
        warnings.push(format!(
            "incomplete TBT poll at target selection: observed {} of {} configured streams",
            tbt_observations.len(),
            requested_streams
        ));
    }

    let target_ms = choose_target_millisecond(
        &tbt_observations.iter().map(|o| o.ms).collect::<Vec<_>>(),
        target_bucket_tolerance_ms(config),
    )
    .ok_or_else(|| anyhow!("failed to choose target TBT millisecond"))?;

    for obs in &mut tbt_observations {
        obs.aligned = abs_diff_u64(obs.ms, target_ms) <= config.align_tolerance_ms;
    }

    let aligned = tbt_observations.iter().filter(|o| o.aligned).count();
    let aligned_fraction = aligned as f64 / tbt_observations.len() as f64;
    if aligned_fraction < config.min_aligned_fraction {
        warnings.push(format!(
            "TBT stream alignment fraction {:.1}% is below configured minimum {:.1}%",
            aligned_fraction * 100.0,
            config.min_aligned_fraction * 100.0
        ));
    }

    let traces =
        collect_stream_traces(config, target_ms, config.align_tolerance_ms, &mut warnings)?;
    if traces.is_empty() {
        bail!(
            "no TBT stream entries were found within ±{} ms of target {}",
            config.align_tolerance_ms,
            target_ms
        );
    }
    if traces.len() < requested_streams {
        warnings.push(format!(
            "incomplete near-target poll: usable traces {} of {} configured streams",
            traces.len(),
            requested_streams
        ));
    }

    let horizontal = traces
        .iter()
        .filter(|trace| trace.plane == Plane::Horizontal)
        .cloned()
        .collect::<Vec<_>>();
    let vertical = traces
        .iter()
        .filter(|trace| trace.plane == Plane::Vertical)
        .cloned()
        .collect::<Vec<_>>();

    let h_analysis = analyze_plane(
        Plane::Horizontal,
        horizontal,
        config,
        flash_count,
        &mut warnings,
    )?;
    let v_analysis = analyze_plane(
        Plane::Vertical,
        vertical,
        config,
        flash_count,
        &mut warnings,
    )?;

    if h_analysis.is_none() && v_analysis.is_none() {
        bail!("both H and V planes were unusable after filtering and window checks");
    }

    Ok(SpillSnapshot {
        target_ms,
        observations: tbt_observations,
        h_analysis,
        v_analysis,
        warnings,
    })
}

fn analyze_spill_snapshot_at_target(
    config: &MonitorConfig,
    target_ms: u64,
    flash_count: Option<usize>,
) -> Result<SpillSnapshot> {
    let mut warnings = Vec::<String>::new();
    let requested_streams = count_requested_tbt_streams(config);

    let (traces, observations) = collect_stream_traces_with_observations(
        config,
        target_ms,
        config.align_tolerance_ms,
        &mut warnings,
    )?;
    if traces.is_empty() {
        bail!(
            "no TBT stream entries were found within ±{} ms of historical target {}",
            config.align_tolerance_ms,
            target_ms
        );
    }

    if observations.is_empty() {
        warnings.push(format!(
            "no parseable near-target stream IDs were observed at target {}",
            target_ms
        ));
    } else {
        if observations.len() < requested_streams {
            warnings.push(format!(
                "incomplete historical poll: observed {} of {} configured streams near target {}",
                observations.len(),
                requested_streams,
                target_ms
            ));
        }
        let aligned = observations.iter().filter(|obs| obs.aligned).count();
        let aligned_fraction = aligned as f64 / observations.len() as f64;
        if aligned_fraction < config.min_aligned_fraction {
            warnings.push(format!(
                "TBT stream alignment fraction {:.1}% is below configured minimum {:.1}%",
                aligned_fraction * 100.0,
                config.min_aligned_fraction * 100.0
            ));
        }
    }
    if traces.len() < requested_streams {
        warnings.push(format!(
            "incomplete historical near-target poll: usable traces {} of {} configured streams at target {}",
            traces.len(),
            requested_streams,
            target_ms
        ));
    }

    let horizontal = traces
        .iter()
        .filter(|trace| trace.plane == Plane::Horizontal)
        .cloned()
        .collect::<Vec<_>>();
    let vertical = traces
        .iter()
        .filter(|trace| trace.plane == Plane::Vertical)
        .cloned()
        .collect::<Vec<_>>();

    let h_analysis = analyze_plane(
        Plane::Horizontal,
        horizontal,
        config,
        flash_count,
        &mut warnings,
    )?;
    let v_analysis = analyze_plane(
        Plane::Vertical,
        vertical,
        config,
        flash_count,
        &mut warnings,
    )?;

    if h_analysis.is_none() && v_analysis.is_none() {
        bail!("both H and V planes were unusable after filtering and window checks");
    }

    Ok(SpillSnapshot {
        target_ms,
        observations,
        h_analysis,
        v_analysis,
        warnings,
    })
}

fn analyze_captured_spill_snapshot(
    config: &MonitorConfig,
    bundle_path: &Path,
    flash_count: Option<usize>,
) -> Result<SpillSnapshot> {
    let (bundle_dir, manifest_path) = resolve_captured_bundle_paths(bundle_path)?;
    let manifest = load_captured_manifest(&manifest_path)?;
    validate_captured_manifest(&manifest, &manifest_path)?;

    let mut warnings = manifest
        .warnings
        .iter()
        .map(|warning| format!("capture manifest: {warning}"))
        .collect::<Vec<_>>();

    if let Some(redis_timestamp_ms) = manifest.redis_timestamp_ms {
        if redis_timestamp_ms != manifest.target_ms {
            warnings.push(format!(
                "manifest redis_timestamp_ms {} differs from target_ms {}",
                redis_timestamp_ms, manifest.target_ms
            ));
        }
    } else {
        warnings.push("manifest does not include redis_timestamp_ms".to_string());
    }

    if let Some(capture_tolerance) = manifest.align_tolerance_ms {
        if capture_tolerance != config.align_tolerance_ms {
            warnings.push(format!(
                "capture align_tolerance_ms {} differs from analysis config {}",
                capture_tolerance, config.align_tolerance_ms
            ));
        }
    }
    if let Some(capture_tolerance) = manifest.same_spill_tolerance_ms {
        if capture_tolerance != config.same_spill_tolerance_ms {
            warnings.push(format!(
                "capture same_spill_tolerance_ms {} differs from analysis config {}",
                capture_tolerance, config.same_spill_tolerance_ms
            ));
        }
    }

    let requested_streams = manifest
        .requested_streams
        .unwrap_or_else(|| count_requested_tbt_streams(config));
    let config_requested_streams = count_requested_tbt_streams(config);
    if config_requested_streams != 0 && config_requested_streams != requested_streams {
        warnings.push(format!(
            "config TBT stream count {} differs from captured manifest requested_streams {}",
            config_requested_streams, requested_streams
        ));
    }

    let (traces, observations) =
        load_captured_stream_traces(&bundle_dir, &manifest, config, &mut warnings)?;
    if traces.is_empty() {
        bail!(
            "captured spill bundle {} produced no usable TBT traces",
            bundle_path.display()
        );
    }

    if observations.is_empty() {
        warnings.push(format!(
            "no parseable captured stream IDs were present for target {}",
            manifest.target_ms
        ));
    } else {
        if observations.len() < requested_streams {
            warnings.push(format!(
                "incomplete captured manifest: observed {} of {} requested streams",
                observations.len(),
                requested_streams
            ));
        }
        let aligned = observations.iter().filter(|obs| obs.aligned).count();
        let aligned_fraction = aligned as f64 / observations.len() as f64;
        if aligned_fraction < config.min_aligned_fraction {
            warnings.push(format!(
                "TBT stream alignment fraction {:.1}% is below configured minimum {:.1}%",
                aligned_fraction * 100.0,
                config.min_aligned_fraction * 100.0
            ));
        }
    }
    if traces.len() < requested_streams {
        warnings.push(format!(
            "incomplete captured payload set: usable traces {} of {} requested streams",
            traces.len(),
            requested_streams
        ));
    }

    let horizontal = traces
        .iter()
        .filter(|trace| trace.plane == Plane::Horizontal)
        .cloned()
        .collect::<Vec<_>>();
    let vertical = traces
        .iter()
        .filter(|trace| trace.plane == Plane::Vertical)
        .cloned()
        .collect::<Vec<_>>();

    let h_analysis = analyze_plane(
        Plane::Horizontal,
        horizontal,
        config,
        flash_count,
        &mut warnings,
    )?;
    let v_analysis = analyze_plane(
        Plane::Vertical,
        vertical,
        config,
        flash_count,
        &mut warnings,
    )?;

    if h_analysis.is_none() && v_analysis.is_none() {
        bail!(
            "both H and V planes were unusable after captured-bundle filtering and window checks"
        );
    }

    Ok(SpillSnapshot {
        target_ms: manifest.target_ms,
        observations,
        h_analysis,
        v_analysis,
        warnings,
    })
}

fn resolve_captured_bundle_paths(bundle_path: &Path) -> Result<(PathBuf, PathBuf)> {
    if bundle_path
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name == "manifest.json")
    {
        let bundle_dir = bundle_path
            .parent()
            .ok_or_else(|| anyhow!("manifest path {} has no parent", bundle_path.display()))?
            .to_path_buf();
        return Ok((bundle_dir, bundle_path.to_path_buf()));
    }

    Ok((bundle_path.to_path_buf(), bundle_path.join("manifest.json")))
}

fn discover_captured_bundle_candidates(
    root: &Path,
) -> Result<(Vec<CapturedBundleCandidate>, usize)> {
    if is_manifest_path(root) || root.join("manifest.json").is_file() {
        return Ok((vec![captured_bundle_candidate(root)?], 0));
    }

    if !root.is_dir() {
        bail!(
            "{} is neither a captured-spill bundle directory nor a directory of bundles",
            root.display()
        );
    }

    let mut candidates = Vec::<CapturedBundleCandidate>::new();
    let mut skipped = 0usize;
    let entries =
        fs::read_dir(root).with_context(|| format!("failed to read {}", root.display()))?;

    for entry in entries {
        let entry = match entry {
            Ok(entry) => entry,
            Err(err) => {
                skipped += 1;
                eprintln!(
                    "[analyze-captured-spills] skipped unreadable directory entry under {}: {}",
                    root.display(),
                    err
                );
                continue;
            }
        };

        let path = entry.path();
        if !path.is_dir() || !path.join("manifest.json").is_file() {
            continue;
        }

        match captured_bundle_candidate(&path) {
            Ok(candidate) => candidates.push(candidate),
            Err(err) => {
                skipped += 1;
                eprintln!(
                    "[analyze-captured-spills] skipped {}: {}",
                    path.display(),
                    err
                );
            }
        }
    }

    candidates.sort_by(|a, b| {
        a.target_ms
            .cmp(&b.target_ms)
            .then_with(|| a.manifest_path.cmp(&b.manifest_path))
    });
    Ok((candidates, skipped))
}

fn captured_bundle_candidate(path: &Path) -> Result<CapturedBundleCandidate> {
    let (bundle_dir, manifest_path) = resolve_captured_bundle_paths(path)?;
    let manifest = load_captured_manifest(&manifest_path)?;
    validate_captured_manifest(&manifest, &manifest_path)?;
    Ok(CapturedBundleCandidate {
        bundle_dir,
        manifest_path,
        target_ms: manifest.target_ms,
    })
}

fn is_manifest_path(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name == "manifest.json")
}

fn load_captured_manifest(path: &Path) -> Result<CapturedManifest> {
    let raw = fs::read_to_string(path)
        .with_context(|| format!("failed to read captured manifest {}", path.display()))?;
    let value: Value = serde_json::from_str(&raw)
        .with_context(|| format!("failed to parse captured manifest {}", path.display()))?;
    parse_captured_manifest(&value).with_context(|| format!("invalid manifest {}", path.display()))
}

fn parse_captured_manifest(value: &Value) -> Result<CapturedManifest> {
    let obj = value
        .as_object()
        .ok_or_else(|| anyhow!("manifest root must be a JSON object"))?;

    let streams = required_array(obj, "streams", "manifest")?
        .iter()
        .enumerate()
        .map(|(idx, value)| parse_captured_manifest_stream(value, idx))
        .collect::<Result<Vec<_>>>()?;

    Ok(CapturedManifest {
        schema_version: required_u64(obj, "schema_version", "manifest")?,
        artifact_type: required_string(obj, "artifact_type", "manifest")?,
        target_ms: required_u64(obj, "target_ms", "manifest")?,
        redis_timestamp_ms: optional_u64(obj, "redis_timestamp_ms", "manifest")?,
        align_tolerance_ms: optional_u64(obj, "align_tolerance_ms", "manifest")?,
        same_spill_tolerance_ms: optional_u64(obj, "same_spill_tolerance_ms", "manifest")?,
        requested_streams: optional_usize(obj, "requested_streams", "manifest")?,
        streams,
        warnings: optional_string_array(obj, "warnings", "manifest")?,
    })
}

fn parse_captured_manifest_stream(value: &Value, idx: usize) -> Result<CapturedManifestStream> {
    let context = format!("streams[{idx}]");
    let obj = value
        .as_object()
        .ok_or_else(|| anyhow!("{context} must be a JSON object"))?;

    Ok(CapturedManifestStream {
        bpm_ip: required_string(obj, "bpm_ip", &context)?,
        stream_key: required_string(obj, "stream_key", &context)?,
        stream_id: required_string(obj, "stream_id", &context)?,
        stream_ms: required_u64(obj, "stream_ms", &context)?,
        payload_file: optional_string(obj, "payload_file", &context)?,
        payload_bytes: optional_usize(obj, "payload_bytes", &context)?,
        sample_count: optional_usize(obj, "sample_count", &context)?,
        checksum_fnv1a64: optional_string(obj, "checksum_fnv1a64", &context)?,
    })
}

fn validate_captured_manifest(manifest: &CapturedManifest, manifest_path: &Path) -> Result<()> {
    if manifest.schema_version != 1 {
        bail!(
            "{} has unsupported captured-spill schema_version {}",
            manifest_path.display(),
            manifest.schema_version
        );
    }
    if manifest.artifact_type != "tbt-monitor.captured-spill" {
        bail!(
            "{} has unsupported artifact_type '{}'",
            manifest_path.display(),
            manifest.artifact_type
        );
    }
    if manifest.streams.is_empty() {
        bail!("{} has no captured stream entries", manifest_path.display());
    }
    Ok(())
}

fn load_captured_stream_traces(
    bundle_dir: &Path,
    manifest: &CapturedManifest,
    config: &MonitorConfig,
    warnings: &mut Vec<String>,
) -> Result<(Vec<StreamTrace>, Vec<TbtObservation>)> {
    let mut traces = Vec::<StreamTrace>::new();
    let mut observations = Vec::<TbtObservation>::new();
    let same_spill_tolerance_ms = manifest
        .same_spill_tolerance_ms
        .or(manifest.align_tolerance_ms)
        .unwrap_or(config.same_spill_tolerance_ms);

    for stream in &manifest.streams {
        let Some(plane) = classify_plane(&stream.stream_key) else {
            warnings.push(format!(
                "{}: captured stream key {} is not a known TBT plane",
                stream.bpm_ip, stream.stream_key
            ));
            continue;
        };

        let parsed_ms = match parse_stream_id(&stream.stream_id) {
            Some((ms, _)) => {
                if ms != stream.stream_ms {
                    warnings.push(format!(
                        "{}: stream_id {} millisecond {} differs from manifest stream_ms {}",
                        stream.bpm_ip, stream.stream_id, ms, stream.stream_ms
                    ));
                }
                ms
            }
            None => {
                warnings.push(format!(
                    "{}: captured stream id {} for {} is not parseable",
                    stream.bpm_ip, stream.stream_id, stream.stream_key
                ));
                stream.stream_ms
            }
        };

        observations.push(TbtObservation {
            bpm_ip: stream.bpm_ip.clone(),
            stream_key: stream.stream_key.clone(),
            id: stream.stream_id.clone(),
            ms: parsed_ms,
            aligned: abs_diff_u64(parsed_ms, manifest.target_ms) <= same_spill_tolerance_ms,
        });

        let Some(payload_file) = stream.payload_file.as_deref() else {
            warnings.push(format!(
                "{}: captured {} ({}) has no payload_file",
                stream.bpm_ip, stream.stream_key, stream.stream_id
            ));
            continue;
        };
        let Some(payload_path) = safe_payload_path(bundle_dir, payload_file) else {
            warnings.push(format!(
                "{}: captured {} payload path {} is not a safe relative path",
                stream.bpm_ip, stream.stream_key, payload_file
            ));
            continue;
        };
        let payload = match fs::read(&payload_path) {
            Ok(payload) => payload,
            Err(err) => {
                warnings.push(format!(
                    "{}: failed reading captured payload {}: {}",
                    stream.bpm_ip,
                    payload_path.display(),
                    err
                ));
                continue;
            }
        };

        if let Some(expected_bytes) = stream.payload_bytes {
            if payload.len() != expected_bytes {
                warnings.push(format!(
                    "{}: captured {} payload byte count {} differs from manifest {}",
                    stream.bpm_ip,
                    stream.stream_key,
                    payload.len(),
                    expected_bytes
                ));
                continue;
            }
        }
        if let Some(expected_checksum) = stream.checksum_fnv1a64.as_deref() {
            let actual_checksum = fnv1a64_hex(&payload);
            if actual_checksum != expected_checksum {
                warnings.push(format!(
                    "{}: captured {} checksum {} differs from manifest {}",
                    stream.bpm_ip, stream.stream_key, actual_checksum, expected_checksum
                ));
                continue;
            }
        }

        let samples = match decode_f32_payload_bytes(&payload) {
            Ok(samples) => samples,
            Err(err) => {
                warnings.push(format!(
                    "{}: malformed captured payload in {} ({}): {}",
                    stream.bpm_ip, stream.stream_key, stream.stream_id, err
                ));
                continue;
            }
        };

        if let Some(expected_samples) = stream.sample_count {
            if samples.len() != expected_samples {
                warnings.push(format!(
                    "{}: captured {} sample count {} differs from manifest {}",
                    stream.bpm_ip,
                    stream.stream_key,
                    samples.len(),
                    expected_samples
                ));
                continue;
            }
        }

        traces.push(StreamTrace {
            plane,
            bpm_ip: stream.bpm_ip.clone(),
            stream_key: stream.stream_key.clone(),
            samples,
        });
    }

    Ok((traces, observations))
}

fn safe_payload_path(bundle_dir: &Path, payload_file: &str) -> Option<PathBuf> {
    let relative = Path::new(payload_file);
    if relative.is_absolute() {
        return None;
    }
    if relative.components().any(|component| {
        matches!(
            component,
            std::path::Component::ParentDir | std::path::Component::RootDir
        )
    }) {
        return None;
    }
    Some(bundle_dir.join(relative))
}

fn write_spill_outputs(
    out_dir: &Path,
    stem: Option<&str>,
    config: &MonitorConfig,
    snapshot: &SpillSnapshot,
    flash_count: Option<usize>,
) -> Result<SpillOutputPaths> {
    let (h_name, v_name, hg_name, vg_name, t_name, tv_name, s_name) = match stem {
        Some(stem) => (
            format!("{stem}_spectrum_h.png"),
            format!("{stem}_spectrum_v.png"),
            format!("{stem}_spectrogram_h.png"),
            format!("{stem}_spectrogram_v.png"),
            format!("{stem}_tune_vs_time.png"),
            format!("{stem}_tune_validation.png"),
            format!("{stem}_sliding_tune.csv"),
        ),
        None => (
            "spectrum_h.png".to_string(),
            "spectrum_v.png".to_string(),
            "spectrogram_h.png".to_string(),
            "spectrogram_v.png".to_string(),
            "tune_vs_time.png".to_string(),
            "tune_validation.png".to_string(),
            "sliding_tune.csv".to_string(),
        ),
    };

    let paths = SpillOutputPaths {
        spectrum_h: out_dir.join(h_name),
        spectrum_v: out_dir.join(v_name),
        spectrogram_h: out_dir.join(hg_name),
        spectrogram_v: out_dir.join(vg_name),
        tune_vs_time: out_dir.join(t_name),
        tune_validation: out_dir.join(tv_name),
        sliding_tune_csv: out_dir.join(s_name),
    };

    if let Some(h) = snapshot.h_analysis.as_ref() {
        write_spectrum_png(
            &paths.spectrum_h,
            &h.injection_spectrum,
            h.plane.tune_band(config),
            h.injection_peak.as_ref().map(|peak| peak.tune),
        )?;
    } else {
        write_empty_png(&paths.spectrum_h)?;
    }

    if let Some(v) = snapshot.v_analysis.as_ref() {
        write_spectrum_png(
            &paths.spectrum_v,
            &v.injection_spectrum,
            v.plane.tune_band(config),
            v.injection_peak.as_ref().map(|peak| peak.tune),
        )?;
    } else {
        write_empty_png(&paths.spectrum_v)?;
    }

    if let Some(h) = snapshot.h_analysis.as_ref() {
        write_spectrogram_png(
            &paths.spectrogram_h,
            h.plane,
            &h.sliding_spectra,
            &h.sliding,
            config,
        )?;
    } else {
        write_empty_png(&paths.spectrogram_h)?;
    }

    if let Some(v) = snapshot.v_analysis.as_ref() {
        write_spectrogram_png(
            &paths.spectrogram_v,
            v.plane,
            &v.sliding_spectra,
            &v.sliding,
            config,
        )?;
    } else {
        write_empty_png(&paths.spectrogram_v)?;
    }

    write_tune_trace_png(
        &paths.tune_vs_time,
        snapshot
            .h_analysis
            .as_ref()
            .map(|a| a.sliding.as_slice())
            .unwrap_or(&[]),
        snapshot
            .v_analysis
            .as_ref()
            .map(|a| a.sliding.as_slice())
            .unwrap_or(&[]),
        config.tune_plot_y_min,
        config.tune_plot_y_max,
        config.tune_plot_y_tick_step,
        config.plot_time_axes_in_us,
        config.turn_period_us,
        snapshot
            .h_analysis
            .as_ref()
            .and_then(|a| a.injection_peak.as_ref().map(|peak| peak.tune)),
        snapshot
            .v_analysis
            .as_ref()
            .and_then(|a| a.injection_peak.as_ref().map(|peak| peak.tune)),
        flash_count.is_some(),
    )?;

    write_tune_validation_png(&paths.tune_validation, snapshot, config)?;

    write_spill_sliding_csv(
        &paths.sliding_tune_csv,
        snapshot
            .h_analysis
            .as_ref()
            .map(|analysis| analysis.sliding.as_slice())
            .unwrap_or(&[]),
        snapshot
            .v_analysis
            .as_ref()
            .map(|analysis| analysis.sliding.as_slice())
            .unwrap_or(&[]),
    )?;

    Ok(paths)
}

fn write_summary_text(path: &Path, lines: &[String]) -> Result<()> {
    let mut content = lines.join("\n");
    content.push('\n');
    fs::write(path, content)
        .with_context(|| format!("failed to write summary text {}", path.display()))
}

fn discover_historical_candidates(
    config: &MonitorConfig,
    stale_depth: usize,
) -> Result<Vec<HistoricalCandidate>> {
    if stale_depth == 0 {
        bail!("stale_depth must be >= 1");
    }

    let mut coverage_map = HashMap::<u64, HashSet<String>>::new();
    let mut observation_count = HashMap::<u64, usize>::new();
    let mut scanned_streams = 0usize;

    for device in &config.devices {
        let mut conn = match connect_device(&device.redis) {
            Ok(conn) => conn,
            Err(err) => {
                eprintln!(
                    "[no-beam] {}: failed to connect for historical scan: {}",
                    device.bpm_ip, err
                );
                continue;
            }
        };

        for stream_key in collect_tbt_stream_keys(device) {
            scanned_streams += 1;
            let entries = match fetch_recent_entries(&mut conn, &stream_key, stale_depth) {
                Ok(entries) => entries,
                Err(err) => {
                    eprintln!(
                        "[no-beam] {}: failed reading historical entries for {}: {}",
                        device.bpm_ip, stream_key, err
                    );
                    continue;
                }
            };

            let stream_id = format!("{}|{}", device.bpm_ip, stream_key);
            for (id, _) in entries {
                let Some((ms, _)) = parse_stream_id(&id) else {
                    continue;
                };
                *observation_count.entry(ms).or_insert(0) += 1;
                coverage_map
                    .entry(ms)
                    .or_default()
                    .insert(stream_id.clone());
            }
        }
    }

    if scanned_streams == 0 {
        bail!("no TBT_POSITION_SCALED streams were available to scan");
    }

    Ok(rank_historical_candidates(
        coverage_map,
        observation_count,
        ADJACENT_BUCKET_TOLERANCE_MS,
    ))
}

fn rank_historical_candidates(
    coverage_map: HashMap<u64, HashSet<String>>,
    observation_count: HashMap<u64, usize>,
    merge_tolerance_ms: u64,
) -> Vec<HistoricalCandidate> {
    let mut all_ms = coverage_map
        .keys()
        .chain(observation_count.keys())
        .copied()
        .collect::<Vec<_>>();
    all_ms.sort_unstable();
    all_ms.dedup();

    // In no-beam mode, one physical spill can be split across adjacent timestamps.
    let mut clustered_ms = Vec::<Vec<u64>>::new();
    for ms in all_ms {
        if let Some(cluster) = clustered_ms.last_mut() {
            let last_ms = *cluster.last().expect("cluster has at least one timestamp");
            if ms.saturating_sub(last_ms) <= merge_tolerance_ms {
                cluster.push(ms);
                continue;
            }
        }
        clustered_ms.push(vec![ms]);
    }

    let mut candidates = clustered_ms
        .into_iter()
        .map(|cluster| {
            let target_ms = *cluster.last().expect("cluster has at least one timestamp");
            let mut merged_streams = HashSet::<String>::new();
            let mut merged_observations = 0usize;
            for ms in cluster {
                if let Some(streams) = coverage_map.get(&ms) {
                    merged_streams.extend(streams.iter().cloned());
                }
                merged_observations += observation_count.get(&ms).copied().unwrap_or(0);
            }
            HistoricalCandidate {
                target_ms,
                stream_coverage: merged_streams.len(),
                observation_count: merged_observations,
            }
        })
        .collect::<Vec<_>>();

    candidates.sort_by(|a, b| {
        b.target_ms
            .cmp(&a.target_ms)
            .then_with(|| b.stream_coverage.cmp(&a.stream_coverage))
            .then_with(|| b.observation_count.cmp(&a.observation_count))
    });
    candidates
}

fn write_spill_sliding_csv(
    path: &Path,
    horizontal: &[SlidingPoint],
    vertical: &[SlidingPoint],
) -> Result<()> {
    let mut rows = Vec::<String>::new();
    rows.push(
        "plane,window_index,center_turn,raw_global_tune,tracked_local_tune,selected_tune,raw_global_confidence,selected_confidence,used_global_fallback,suspicious_step,step_delta"
            .to_string(),
    );

    for (idx, point) in horizontal.iter().enumerate() {
        rows.push(format!(
            "H,{},{},{},{},{},{},{},{},{},{}",
            idx,
            point.center_turn,
            opt_fmt(point.raw_global_tune),
            opt_fmt(point.tracked_local_tune),
            opt_fmt(point.selected_tune),
            opt_fmt(point.raw_global_confidence),
            opt_fmt(point.selected_confidence),
            point.used_global_fallback,
            point.suspicious_step,
            opt_fmt(point.step_delta),
        ));
    }

    for (idx, point) in vertical.iter().enumerate() {
        rows.push(format!(
            "V,{},{},{},{},{},{},{},{},{},{}",
            idx,
            point.center_turn,
            opt_fmt(point.raw_global_tune),
            opt_fmt(point.tracked_local_tune),
            opt_fmt(point.selected_tune),
            opt_fmt(point.raw_global_confidence),
            opt_fmt(point.selected_confidence),
            point.used_global_fallback,
            point.suspicious_step,
            opt_fmt(point.step_delta),
        ));
    }

    fs::write(path, rows.join("\n") + "\n")
        .with_context(|| format!("failed to write {}", path.display()))
}

fn collect_latest_tbt_observations(
    config: &MonitorConfig,
    warnings: &mut Vec<String>,
) -> Result<Vec<TbtObservation>> {
    let mut observations = Vec::new();

    for device in &config.devices {
        let mut conn = match connect_device(&device.redis) {
            Ok(conn) => conn,
            Err(err) => {
                warnings.push(format!(
                    "{}: failed to connect for TBT latest-id read: {err}",
                    device.bpm_ip
                ));
                continue;
            }
        };

        let mut device_observations = 0usize;
        for stream_key in &device.stream_keys {
            if classify_plane(stream_key).is_none() {
                continue;
            }

            match fetch_latest_entry(&mut conn, stream_key) {
                Ok(Some((id, _))) => {
                    let Some((ms, _)) = parse_stream_id(&id) else {
                        warnings.push(format!(
                            "{}: TBT id {} for {} could not be parsed",
                            device.bpm_ip, id, stream_key
                        ));
                        continue;
                    };

                    observations.push(TbtObservation {
                        bpm_ip: device.bpm_ip.clone(),
                        stream_key: stream_key.clone(),
                        id,
                        ms,
                        aligned: false,
                    });
                    device_observations += 1;
                }
                Ok(None) => {}
                Err(err) => {
                    warnings.push(format!(
                        "{}: failed reading latest entry for {}: {err}",
                        device.bpm_ip, stream_key
                    ));
                }
            }
        }

        if device_observations == 0 {
            warnings.push(format!(
                "{}: no latest TBT entries found on configured position streams",
                device.bpm_ip
            ));
        }
    }

    Ok(observations)
}

fn collect_stream_traces(
    config: &MonitorConfig,
    target_ms: u64,
    tolerance_ms: u64,
    warnings: &mut Vec<String>,
) -> Result<Vec<StreamTrace>> {
    let (traces, _) =
        collect_stream_traces_with_observations(config, target_ms, tolerance_ms, warnings)?;
    Ok(traces)
}

fn collect_stream_traces_with_observations(
    config: &MonitorConfig,
    target_ms: u64,
    tolerance_ms: u64,
    warnings: &mut Vec<String>,
) -> Result<(Vec<StreamTrace>, Vec<TbtObservation>)> {
    let mut traces = Vec::new();
    let mut observations = Vec::new();

    for device in &config.devices {
        let mut conn = match connect_device(&device.redis) {
            Ok(conn) => conn,
            Err(err) => {
                warnings.push(format!(
                    "{}: failed to connect for stream reads: {err}",
                    device.bpm_ip
                ));
                continue;
            }
        };

        for stream_key in &device.stream_keys {
            let Some(plane) = classify_plane(stream_key) else {
                continue;
            };

            let entry = match fetch_entry_near_target(
                &mut conn,
                stream_key,
                target_ms,
                tolerance_ms,
                DEFAULT_XRANGE_COUNT,
            ) {
                Ok(v) => v,
                Err(err) => {
                    warnings.push(format!(
                        "{}: failed reading {} near {} ms: {err}",
                        device.bpm_ip, stream_key, target_ms
                    ));
                    continue;
                }
            };

            let Some((id, fields)) = entry else {
                continue;
            };

            let Some((ms, _)) = parse_stream_id(&id) else {
                warnings.push(format!(
                    "{}: stream id {} for {} is not parseable",
                    device.bpm_ip, id, stream_key
                ));
                continue;
            };
            observations.push(TbtObservation {
                bpm_ip: device.bpm_ip.clone(),
                stream_key: stream_key.clone(),
                id: id.clone(),
                ms,
                aligned: abs_diff_u64(ms, target_ms) <= tolerance_ms,
            });

            let samples = match decode_f32_payload(&fields) {
                Ok(v) => v,
                Err(err) => {
                    warnings.push(format!(
                        "{}: malformed payload in {} ({id}): {err}",
                        device.bpm_ip, stream_key
                    ));
                    continue;
                }
            };

            traces.push(StreamTrace {
                plane,
                bpm_ip: device.bpm_ip.clone(),
                stream_key: stream_key.clone(),
                samples,
            });
        }
    }

    Ok((traces, observations))
}

fn collect_tbt_stream_keys(device: &DeviceConfig) -> Vec<String> {
    let mut keys = Vec::new();
    let mut seen = HashMap::<String, bool>::new();

    for key in &device.stream_keys {
        if classify_plane(key).is_none() {
            continue;
        }
        if !seen.contains_key(key) {
            seen.insert(key.clone(), true);
            keys.push(key.clone());
        }
    }

    keys
}

fn initialize_read_ids(conn: &mut Connection, keys: &[String], bpm_ip: &str) -> Vec<String> {
    let mut read_ids = Vec::with_capacity(keys.len());

    for key in keys {
        match fetch_latest_entry(conn, key) {
            Ok(Some((id, _))) => read_ids.push(id),
            Ok(None) => read_ids.push("$".to_string()),
            Err(err) => {
                eprintln!(
                    "[free-run {}] failed reading baseline id for {}: {}",
                    bpm_ip, key, err
                );
                read_ids.push("$".to_string());
            }
        }
    }

    read_ids
}

fn wait_for_next_device_event(
    conn: &mut Connection,
    keys: &[String],
    read_ids: &mut [String],
) -> Result<Option<DeviceEvent>> {
    if keys.is_empty() || read_ids.is_empty() || keys.len() != read_ids.len() {
        bail!("wait_for_next_device_event received invalid stream key/read-id state");
    }

    let key_index = keys
        .iter()
        .enumerate()
        .map(|(idx, key)| (key.as_str(), idx))
        .collect::<HashMap<_, _>>();

    let options = StreamReadOptions::default().block(0).count(64);
    let reply: StreamReadReply = conn
        .xread_options(keys, read_ids, &options)
        .with_context(|| format!("XREAD failed for {} stream keys", keys.len()))?;

    let mut newest_id: Option<String> = None;
    let mut newest_ms: Option<u64> = None;

    for key_reply in reply.keys {
        let Some(&idx) = key_index.get(key_reply.key.as_str()) else {
            continue;
        };

        for id in key_reply.ids {
            if compare_stream_ids(id.id.as_str(), read_ids[idx].as_str()) == Ordering::Greater {
                read_ids[idx] = id.id.clone();
            }

            if let Some((ms, _)) = parse_stream_id(&id.id) {
                match newest_id.as_ref() {
                    Some(current) => {
                        if compare_stream_ids(&id.id, current.as_str()) == Ordering::Greater {
                            newest_id = Some(id.id.clone());
                            newest_ms = Some(ms);
                        }
                    }
                    None => {
                        newest_id = Some(id.id.clone());
                        newest_ms = Some(ms);
                    }
                }
            }
        }
    }

    match (newest_id, newest_ms) {
        (Some(id), Some(ms)) => Ok(Some(DeviceEvent { id, ms })),
        _ => Ok(None),
    }
}

fn analyze_plane(
    plane: Plane,
    traces: Vec<StreamTrace>,
    config: &MonitorConfig,
    flash_count: Option<usize>,
    warnings: &mut Vec<String>,
) -> Result<Option<PlaneAnalysis>> {
    if traces.is_empty() {
        return Ok(None);
    }

    let traces_total = traces.len();
    let Some(consensus_turns) = consensus_length(&traces) else {
        return Ok(None);
    };

    let mut filtered = traces
        .into_iter()
        .filter(|trace| trace.samples.len() == consensus_turns)
        .collect::<Vec<_>>();

    let injection_window_turns = effective_injection_window_turns(config, flash_count);

    let required_turns = config
        .injection_start_turn
        .saturating_add(injection_window_turns)
        .max(config.sliding_window_turns);

    if consensus_turns < required_turns {
        warnings.push(format!(
            "plane {} consensus turn count {} is smaller than required {}",
            plane.label(),
            consensus_turns,
            required_turns
        ));
        return Ok(None);
    }

    filtered.retain(|trace| {
        trace.samples.len() >= required_turns && trace.samples.len() == consensus_turns
    });

    if filtered.is_empty() {
        warnings.push(format!(
            "plane {} has no traces after consensus/length filtering",
            plane.label()
        ));
        return Ok(None);
    }

    let mut participating_bpms = filtered
        .iter()
        .map(|trace| trace.stream_key.clone())
        .collect::<Vec<_>>();
    participating_bpms.sort_unstable();
    participating_bpms.dedup();

    let mut max_rms_bpm: Option<f64> = None;
    let mut best_bpm_stream: Option<String> = None;
    let mut best_bpm_conf = f64::NEG_INFINITY;

    for trace in &filtered {
        if let Some(rms) =
            trace_window_rms(trace, config.injection_start_turn, injection_window_turns)
        {
            max_rms_bpm = Some(max_rms_bpm.map_or(rms, |v| v.max(rms)));
        }
        if let Some(spectrum) =
            compute_trace_spectrum(trace, config.injection_start_turn, injection_window_turns)
        {
            if let Some(peak) = pick_peak_in_band(
                &spectrum,
                plane.tune_band(config),
                config.min_peak_confidence,
            ) {
                if peak.confidence > best_bpm_conf {
                    best_bpm_conf = peak.confidence;
                    best_bpm_stream = Some(trace.stream_key.clone());
                }
            }
        }
    }

    let injection_spectrum = average_spectrum(
        &filtered,
        config.injection_start_turn,
        injection_window_turns,
    )
    .with_context(|| format!("failed injection spectrum for plane {}", plane.label()))?;

    let injection_peak = pick_peak_in_band(
        &injection_spectrum,
        plane.tune_band(config),
        config.min_peak_confidence,
    );
    if injection_peak.is_none() {
        warnings.push(format!(
            "plane {} had no injection peak in configured tune band",
            plane.label()
        ));
    }

    let (sliding, sliding_spectra, diagnostics) = compute_sliding_tunes(
        &filtered,
        consensus_turns,
        config.sliding_window_turns,
        config.sliding_stride_turns,
        flash_count,
        plane.tune_band(config),
        injection_peak.as_ref().map(|peak| peak.tune),
        config.enable_peak_tracking,
        plane.track_half_width(config),
        config.max_tune_step_per_window,
        config.min_peak_confidence,
    )?;

    if let Some(requested) = flash_count {
        let expected =
            resolved_flash_count(requested, consensus_turns, config.sliding_window_turns);
        if diagnostics.total_windows < expected {
            let requested_label = if is_flash_max_request(requested) {
                "max".to_string()
            } else {
                requested.to_string()
            };
            warnings.push(format!(
                "plane {} flash sampling reduced from requested {} to {} windows (consensus_turns={}, sliding_window_turns={})",
                plane.label(),
                requested_label,
                diagnostics.total_windows,
                consensus_turns,
                config.sliding_window_turns
            ));
        }
    }

    if diagnostics.fallback_count > 0 {
        warnings.push(format!(
            "plane {} sliding fallback used in {}/{} windows",
            plane.label(),
            diagnostics.fallback_count,
            diagnostics.total_windows
        ));
    }
    if diagnostics.suspicious_count > 0 {
        warnings.push(format!(
            "plane {} sliding suspicious-step windows: {}/{} (max_step={:.6})",
            plane.label(),
            diagnostics.suspicious_count,
            diagnostics.total_windows,
            config.max_tune_step_per_window
        ));
    }
    if diagnostics.missing_seed_count > 0 {
        warnings.push(format!(
            "plane {} sliding missing-seed windows: {}/{}",
            plane.label(),
            diagnostics.missing_seed_count,
            diagnostics.total_windows
        ));
    }

    Ok(Some(PlaneAnalysis {
        plane,
        traces_total,
        traces_used: filtered.len(),
        consensus_turns,
        participating_bpms,
        best_bpm_stream,
        max_rms_bpm,
        injection_spectrum,
        injection_peak,
        sliding,
        sliding_spectra,
        sliding_fallback_count: diagnostics.fallback_count,
        sliding_suspicious_count: diagnostics.suspicious_count,
    }))
}

fn compute_sliding_tunes(
    traces: &[StreamTrace],
    total_turns: usize,
    window_turns: usize,
    stride_turns: usize,
    flash_count: Option<usize>,
    band: (f64, f64),
    seed_tune: Option<f64>,
    enable_tracking: bool,
    track_half_width: f64,
    max_step_per_window: f64,
    min_peak_confidence: f64,
) -> Result<(Vec<SlidingPoint>, Vec<Vec<f64>>, SlidingDiagnostics)> {
    if window_turns > total_turns {
        return Ok((
            Vec::new(),
            Vec::new(),
            SlidingDiagnostics {
                fallback_count: 0,
                suspicious_count: 0,
                missing_seed_count: 0,
                total_windows: 0,
            },
        ));
    }

    let mut points = Vec::new();
    let mut spectra = Vec::<Vec<f64>>::new();
    let starts = sliding_window_starts(total_turns, window_turns, stride_turns, flash_count);
    if starts.is_empty() {
        return Ok((
            Vec::new(),
            Vec::new(),
            SlidingDiagnostics {
                fallback_count: 0,
                suspicious_count: 0,
                missing_seed_count: 0,
                total_windows: 0,
            },
        ));
    }
    let mut previous_trusted_tune = seed_tune;
    let mut fallback_count = 0usize;
    let mut suspicious_count = 0usize;
    let mut missing_seed_count = 0usize;

    for start in starts {
        let spectrum = average_spectrum(traces, start, window_turns)?;
        let raw_peak = pick_peak_in_band(&spectrum, band, min_peak_confidence);

        let mut tracked_local_peak: Option<PeakResult> = None;
        let selected_peak: Option<PeakResult>;
        let mut used_global_fallback = false;
        let mut suspicious_step = false;
        let mut step_delta = None;

        if !enable_tracking {
            selected_peak = raw_peak.clone();
        } else if let Some(trusted) = previous_trusted_tune {
            if let Some(local_band) = local_tracking_band(band, trusted, track_half_width) {
                tracked_local_peak = pick_peak_in_band(&spectrum, local_band, min_peak_confidence);
            }

            if tracked_local_peak.is_some() {
                selected_peak = tracked_local_peak.clone();
            } else {
                selected_peak = raw_peak.clone();
                if selected_peak.is_some() {
                    used_global_fallback = true;
                    fallback_count += 1;
                }
            }

            if let Some(candidate) = selected_peak.as_ref() {
                let delta = (candidate.tune - trusted).abs();
                step_delta = Some(delta);
                if delta > max_step_per_window {
                    suspicious_step = true;
                    suspicious_count += 1;
                } else if !used_global_fallback {
                    // Keep tracker anchored to local, smooth evolution only.
                    previous_trusted_tune = Some(candidate.tune);
                }
            }
        } else {
            selected_peak = raw_peak.clone();
            missing_seed_count += 1;
        }

        points.push(SlidingPoint {
            center_turn: start + window_turns / 2,
            raw_global_tune: raw_peak.as_ref().map(|peak| peak.tune),
            tracked_local_tune: tracked_local_peak.as_ref().map(|peak| peak.tune),
            selected_tune: selected_peak.as_ref().map(|peak| peak.tune),
            raw_global_confidence: raw_peak.as_ref().map(|peak| peak.confidence),
            selected_confidence: selected_peak.as_ref().map(|peak| peak.confidence),
            used_global_fallback,
            suspicious_step,
            step_delta,
        });
        spectra.push(spectrum);
    }

    let total_windows = points.len();
    Ok((
        points,
        spectra,
        SlidingDiagnostics {
            fallback_count,
            suspicious_count,
            missing_seed_count,
            total_windows,
        },
    ))
}

fn sliding_window_starts(
    total_turns: usize,
    window_turns: usize,
    stride_turns: usize,
    flash_count: Option<usize>,
) -> Vec<usize> {
    if window_turns == 0 || window_turns > total_turns {
        return Vec::new();
    }

    let last_start = total_turns - window_turns;
    if let Some(requested) = flash_count {
        let effective = resolved_flash_count(requested, total_turns, window_turns);
        return flash_window_starts(total_turns, window_turns, last_start, effective);
    }

    (0..=last_start)
        .step_by(stride_turns.max(1))
        .collect::<Vec<_>>()
}

fn flash_window_starts(
    total_turns: usize,
    window_turns: usize,
    last_start: usize,
    flash_count: usize,
) -> Vec<usize> {
    let half_window = window_turns / 2;
    let min_center = half_window;
    let max_center = last_start + half_window;
    let mut starts = Vec::<usize>::new();

    for idx in 0..flash_count {
        let numerator = (2usize.saturating_mul(idx)).saturating_add(1);
        let denom = 2usize.saturating_mul(flash_count).max(1);
        let center = numerator.saturating_mul(total_turns) / denom;
        let clamped_center = center.clamp(min_center, max_center);
        let start = clamped_center.saturating_sub(half_window).min(last_start);
        if starts.last().copied() != Some(start) {
            starts.push(start);
        }
    }

    if starts.is_empty() {
        starts.push(0);
    }
    starts
}

fn local_tracking_band(
    global_band: (f64, f64),
    trusted: f64,
    half_width: f64,
) -> Option<(f64, f64)> {
    if !trusted.is_finite() || !half_width.is_finite() || half_width <= 0.0 {
        return None;
    }

    let local_min = (trusted - half_width).max(global_band.0);
    let local_max = (trusted + half_width).min(global_band.1);
    if local_max <= local_min {
        return None;
    }
    Some((local_min, local_max))
}

fn average_spectrum(traces: &[StreamTrace], start: usize, window_turns: usize) -> Result<Vec<f64>> {
    if window_turns == 0 {
        bail!("window_turns must be >= 1");
    }

    let hann = hann_window(window_turns);
    let mut accum = vec![0.0f64; window_turns];
    let mut used = 0usize;

    for trace in traces {
        if start + window_turns > trace.samples.len() {
            continue;
        }

        let window = &trace.samples[start..start + window_turns];
        let mean = window.iter().sum::<f64>() / window_turns as f64;

        let mut signal = Vec::with_capacity(window_turns);
        for (idx, value) in window.iter().enumerate() {
            signal.push((value - mean) * hann[idx]);
        }

        let mut spectrum = spectrum_power(&signal);
        if let Some(dc_bin) = spectrum.first_mut() {
            // Explicitly suppress residual DC contribution before accumulation/peak search.
            *dc_bin = 0.0;
        }
        for (idx, power) in spectrum.into_iter().enumerate() {
            accum[idx] += power;
        }
        used += 1;
    }

    if used == 0 {
        bail!("no traces had enough turns for requested window");
    }

    for value in &mut accum {
        *value /= used as f64;
    }

    Ok(accum)
}

fn pick_peak_in_band(
    spectrum: &[f64],
    band: (f64, f64),
    min_peak_confidence: f64,
) -> Option<PeakResult> {
    let n = spectrum.len();
    if n < 8 {
        return None;
    }

    let mut start_idx = (band.0 * n as f64).floor() as usize;
    let mut end_idx = (band.1 * n as f64).ceil() as usize;

    start_idx = start_idx.clamp(MIN_PEAK_SEARCH_BIN, n.saturating_sub(2));
    end_idx = end_idx.clamp(2, n.saturating_sub(1));

    if end_idx <= start_idx {
        return None;
    }

    let mut best_idx = None;
    let mut best_power = f64::NEG_INFINITY;

    for idx in start_idx..end_idx {
        let power = spectrum[idx];
        if power > best_power {
            best_power = power;
            best_idx = Some(idx);
        }
    }

    let idx = best_idx?;
    if !best_power.is_finite() {
        return None;
    }

    let mut refined_idx = idx as f64;
    if idx > 0 && idx + 1 < n {
        let y1 = spectrum[idx - 1];
        let y2 = spectrum[idx];
        let y3 = spectrum[idx + 1];
        let denom = y1 - 2.0 * y2 + y3;
        if denom.abs() > 1e-15 {
            let delta = 0.5 * (y1 - y3) / denom;
            if delta.is_finite() && delta.abs() <= 1.0 {
                refined_idx += delta;
            }
        }
    }

    let tune = (refined_idx / n as f64).clamp(0.0, 1.0);

    let mut band_values = spectrum[start_idx..end_idx].to_vec();
    band_values.sort_by(|a, b| a.partial_cmp(b).unwrap_or(Ordering::Equal));
    let median = band_values[band_values.len() / 2].max(1e-12);
    let confidence = best_power / median;
    if confidence < min_peak_confidence {
        return None;
    }

    Some(PeakResult {
        tune,
        confidence,
        peak_power: best_power,
        median_power: median,
        prominence: best_power - median,
    })
}

fn consensus_length(traces: &[StreamTrace]) -> Option<usize> {
    let mut counts = HashMap::<usize, usize>::new();
    for trace in traces {
        *counts.entry(trace.samples.len()).or_insert(0) += 1;
    }

    counts
        .into_iter()
        .max_by(|(len_a, count_a), (len_b, count_b)| {
            count_a.cmp(count_b).then_with(|| len_a.cmp(len_b))
        })
        .map(|(len, _)| len)
}

fn connect_device(redis_cfg: &RedisConfig) -> Result<Connection> {
    let client = redis::Client::open(redis_cfg.to_url())
        .with_context(|| format!("failed to open redis client {}", redis_cfg.to_url()))?;
    client
        .get_connection()
        .with_context(|| format!("failed to connect to {}", redis_cfg.display_addr()))
}

type XRangeReply = Vec<(String, Vec<(Vec<u8>, Vec<u8>)>)>;

fn fetch_latest_entry(
    conn: &mut Connection,
    key: &str,
) -> Result<Option<(String, Vec<(Vec<u8>, Vec<u8>)>)>> {
    let reply: XRangeReply = redis::cmd("XREVRANGE")
        .arg(key)
        .arg("+")
        .arg("-")
        .arg("COUNT")
        .arg(1)
        .query(conn)
        .with_context(|| format!("XREVRANGE failed for key {key}"))?;

    Ok(reply.into_iter().next())
}

fn fetch_recent_entries(conn: &mut Connection, key: &str, count: usize) -> Result<XRangeReply> {
    let reply: XRangeReply = redis::cmd("XREVRANGE")
        .arg(key)
        .arg("+")
        .arg("-")
        .arg("COUNT")
        .arg(count.max(1))
        .query(conn)
        .with_context(|| format!("XREVRANGE failed for key {key}"))?;

    Ok(reply)
}

fn fetch_entry_near_target(
    conn: &mut Connection,
    key: &str,
    target_ms: u64,
    tolerance_ms: u64,
    count: usize,
) -> Result<Option<(String, Vec<(Vec<u8>, Vec<u8>)>)>> {
    let start_ms = target_ms.saturating_sub(tolerance_ms);
    let end_ms = target_ms.saturating_add(tolerance_ms);

    let start_id = format!("{}-0", start_ms);
    let end_id = format!("{}-18446744073709551615", end_ms);

    let reply: XRangeReply = redis::cmd("XRANGE")
        .arg(key)
        .arg(&start_id)
        .arg(&end_id)
        .arg("COUNT")
        .arg(count.max(1))
        .query(conn)
        .with_context(|| format!("XRANGE failed for key {key}"))?;

    if reply.is_empty() {
        return Ok(None);
    }

    let mut best: Option<(String, Vec<(Vec<u8>, Vec<u8>)>, u64)> = None;

    for (id, fields) in reply {
        let Some((ms, _)) = parse_stream_id(&id) else {
            continue;
        };
        let diff = abs_diff_u64(ms, target_ms);

        match &best {
            Some((best_id, _, best_diff)) => {
                if diff < *best_diff
                    || (diff == *best_diff && compare_stream_ids(&id, best_id) == Ordering::Greater)
                {
                    best = Some((id, fields, diff));
                }
            }
            None => best = Some((id, fields, diff)),
        }
    }

    Ok(best.map(|(id, fields, _)| (id, fields)))
}

fn decode_f32_payload(fields: &[(Vec<u8>, Vec<u8>)]) -> Result<Vec<f64>> {
    let payload = fields
        .iter()
        .find(|(k, _)| k.as_slice() == b"_")
        .map(|(_, v)| v.as_slice())
        .ok_or_else(|| anyhow!("missing '_' payload field"))?;

    decode_f32_payload_bytes(payload)
}

fn decode_f32_payload_bytes(payload: &[u8]) -> Result<Vec<f64>> {
    if payload.is_empty() {
        bail!("payload is empty");
    }

    if payload.len() % 4 != 0 {
        bail!("payload length {} is not divisible by 4", payload.len());
    }

    let mut out = Vec::with_capacity(payload.len() / 4);
    for chunk in payload.chunks_exact(4) {
        out.push(f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]) as f64);
    }

    Ok(out)
}

fn fnv1a64_hex(bytes: &[u8]) -> String {
    let mut hash = 0xcbf29ce484222325u64;
    for byte in bytes {
        hash ^= *byte as u64;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}

fn classify_plane(key: &str) -> Option<Plane> {
    if key.contains(":HP") && key.ends_with(":TBT_POSITION_SCALED") {
        Some(Plane::Horizontal)
    } else if key.contains(":VP") && key.ends_with(":TBT_POSITION_SCALED") {
        Some(Plane::Vertical)
    } else {
        None
    }
}

fn choose_target_millisecond(values: &[u64], merge_tolerance_ms: u64) -> Option<u64> {
    if values.is_empty() {
        return None;
    }

    let mut counts = HashMap::<u64, usize>::new();
    for value in values {
        *counts.entry(*value).or_insert(0) += 1;
    }

    // Cluster neighboring millisecond buckets so one physical spill is not split
    // across adjacent stream-id timestamps.
    let mut unique_ms = counts.keys().copied().collect::<Vec<_>>();
    unique_ms.sort_unstable();

    let mut clusters = Vec::<Vec<u64>>::new();
    for ms in unique_ms {
        if let Some(cluster) = clusters.last_mut() {
            let last_ms = *cluster.last().expect("cluster has at least one timestamp");
            if ms.saturating_sub(last_ms) <= merge_tolerance_ms {
                cluster.push(ms);
                continue;
            }
        }
        clusters.push(vec![ms]);
    }

    // Pick the cluster with the highest aggregate support, then pick that
    // cluster's strongest (and newest on ties) representative millisecond.
    let mut best: Option<(usize, u64)> = None;
    for cluster in clusters {
        let mut total_count = 0usize;
        let mut representative_ms = 0u64;
        let mut representative_count = 0usize;

        for ms in cluster {
            let count = counts.get(&ms).copied().unwrap_or(0);
            total_count += count;
            if count > representative_count
                || (count == representative_count && ms > representative_ms)
            {
                representative_count = count;
                representative_ms = ms;
            }
        }

        match best {
            Some((best_total, best_ms))
                if total_count < best_total
                    || (total_count == best_total && representative_ms <= best_ms) => {}
            _ => best = Some((total_count, representative_ms)),
        }
    }

    best.map(|(_, ms)| ms)
}

fn parse_stream_id(id: &str) -> Option<(u64, u64)> {
    let (ms, sub) = id.split_once('-')?;
    Some((ms.parse::<u64>().ok()?, sub.parse::<u64>().ok()?))
}

fn compare_stream_ids(a: &str, b: &str) -> Ordering {
    match (parse_stream_id(a), parse_stream_id(b)) {
        (Some(a_parts), Some(b_parts)) => a_parts.cmp(&b_parts),
        _ => a.cmp(b),
    }
}

fn abs_diff_u64(a: u64, b: u64) -> u64 {
    a.max(b) - a.min(b)
}

fn target_bucket_tolerance_ms(config: &MonitorConfig) -> u64 {
    // Keep clustering tolerance conservative and bounded by alignment policy.
    config.align_tolerance_ms.min(ADJACENT_BUCKET_TOLERANCE_MS)
}

fn target_seen_within_tolerance(seen: &HashSet<u64>, target_ms: u64, tolerance_ms: u64) -> bool {
    seen.iter()
        .any(|seen_ms| abs_diff_u64(*seen_ms, target_ms) <= tolerance_ms)
}

fn signed_delta_ms(ms: u64, target_ms: u64) -> i64 {
    if ms >= target_ms {
        ms.saturating_sub(target_ms).min(i64::MAX as u64) as i64
    } else {
        -(target_ms.saturating_sub(ms).min(i64::MAX as u64) as i64)
    }
}

fn timeliness_stats(observations: &[TbtObservation], target_ms: u64) -> Option<TimelinessStats> {
    if observations.is_empty() {
        return None;
    }

    let mut min_delta = i64::MAX;
    let mut max_delta = i64::MIN;
    let mut abs_deltas = Vec::<f64>::with_capacity(observations.len());
    let mut max_abs_delta = 0u64;

    for obs in observations {
        let delta = signed_delta_ms(obs.ms, target_ms);
        min_delta = min_delta.min(delta);
        max_delta = max_delta.max(delta);
        let abs_delta = abs_diff_u64(obs.ms, target_ms);
        max_abs_delta = max_abs_delta.max(abs_delta);
        abs_deltas.push(abs_delta as f64);
    }

    Some(TimelinessStats {
        min_delta_ms: min_delta,
        max_delta_ms: max_delta,
        median_abs_delta_ms: median(&abs_deltas).unwrap_or(0.0),
        max_abs_delta_ms: max_abs_delta,
    })
}

fn hann_window(n: usize) -> Vec<f64> {
    if n <= 1 {
        return vec![1.0; n];
    }
    (0..n)
        .map(|idx| 0.5 - 0.5 * ((2.0 * std::f64::consts::PI * idx as f64) / (n as f64 - 1.0)).cos())
        .collect()
}

#[derive(Clone, Copy, Debug)]
struct Complex64 {
    re: f64,
    im: f64,
}

impl Complex64 {
    fn new(re: f64, im: f64) -> Self {
        Self { re, im }
    }

    fn norm_sqr(self) -> f64 {
        self.re * self.re + self.im * self.im
    }
}

fn spectrum_power(signal: &[f64]) -> Vec<f64> {
    let mut buffer = signal
        .iter()
        .map(|v| Complex64::new(*v, 0.0))
        .collect::<Vec<_>>();

    if signal.len().is_power_of_two() {
        fft_radix2_in_place(&mut buffer);
    } else {
        buffer = dft(&buffer);
    }

    buffer.into_iter().map(Complex64::norm_sqr).collect()
}

fn fft_radix2_in_place(buffer: &mut [Complex64]) {
    let n = buffer.len();
    if n <= 1 {
        return;
    }

    let mut j = 0usize;
    for i in 1..n {
        let mut bit = n >> 1;
        while j & bit != 0 {
            j ^= bit;
            bit >>= 1;
        }
        j ^= bit;
        if i < j {
            buffer.swap(i, j);
        }
    }

    let mut len = 2usize;
    while len <= n {
        let theta = -2.0 * std::f64::consts::PI / len as f64;
        let wlen = Complex64::new(theta.cos(), theta.sin());

        let mut i = 0usize;
        while i < n {
            let mut w = Complex64::new(1.0, 0.0);
            for j in 0..(len / 2) {
                let u = buffer[i + j];
                let v = complex_mul(buffer[i + j + len / 2], w);

                buffer[i + j] = Complex64::new(u.re + v.re, u.im + v.im);
                buffer[i + j + len / 2] = Complex64::new(u.re - v.re, u.im - v.im);

                w = complex_mul(w, wlen);
            }
            i += len;
        }

        len <<= 1;
    }
}

fn dft(input: &[Complex64]) -> Vec<Complex64> {
    let n = input.len();
    let mut output = vec![Complex64::new(0.0, 0.0); n];

    for (k, out) in output.iter_mut().enumerate().take(n) {
        let mut sum = Complex64::new(0.0, 0.0);
        for (n_idx, value) in input.iter().enumerate() {
            let angle = -2.0 * std::f64::consts::PI * (k as f64) * (n_idx as f64) / n as f64;
            let twiddle = Complex64::new(angle.cos(), angle.sin());
            let term = complex_mul(*value, twiddle);
            sum.re += term.re;
            sum.im += term.im;
        }
        *out = sum;
    }

    output
}

fn complex_mul(a: Complex64, b: Complex64) -> Complex64 {
    Complex64::new(a.re * b.re - a.im * b.im, a.re * b.im + a.im * b.re)
}

fn write_spectrum_png(
    path: &Path,
    spectrum: &[f64],
    band: (f64, f64),
    peak_tune: Option<f64>,
) -> Result<()> {
    let mut image = RgbImage::new(1280, 720);
    image.fill([255, 255, 255]);

    let bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 20,
        bottom: 60,
    };

    draw_axes(&mut image, bounds, [0, 0, 0]);

    let max_y = spectrum
        .iter()
        .copied()
        .filter(|v| v.is_finite())
        .fold(0.0f64, f64::max)
        .max(1.0);

    draw_spectrum_ticks(&mut image, bounds, max_y);

    draw_vertical_marker(&mut image, bounds, band.0, [0, 180, 0]);
    draw_vertical_marker(&mut image, bounds, band.1, [0, 180, 0]);

    let points = spectrum
        .iter()
        .enumerate()
        .map(|(idx, power)| {
            let x = idx as f64 / spectrum.len().max(1) as f64;
            let y = (*power / max_y).clamp(0.0, 1.0);
            (x, y)
        })
        .collect::<Vec<_>>();
    draw_polyline_normalized(&mut image, bounds, &points, [0, 70, 220]);

    if let Some(tune) = peak_tune {
        draw_vertical_marker(&mut image, bounds, tune, [220, 0, 0]);
        draw_peak_label(&mut image, bounds, tune, [220, 0, 0]);
    }

    let legend_x = image.width as i32 - bounds.right as i32 - 250;
    draw_line_legend(
        &mut image,
        (legend_x, bounds.top as i32 + 8),
        &[
            ([0, 70, 220], "SPECTRUM"),
            ([0, 180, 0], "BAND"),
            ([220, 0, 0], "PEAK"),
        ],
    );

    write_png_rgb(path, &image)
}

fn write_tune_trace_png(
    path: &Path,
    horizontal: &[SlidingPoint],
    vertical: &[SlidingPoint],
    tune_y_min: f64,
    tune_y_max: f64,
    tune_y_tick_step: f64,
    plot_time_axes_in_us: bool,
    turn_period_us: f64,
    h_injection: Option<f64>,
    v_injection: Option<f64>,
    show_flash_markers: bool,
) -> Result<()> {
    let mut image = RgbImage::new(1280, 720);
    image.fill([255, 255, 255]);

    let bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 20,
        bottom: 60,
    };

    draw_axes(&mut image, bounds, [0, 0, 0]);

    let x_max = horizontal
        .iter()
        .chain(vertical.iter())
        .map(|point| {
            if plot_time_axes_in_us {
                point.center_turn as f64 * turn_period_us
            } else {
                point.center_turn as f64
            }
        })
        .fold(1.0f64, f64::max);

    draw_trace_ticks(
        &mut image,
        bounds,
        x_max,
        tune_y_min,
        tune_y_max,
        tune_y_tick_step,
    );

    if let Some(qx_inj) = h_injection.filter(|q| q.is_finite()) {
        draw_horizontal_xy(
            &mut image,
            bounds,
            qx_inj,
            0.0,
            x_max,
            tune_y_min,
            tune_y_max,
            [170, 210, 255],
        );
    }
    if let Some(qy_inj) = v_injection.filter(|q| q.is_finite()) {
        draw_horizontal_xy(
            &mut image,
            bounds,
            qy_inj,
            0.0,
            x_max,
            tune_y_min,
            tune_y_max,
            [255, 180, 180],
        );
    }

    for segment in finite_segments(horizontal) {
        let normalized = segment
            .iter()
            .map(|(x_turn, y_tune)| {
                let x_value = if plot_time_axes_in_us {
                    x_turn * turn_period_us
                } else {
                    *x_turn
                };
                let x = (x_value / x_max).clamp(0.0, 1.0);
                let y =
                    ((y_tune - tune_y_min) / (tune_y_max - tune_y_min).max(1e-12)).clamp(0.0, 1.0);
                (x, y)
            })
            .collect::<Vec<_>>();
        draw_polyline_normalized(&mut image, bounds, &normalized, [0, 70, 220]);
        if let Some(&(x_last, y_last)) = normalized.last() {
            let (x_px, y_px) = map_point(&image, bounds, (x_last, y_last));
            draw_text_small(&mut image, x_px + 6, y_px - 6, "H", [0, 70, 220], 2);
        }
    }

    for segment in finite_segments(vertical) {
        let normalized = segment
            .iter()
            .map(|(x_turn, y_tune)| {
                let x_value = if plot_time_axes_in_us {
                    x_turn * turn_period_us
                } else {
                    *x_turn
                };
                let x = (x_value / x_max).clamp(0.0, 1.0);
                let y =
                    ((y_tune - tune_y_min) / (tune_y_max - tune_y_min).max(1e-12)).clamp(0.0, 1.0);
                (x, y)
            })
            .collect::<Vec<_>>();
        draw_polyline_normalized(&mut image, bounds, &normalized, [220, 0, 0]);
        if let Some(&(x_last, y_last)) = normalized.last() {
            let (x_px, y_px) = map_point(&image, bounds, (x_last, y_last));
            draw_text_small(&mut image, x_px + 6, y_px - 6, "V", [220, 0, 0], 2);
        }
    }

    if show_flash_markers {
        for point in horizontal {
            let Some(tune) = point.selected_tune else {
                continue;
            };
            draw_point_xy(
                &mut image,
                bounds,
                if plot_time_axes_in_us {
                    point.center_turn as f64 * turn_period_us
                } else {
                    point.center_turn as f64
                },
                tune,
                0.0,
                x_max,
                tune_y_min,
                tune_y_max,
                [0, 70, 220],
            );
            let x_value = if plot_time_axes_in_us {
                point.center_turn as f64 * turn_period_us
            } else {
                point.center_turn as f64
            };
            let x = (x_value / x_max).clamp(0.0, 1.0);
            let y = ((tune - tune_y_min) / (tune_y_max - tune_y_min).max(1e-12)).clamp(0.0, 1.0);
            let (x_px, y_px) = map_point(&image, bounds, (x, y));
            let label = if plot_time_axes_in_us {
                format_number_label(x_value)
            } else {
                point.center_turn.to_string()
            };
            draw_text_small(&mut image, x_px + 4, y_px - 18, &label, [0, 70, 220], 1);
        }
        for point in vertical {
            let Some(tune) = point.selected_tune else {
                continue;
            };
            draw_point_xy(
                &mut image,
                bounds,
                if plot_time_axes_in_us {
                    point.center_turn as f64 * turn_period_us
                } else {
                    point.center_turn as f64
                },
                tune,
                0.0,
                x_max,
                tune_y_min,
                tune_y_max,
                [220, 0, 0],
            );
            let x_value = if plot_time_axes_in_us {
                point.center_turn as f64 * turn_period_us
            } else {
                point.center_turn as f64
            };
            let x = (x_value / x_max).clamp(0.0, 1.0);
            let y = ((tune - tune_y_min) / (tune_y_max - tune_y_min).max(1e-12)).clamp(0.0, 1.0);
            let (x_px, y_px) = map_point(&image, bounds, (x, y));
            let label = if plot_time_axes_in_us {
                format_number_label(x_value)
            } else {
                point.center_turn.to_string()
            };
            draw_text_small(&mut image, x_px + 4, y_px + 6, &label, [220, 0, 0], 1);
        }
    }

    let x_label = if plot_time_axes_in_us {
        "TIME AFTER INJECTION [US]"
    } else {
        "CENTER TURN"
    };
    let x_label_w = text_width_px(x_label, 2);
    let x_label_x =
        (bounds.left + ((image.width - bounds.left - bounds.right) / 2)) as i32 - x_label_w / 2;
    let x_label_y = (image.height - 24) as i32;
    draw_text_small(&mut image, x_label_x, x_label_y, x_label, [0, 0, 0], 2);

    let legend_x = image.width as i32 - bounds.right as i32 - 160;
    let mut legend = vec![([0, 70, 220], "H"), ([220, 0, 0], "V")];
    if h_injection.is_some() {
        legend.push(([170, 210, 255], "H inj"));
    }
    if v_injection.is_some() {
        legend.push(([255, 180, 180], "V inj"));
    }
    if show_flash_markers {
        legend.push(([90, 90, 90], "Flash turns"));
    }
    draw_line_legend(&mut image, (legend_x, bounds.top as i32 + 8), &legend);

    write_png_rgb(path, &image)
}

#[derive(Debug, Clone, Copy)]
struct PanelRect {
    x0: usize,
    y0: usize,
    x1: usize,
    y1: usize,
}

impl PanelRect {
    fn width(self) -> usize {
        self.x1.saturating_sub(self.x0)
    }

    fn height(self) -> usize {
        self.y1.saturating_sub(self.y0)
    }
}

fn panel_plot_bounds(
    image: &RgbImage,
    rect: PanelRect,
    pad_left: usize,
    pad_right: usize,
    pad_top: usize,
    pad_bottom: usize,
) -> PlotBounds {
    let left = rect.x0.saturating_add(pad_left);
    let right_edge = rect.x1.saturating_sub(pad_right).max(left + 1);
    let top = rect.y0.saturating_add(pad_top);
    let bottom_edge = rect.y1.saturating_sub(pad_bottom).max(top + 1);
    PlotBounds {
        left,
        right: image.width.saturating_sub(right_edge),
        top,
        bottom: image.height.saturating_sub(bottom_edge),
    }
}

fn draw_panel_border(image: &mut RgbImage, rect: PanelRect, color: [u8; 3]) {
    if rect.width() < 2 || rect.height() < 2 {
        return;
    }
    let x0 = rect.x0 as i32;
    let y0 = rect.y0 as i32;
    let x1 = rect.x1.saturating_sub(1) as i32;
    let y1 = rect.y1.saturating_sub(1) as i32;
    image.draw_line(x0, y0, x1, y0, color);
    image.draw_line(x0, y1, x1, y1, color);
    image.draw_line(x0, y0, x0, y1, color);
    image.draw_line(x1, y0, x1, y1, color);
}

fn draw_vertical_dashed_marker(
    image: &mut RgbImage,
    bounds: PlotBounds,
    x_norm: f64,
    color: [u8; 3],
    dash_len: i32,
    gap_len: i32,
) {
    let x = x_from_norm(image, bounds, x_norm);
    let y0 = bounds.top as i32;
    let y1 = (image.height - bounds.bottom) as i32;
    let dash_len = dash_len.max(1);
    let gap_len = gap_len.max(1);
    let mut y = y0;
    while y <= y1 {
        let y_end = (y + dash_len - 1).min(y1);
        image.draw_line(x, y, x, y_end, color);
        y += dash_len + gap_len;
    }
}

fn draw_cross_marker(image: &mut RgbImage, x: i32, y: i32, color: [u8; 3], size: i32) {
    let s = size.max(1);
    image.draw_line(x - s, y - s, x + s, y + s, color);
    image.draw_line(x - s, y + s, x + s, y - s, color);
}

fn draw_circle_marker(image: &mut RgbImage, x: i32, y: i32, color: [u8; 3], radius: i32) {
    let r = radius.max(1);
    for dx in -r..=r {
        for dy in -r..=r {
            let d2 = dx * dx + dy * dy;
            let inner = (r - 1).max(0);
            if d2 <= r * r && d2 >= inner * inner {
                image.set_pixel(x + dx, y + dy, color);
            }
        }
    }
}

fn collect_tune_segments<F>(
    sliding: &[SlidingPoint],
    config: &MonitorConfig,
    mut selector: F,
) -> Vec<Vec<(f64, f64)>>
where
    F: FnMut(&SlidingPoint) -> Option<f64>,
{
    let mut segments = Vec::<Vec<(f64, f64)>>::new();
    let mut current = Vec::<(f64, f64)>::new();

    for point in sliding {
        let time_axis = time_axis_value_from_turn(point.center_turn, config);
        let Some(tune) = selector(point) else {
            if current.len() >= 2 {
                segments.push(current.clone());
            }
            current.clear();
            continue;
        };
        if !time_axis.is_finite() || !tune.is_finite() {
            if current.len() >= 2 {
                segments.push(current.clone());
            }
            current.clear();
            continue;
        }
        current.push((time_axis, tune));
    }

    if current.len() >= 2 {
        segments.push(current);
    }
    segments
}

fn draw_polyline_xy_top_down(
    image: &mut RgbImage,
    bounds: PlotBounds,
    points: &[(f64, f64)],
    x_min: f64,
    x_max: f64,
    y_min: f64,
    y_max: f64,
    color: [u8; 3],
) {
    if points.len() < 2 {
        return;
    }
    let x_span = (x_max - x_min).abs().max(1e-12);
    let y_span = (y_max - y_min).abs().max(1e-12);
    let normalized = points
        .iter()
        .map(|(x, y)| {
            let nx = ((*x - x_min) / x_span).clamp(0.0, 1.0);
            let ny = 1.0 - ((*y - y_min) / y_span).clamp(0.0, 1.0);
            (nx, ny)
        })
        .collect::<Vec<_>>();
    draw_polyline_normalized(image, bounds, &normalized, color);
}

fn draw_polyline_xy_top_down_thick(
    image: &mut RgbImage,
    bounds: PlotBounds,
    points: &[(f64, f64)],
    x_min: f64,
    x_max: f64,
    y_min: f64,
    y_max: f64,
    color: [u8; 3],
    thickness: i32,
) {
    if points.len() < 2 {
        return;
    }
    let radius = (thickness.max(1) - 1) / 2;
    let x_span = (x_max - x_min).abs().max(1e-12);
    let y_span = (y_max - y_min).abs().max(1e-12);
    let pixel_points = points
        .iter()
        .map(|(x, y)| {
            let nx = ((*x - x_min) / x_span).clamp(0.0, 1.0);
            let ny = 1.0 - ((*y - y_min) / y_span).clamp(0.0, 1.0);
            map_point(image, bounds, (nx, ny))
        })
        .collect::<Vec<_>>();

    for segment in pixel_points.windows(2) {
        let a = segment[0];
        let b = segment[1];
        for dx in -radius..=radius {
            for dy in -radius..=radius {
                if dx.abs() + dy.abs() > radius.max(1) {
                    continue;
                }
                image.draw_line(a.0 + dx, a.1 + dy, b.0 + dx, b.1 + dy, color);
            }
        }
    }
}

fn draw_validation_spectrogram_panel(
    image: &mut RgbImage,
    rect: PanelRect,
    plane: Plane,
    analysis: Option<&PlaneAnalysis>,
    config: &MonitorConfig,
    expected_tune: Option<f64>,
) {
    draw_panel_border(image, rect, [0, 0, 0]);
    let title = match plane {
        Plane::Horizontal => "H TUNE VALIDATION",
        Plane::Vertical => "V TUNE VALIDATION",
    };
    draw_text_small(
        image,
        rect.x0 as i32 + 8,
        rect.y0 as i32 + 8,
        title,
        [0, 0, 0],
        2,
    );

    let bounds = panel_plot_bounds(image, rect, 76, 18, 32, 58);
    draw_axes(image, bounds, [0, 0, 0]);
    let x0 = bounds.left;
    let x1 = image.width.saturating_sub(bounds.right);
    let y0 = bounds.top;
    let y1 = image.height.saturating_sub(bounds.bottom);
    let plot_w = x1.saturating_sub(x0).max(1);
    let plot_h = y1.saturating_sub(y0).max(1);

    let x_label = "TUNE";
    let x_label_w = text_width_px(x_label, 2);
    draw_text_small(
        image,
        (x0 + plot_w / 2) as i32 - x_label_w / 2,
        y1 as i32 + 28,
        x_label,
        [0, 0, 0],
        2,
    );
    draw_text_small(
        image,
        rect.x0 as i32 + 8,
        rect.y0 as i32 + 24,
        time_axis_label(config),
        [0, 0, 0],
        2,
    );

    let Some(analysis) = analysis else {
        draw_text_small(
            image,
            (x0 + 10) as i32,
            (y0 + plot_h / 2) as i32,
            "NO USABLE PLANE DATA",
            [120, 120, 120],
            2,
        );
        return;
    };

    let rows = analysis.sliding_spectra.len().min(analysis.sliding.len());
    if rows == 0 {
        draw_text_small(
            image,
            (x0 + 10) as i32,
            (y0 + plot_h / 2) as i32,
            "NO SLIDING WINDOWS",
            [120, 120, 120],
            2,
        );
        return;
    }

    let n_bins = analysis
        .sliding_spectra
        .iter()
        .take(rows)
        .map(|row| row.len())
        .min()
        .unwrap_or(0);
    if n_bins == 0 {
        draw_text_small(
            image,
            (x0 + 10) as i32,
            (y0 + plot_h / 2) as i32,
            "EMPTY SPECTRUM BINS",
            [120, 120, 120],
            2,
        );
        return;
    }

    let tune_min = config.tune_plot_y_min.clamp(0.0, 1.0);
    let tune_max = config.tune_plot_y_max.clamp(tune_min + 1e-6, 1.0);
    let bin_start = ((tune_min * n_bins as f64).floor() as usize).min(n_bins - 1);
    let mut bin_end = ((tune_max * n_bins as f64).ceil() as usize).max(bin_start + 1);
    bin_end = bin_end.min(n_bins);

    let mut log_values = Vec::<f64>::new();
    for row in analysis.sliding_spectra.iter().take(rows) {
        for power in &row[bin_start..bin_end] {
            if power.is_finite() {
                log_values.push((power.max(1e-12)).log10());
            }
        }
    }
    if log_values.is_empty() {
        draw_text_small(
            image,
            (x0 + 10) as i32,
            (y0 + plot_h / 2) as i32,
            "NO FINITE POWER VALUES",
            [120, 120, 120],
            2,
        );
        return;
    }
    log_values.sort_by(|a, b| a.partial_cmp(b).unwrap_or(Ordering::Equal));
    let lo_idx = ((log_values.len() - 1) as f64 * 0.05).round() as usize;
    let hi_idx = ((log_values.len() - 1) as f64 * 0.995).round() as usize;
    let log_lo = log_values[lo_idx.min(log_values.len() - 1)];
    let log_hi = log_values[hi_idx.min(log_values.len() - 1)].max(log_lo + 1e-9);
    let log_span = (log_hi - log_lo).max(1e-9);

    let mut bins_by_x = Vec::<usize>::with_capacity(plot_w);
    for px in x0..x1 {
        let x_norm = ((px - x0) as f64 / plot_w as f64).clamp(0.0, 1.0);
        let tune = tune_min + (tune_max - tune_min) * x_norm;
        let mut bin = (tune * n_bins as f64).floor() as usize;
        bin = bin.clamp(bin_start, bin_end.saturating_sub(1));
        bins_by_x.push(bin);
    }

    for row_idx in 0..rows {
        let row = &analysis.sliding_spectra[row_idx];
        let row_y0 = y0 + (row_idx * plot_h) / rows;
        let mut row_y1 = y0 + ((row_idx + 1) * plot_h) / rows;
        if row_y1 <= row_y0 {
            row_y1 = row_y0 + 1;
        }
        for (x_offset, px) in (x0..x1).enumerate() {
            let bin = bins_by_x[x_offset];
            let power = row.get(bin).copied().unwrap_or(0.0).max(1e-12);
            let log_power = power.log10();
            let norm = ((log_power - log_lo) / log_span).clamp(0.0, 1.0);
            let color = heatmap_color(norm);
            for py in row_y0..row_y1.min(y1) {
                image.set_pixel(px as i32, py as i32, color);
            }
        }
    }

    for row_idx in 0..rows {
        let row_y0 = y0 + (row_idx * plot_h) / rows;
        let mut row_y1 = y0 + ((row_idx + 1) * plot_h) / rows;
        if row_y1 <= row_y0 {
            row_y1 = row_y0 + 1;
        }
        let y_mid = ((row_y0 + row_y1.min(y1)) / 2) as i32;
        image.draw_line(x0 as i32 - 3, y_mid, x0 as i32, y_mid, [120, 120, 120]);
        if rows <= 120 {
            image.draw_line(x0 as i32 + 1, y_mid, x1 as i32, y_mid, [245, 245, 245]);
        }
    }

    image.draw_line(x0 as i32, y0 as i32, x1 as i32, y0 as i32, [0, 0, 0]);
    image.draw_line(x0 as i32, y1 as i32, x1 as i32, y1 as i32, [0, 0, 0]);
    image.draw_line(x0 as i32, y0 as i32, x0 as i32, y1 as i32, [0, 0, 0]);
    image.draw_line(x1 as i32, y0 as i32, x1 as i32, y1 as i32, [0, 0, 0]);

    for i in 0..=6 {
        let x_norm = i as f64 / 6.0;
        let x = (x0 as f64 + x_norm * plot_w as f64).round() as i32;
        image.draw_line(x, y1 as i32, x, y1 as i32 + 5, [80, 80, 80]);
        let value = tune_min + (tune_max - tune_min) * x_norm;
        let label = format!("{value:.3}");
        let w = text_width_px(&label, 2);
        draw_text_small(image, x - w / 2, y1 as i32 + 8, &label, [0, 0, 0], 2);
    }

    let first_time_axis = analysis
        .sliding
        .first()
        .map(|point| time_axis_value_from_turn(point.center_turn, config))
        .unwrap_or(0.0);
    let last_time_axis = analysis
        .sliding
        .get(rows.saturating_sub(1))
        .map(|point| time_axis_value_from_turn(point.center_turn, config))
        .unwrap_or(first_time_axis);
    let time_span = (last_time_axis - first_time_axis).max(0.0);
    for i in 0..=6 {
        let y_norm = i as f64 / 6.0;
        let y = (y0 as f64 + y_norm * plot_h as f64).round() as i32;
        image.draw_line(x0 as i32 - 5, y, x0 as i32, y, [80, 80, 80]);
        let value = first_time_axis + time_span * y_norm;
        let label = format_number_label(value);
        let w = text_width_px(&label, 2);
        draw_text_small(image, x0 as i32 - 10 - w, y - 4, &label, [0, 0, 0], 2);
    }

    if let Some(injection_tune) = analysis.injection_peak.as_ref().map(|peak| peak.tune) {
        if injection_tune.is_finite() && injection_tune >= tune_min && injection_tune <= tune_max {
            let x_norm = (injection_tune - tune_min) / (tune_max - tune_min).max(1e-12);
            draw_vertical_dashed_marker(image, bounds, x_norm, [255, 255, 255], 6, 4);
        }
    }
    if let Some(reference_tune) = expected_tune {
        if reference_tune.is_finite() && reference_tune >= tune_min && reference_tune <= tune_max {
            let x_norm = (reference_tune - tune_min) / (tune_max - tune_min).max(1e-12);
            draw_vertical_dashed_marker(image, bounds, x_norm, [180, 180, 180], 6, 4);
        }
    }

    let raw_segments =
        collect_tune_segments(&analysis.sliding, config, |point| point.raw_global_tune);
    for segment in raw_segments {
        draw_polyline_xy_top_down(
            image,
            bounds,
            &segment,
            tune_min,
            tune_max,
            first_time_axis,
            last_time_axis,
            [180, 180, 180],
        );
    }

    let selected_segments =
        collect_tune_segments(&analysis.sliding, config, |point| point.selected_tune);
    for segment in selected_segments {
        draw_polyline_xy_top_down_thick(
            image,
            bounds,
            &segment,
            tune_min,
            tune_max,
            first_time_axis,
            last_time_axis,
            [0, 0, 0],
            3,
        );
        draw_polyline_xy_top_down_thick(
            image,
            bounds,
            &segment,
            tune_min,
            tune_max,
            first_time_axis,
            last_time_axis,
            [255, 255, 255],
            1,
        );
    }

    let legend_x = x1 as i32 - 250;
    draw_text_small(
        image,
        legend_x,
        y0 as i32 + 4,
        "TRACKED",
        [255, 255, 255],
        2,
    );
    image.draw_line(
        legend_x - 40,
        y0 as i32 + 10,
        legend_x - 10,
        y0 as i32 + 10,
        [255, 255, 255],
    );
    draw_text_small(image, legend_x, y0 as i32 + 22, "RAW", [180, 180, 180], 2);
    image.draw_line(
        legend_x - 40,
        y0 as i32 + 28,
        legend_x - 10,
        y0 as i32 + 28,
        [180, 180, 180],
    );
}

fn draw_validation_tune_panel(
    image: &mut RgbImage,
    rect: PanelRect,
    plane: Plane,
    analysis: Option<&PlaneAnalysis>,
    config: &MonitorConfig,
) {
    draw_panel_border(image, rect, [0, 0, 0]);
    let title = match plane {
        Plane::Horizontal => "H TUNE VS TIME",
        Plane::Vertical => "V TUNE VS TIME",
    };
    draw_text_small(
        image,
        rect.x0 as i32 + 8,
        rect.y0 as i32 + 8,
        title,
        [0, 0, 0],
        2,
    );

    let bounds = panel_plot_bounds(image, rect, 76, 18, 32, 58);
    draw_axes(image, bounds, [0, 0, 0]);
    let x0 = bounds.left;
    let x1 = image.width.saturating_sub(bounds.right);
    let y0 = bounds.top;
    let y1 = image.height.saturating_sub(bounds.bottom);
    let plot_w = x1.saturating_sub(x0).max(1);

    let x_label = time_axis_label(config);
    let x_label_w = text_width_px(x_label, 2);
    draw_text_small(
        image,
        (x0 + plot_w / 2) as i32 - x_label_w / 2,
        y1 as i32 + 28,
        x_label,
        [0, 0, 0],
        2,
    );
    draw_text_small(
        image,
        rect.x0 as i32 + 8,
        rect.y0 as i32 + 24,
        "TUNE",
        [0, 0, 0],
        2,
    );

    let Some(analysis) = analysis else {
        draw_text_small(
            image,
            (x0 + 10) as i32,
            (y0 + (y1 - y0) / 2) as i32,
            "NO USABLE PLANE DATA",
            [120, 120, 120],
            2,
        );
        return;
    };
    if analysis.sliding.is_empty() {
        draw_text_small(
            image,
            (x0 + 10) as i32,
            (y0 + (y1 - y0) / 2) as i32,
            "NO SLIDING WINDOWS",
            [120, 120, 120],
            2,
        );
        return;
    }

    let time_min = analysis
        .sliding
        .first()
        .map(|point| time_axis_value_from_turn(point.center_turn, config))
        .unwrap_or(0.0);
    let mut time_max = analysis
        .sliding
        .last()
        .map(|point| time_axis_value_from_turn(point.center_turn, config))
        .unwrap_or(time_min + 1.0);
    if (time_max - time_min).abs() < 1e-12 {
        time_max = time_min + 1.0;
    }

    let mut observed = analysis
        .sliding
        .iter()
        .flat_map(|point| [point.raw_global_tune, point.selected_tune])
        .flatten()
        .filter(|tune| tune.is_finite())
        .collect::<Vec<_>>();
    observed.sort_by(|a, b| a.partial_cmp(b).unwrap_or(Ordering::Equal));
    let (mut y_min, mut y_max) =
        if let (Some(first), Some(last)) = (observed.first(), observed.last()) {
            (*first, *last)
        } else {
            (config.tune_plot_y_min, config.tune_plot_y_max)
        };
    let span = (y_max - y_min).abs().max(0.002);
    let pad = (span * 0.15).max(0.001);
    y_min = (y_min - pad).max(0.0);
    y_max = (y_max + pad).min(1.0);
    if y_max <= y_min {
        y_max = (y_min + 0.01).min(1.0);
    }

    draw_xy_ticks(image, bounds, time_min, time_max, y_min, y_max);

    let (tracked_color, raw_color) = match plane {
        Plane::Horizontal => ([0, 70, 220], [160, 190, 240]),
        Plane::Vertical => ([220, 0, 0], [240, 170, 170]),
    };

    let raw_segments =
        collect_tune_segments(&analysis.sliding, config, |point| point.raw_global_tune);
    for segment in raw_segments {
        draw_polyline_xy(
            image, bounds, &segment, time_min, time_max, y_min, y_max, raw_color,
        );
    }

    let selected_segments =
        collect_tune_segments(&analysis.sliding, config, |point| point.selected_tune);
    for segment in selected_segments {
        draw_polyline_xy(
            image,
            bounds,
            &segment,
            time_min,
            time_max,
            y_min,
            y_max,
            tracked_color,
        );
    }

    for point in &analysis.sliding {
        if !point.suspicious_step && !point.used_global_fallback {
            continue;
        }
        let time_axis = time_axis_value_from_turn(point.center_turn, config);
        let tune = point.selected_tune.or(point.raw_global_tune);
        let Some(tune) = tune else {
            continue;
        };
        if !time_axis.is_finite() || !tune.is_finite() {
            continue;
        }
        let x_norm = ((time_axis - time_min) / (time_max - time_min).max(1e-12)).clamp(0.0, 1.0);
        let y_norm = ((tune - y_min) / (y_max - y_min).max(1e-12)).clamp(0.0, 1.0);
        let x_px = x_from_norm(image, bounds, x_norm);
        let y_px = y_from_norm(image, bounds, y_norm);
        if point.suspicious_step {
            draw_cross_marker(image, x_px, y_px, [220, 0, 0], 3);
        }
        if point.used_global_fallback {
            draw_circle_marker(image, x_px, y_px, [255, 140, 0], 3);
        }
    }

    let legend_x = x1 as i32 - 255;
    let legend_y = y0 as i32 + 8;
    image.draw_line(
        legend_x,
        legend_y + 6,
        legend_x + 28,
        legend_y + 6,
        tracked_color,
    );
    draw_text_small(
        image,
        legend_x + 36,
        legend_y + 2,
        "TRACKED",
        tracked_color,
        2,
    );
    image.draw_line(
        legend_x,
        legend_y + 24,
        legend_x + 28,
        legend_y + 24,
        raw_color,
    );
    draw_text_small(image, legend_x + 36, legend_y + 20, "RAW", raw_color, 2);
    draw_cross_marker(image, legend_x + 14, legend_y + 46, [220, 0, 0], 3);
    draw_text_small(
        image,
        legend_x + 36,
        legend_y + 42,
        "SUSPICIOUS STEP",
        [220, 0, 0],
        2,
    );
    draw_circle_marker(image, legend_x + 14, legend_y + 64, [255, 140, 0], 3);
    draw_text_small(
        image,
        legend_x + 36,
        legend_y + 60,
        "FALLBACK",
        [255, 140, 0],
        2,
    );
}

fn write_tune_validation_png(
    path: &Path,
    snapshot: &SpillSnapshot,
    config: &MonitorConfig,
) -> Result<()> {
    let mut image = RgbImage::new(1680, 1180);
    image.fill([255, 255, 255]);

    let margin_left = 28usize;
    let margin_right = 24usize;
    let margin_top = 30usize;
    let margin_bottom = 24usize;
    let col_gap = 28usize;
    let row_gap = 28usize;

    let panel_w = (image.width - margin_left - margin_right - col_gap) / 2;
    let panel_h = (image.height - margin_top - margin_bottom - row_gap) / 2;
    let left_x0 = margin_left;
    let right_x0 = margin_left + panel_w + col_gap;
    let top_y0 = margin_top;
    let bottom_y0 = margin_top + panel_h + row_gap;

    let panel_tl = PanelRect {
        x0: left_x0,
        y0: top_y0,
        x1: left_x0 + panel_w,
        y1: top_y0 + panel_h,
    };
    let panel_tr = PanelRect {
        x0: right_x0,
        y0: top_y0,
        x1: right_x0 + panel_w,
        y1: top_y0 + panel_h,
    };
    let panel_bl = PanelRect {
        x0: left_x0,
        y0: bottom_y0,
        x1: left_x0 + panel_w,
        y1: bottom_y0 + panel_h,
    };
    let panel_br = PanelRect {
        x0: right_x0,
        y0: bottom_y0,
        x1: right_x0 + panel_w,
        y1: bottom_y0 + panel_h,
    };

    let header = format!("SPILL {} TUNE VALIDATION", snapshot.target_ms);
    let w = text_width_px(&header, 2);
    let header_x = (image.width as i32 / 2) - (w / 2);
    draw_text_small(&mut image, header_x, 8, &header, [0, 0, 0], 2);

    draw_validation_spectrogram_panel(
        &mut image,
        panel_tl,
        Plane::Horizontal,
        snapshot.h_analysis.as_ref(),
        config,
        None,
    );
    draw_validation_spectrogram_panel(
        &mut image,
        panel_tr,
        Plane::Vertical,
        snapshot.v_analysis.as_ref(),
        config,
        None,
    );
    draw_validation_tune_panel(
        &mut image,
        panel_bl,
        Plane::Horizontal,
        snapshot.h_analysis.as_ref(),
        config,
    );
    draw_validation_tune_panel(
        &mut image,
        panel_br,
        Plane::Vertical,
        snapshot.v_analysis.as_ref(),
        config,
    );

    write_png_rgb(path, &image)
}

fn write_spectrogram_png(
    path: &Path,
    plane: Plane,
    spectra: &[Vec<f64>],
    sliding: &[SlidingPoint],
    config: &MonitorConfig,
) -> Result<()> {
    if spectra.is_empty() || sliding.is_empty() {
        return write_empty_png(path);
    }
    let rows = spectra.len().min(sliding.len());
    if rows == 0 {
        return write_empty_png(path);
    }
    let n_bins = spectra
        .iter()
        .take(rows)
        .map(|row| row.len())
        .min()
        .unwrap_or(0);
    if n_bins == 0 {
        return write_empty_png(path);
    }

    let tune_min = config.tune_plot_y_min.clamp(0.0, 1.0);
    let tune_max = config.tune_plot_y_max.clamp(tune_min + 1e-6, 1.0);
    let bin_start = ((tune_min * n_bins as f64).floor() as usize).min(n_bins - 1);
    let mut bin_end = ((tune_max * n_bins as f64).ceil() as usize).max(bin_start + 1);
    bin_end = bin_end.min(n_bins);

    let mut log_values = Vec::<f64>::new();
    for row in spectra.iter().take(rows) {
        for power in &row[bin_start..bin_end] {
            if power.is_finite() {
                log_values.push((power.max(1e-12)).log10());
            }
        }
    }
    if log_values.is_empty() {
        return write_empty_png(path);
    }
    log_values.sort_by(|a, b| a.partial_cmp(b).unwrap_or(Ordering::Equal));
    let lo_idx = ((log_values.len() - 1) as f64 * 0.05).round() as usize;
    let hi_idx = ((log_values.len() - 1) as f64 * 0.995).round() as usize;
    let log_lo = log_values[lo_idx.min(log_values.len() - 1)];
    let log_hi = log_values[hi_idx.min(log_values.len() - 1)].max(log_lo + 1e-9);
    let log_span = (log_hi - log_lo).max(1e-9);

    let plot_h_target = rows.max(240);
    let image_h = 30usize
        .saturating_add(plot_h_target)
        .saturating_add(70)
        .max(420);
    let mut image = RgbImage::new(1280, image_h);
    image.fill([255, 255, 255]);
    let bounds = PlotBounds {
        left: 90,
        right: 20,
        top: 30,
        bottom: 70,
    };
    let x0 = bounds.left;
    let x1 = image.width.saturating_sub(bounds.right);
    let y0 = bounds.top;
    let y1 = image.height.saturating_sub(bounds.bottom);
    let plot_w = x1.saturating_sub(x0).max(1);
    let plot_h = y1.saturating_sub(y0).max(1);

    let mut bins_by_x = Vec::<usize>::with_capacity(plot_w as usize);
    for px in x0..x1 {
        let x_norm = ((px - x0) as f64 / plot_w as f64).clamp(0.0, 1.0);
        let tune = tune_min + (tune_max - tune_min) * x_norm;
        let mut bin = (tune * n_bins as f64).floor() as usize;
        bin = bin.clamp(bin_start, bin_end.saturating_sub(1));
        bins_by_x.push(bin);
    }

    // Render one discrete heatmap row per sliding FFT window.
    for row_idx in 0..rows {
        let row = &spectra[row_idx];
        let row_y0 = y0 + (row_idx * plot_h) / rows;
        let mut row_y1 = y0 + ((row_idx + 1) * plot_h) / rows;
        if row_y1 <= row_y0 {
            row_y1 = row_y0 + 1;
        }
        for (x_offset, px) in (x0..x1).enumerate() {
            let bin = bins_by_x[x_offset];
            let power = row.get(bin).copied().unwrap_or(0.0).max(1e-12);
            let log_power = power.log10();
            let norm = ((log_power - log_lo) / log_span).clamp(0.0, 1.0);
            let color = heatmap_color(norm);
            for py in row_y0..row_y1.min(y1) {
                image.set_pixel(px as i32, py as i32, color);
            }
        }
    }

    // Border box.
    image.draw_line(x0 as i32, y0 as i32, x1 as i32, y0 as i32, [0, 0, 0]);
    image.draw_line(x0 as i32, y1 as i32, x1 as i32, y1 as i32, [0, 0, 0]);
    image.draw_line(x0 as i32, y0 as i32, x0 as i32, y1 as i32, [0, 0, 0]);
    image.draw_line(x1 as i32, y0 as i32, x1 as i32, y1 as i32, [0, 0, 0]);

    // X-axis ticks: tune.
    for i in 0..=6 {
        let x_norm = i as f64 / 6.0;
        let x = (x0 as f64 + x_norm * plot_w as f64).round() as i32;
        image.draw_line(x, y1 as i32, x, y1 as i32 + 6, [80, 80, 80]);
        let value = tune_min + (tune_max - tune_min) * x_norm;
        let label = format!("{value:.2}");
        let w = text_width_px(&label, 2);
        draw_text_small(&mut image, x - w / 2, y1 as i32 + 10, &label, [0, 0, 0], 2);
    }

    // Y-axis ticks: first-to-last sliding-window center in configured axis domain.
    let first_time_axis = sliding
        .iter()
        .take(rows)
        .map(|point| time_axis_value_from_turn(point.center_turn, config))
        .next()
        .unwrap_or(0.0);
    let last_time_axis = sliding
        .iter()
        .take(rows)
        .map(|point| time_axis_value_from_turn(point.center_turn, config))
        .last()
        .unwrap_or(first_time_axis);
    let time_span_axis = (last_time_axis - first_time_axis).max(0.0);
    for i in 0..=6 {
        let y_norm = i as f64 / 6.0;
        let y = (y0 as f64 + y_norm * plot_h as f64).round() as i32;
        image.draw_line(x0 as i32 - 6, y, x0 as i32, y, [80, 80, 80]);
        let value = first_time_axis + time_span_axis * y_norm;
        let label = format_number_label(value);
        let w = text_width_px(&label, 2);
        draw_text_small(&mut image, x0 as i32 - 12 - w, y - 4, &label, [0, 0, 0], 2);
    }

    // Overlay raw per-window peak trajectory (unconstrained by tracking state).
    let mut selected_line = Vec::<(i32, i32)>::new();
    for (row_idx, point) in sliding.iter().take(rows).enumerate() {
        let Some(tune) = point.raw_global_tune else {
            continue;
        };
        if !tune.is_finite() || tune < tune_min || tune > tune_max {
            continue;
        }
        let x = x0 as f64 + ((tune - tune_min) / (tune_max - tune_min).max(1e-12)) * plot_w as f64;
        let row_y0 = y0 + (row_idx * plot_h) / rows;
        let mut row_y1 = y0 + ((row_idx + 1) * plot_h) / rows;
        if row_y1 <= row_y0 {
            row_y1 = row_y0 + 1;
        }
        let y = ((row_y0 + row_y1.min(y1)) as f64) * 0.5;
        selected_line.push((x.round() as i32, y.round() as i32));
    }
    for segment in selected_line.windows(2) {
        let a = segment[0];
        let b = segment[1];
        image.draw_line(a.0, a.1, b.0, b.1, [255, 255, 255]);
    }

    let title = match plane {
        Plane::Horizontal => "H TUNE SPECTROGRAM",
        Plane::Vertical => "V TUNE SPECTROGRAM",
    };
    draw_text_small(&mut image, x0 as i32 + 4, 8, title, [0, 0, 0], 2);
    draw_text_small(
        &mut image,
        x0 as i32 + 4,
        24,
        &format!("TURN PERIOD: {:.3} us", config.turn_period_us),
        [0, 0, 0],
        2,
    );
    let x_label = "TUNE";
    let x_label_w = text_width_px(x_label, 2);
    let image_height = image.height as i32;
    draw_text_small(
        &mut image,
        (x0 + plot_w / 2) as i32 - x_label_w / 2,
        image_height - 24,
        x_label,
        [0, 0, 0],
        2,
    );
    draw_text_small(
        &mut image,
        8,
        (y0 + plot_h / 2) as i32,
        time_axis_label(config),
        [0, 0, 0],
        2,
    );

    write_png_rgb(path, &image)
}

fn write_empty_png(path: &Path) -> Result<()> {
    let mut image = RgbImage::new(1280, 720);
    image.fill([255, 255, 255]);
    let bounds = PlotBounds {
        left: 80,
        right: 20,
        top: 20,
        bottom: 60,
    };
    draw_axes(&mut image, bounds, [0, 0, 0]);
    image.draw_line(
        bounds.left as i32,
        bounds.top as i32,
        (image.width - bounds.right) as i32,
        (image.height - bounds.bottom) as i32,
        [220, 0, 0],
    );
    image.draw_line(
        (image.width - bounds.right) as i32,
        bounds.top as i32,
        bounds.left as i32,
        (image.height - bounds.bottom) as i32,
        [220, 0, 0],
    );
    write_png_rgb(path, &image)
}

fn finite_segments(points: &[SlidingPoint]) -> Vec<Vec<(f64, f64)>> {
    let mut segments = Vec::<Vec<(f64, f64)>>::new();
    let mut current = Vec::<(f64, f64)>::new();

    for point in points {
        match point.selected_tune {
            Some(tune) if tune.is_finite() => {
                current.push((point.center_turn as f64, tune));
            }
            _ => {
                if current.len() >= 2 {
                    segments.push(current.clone());
                }
                current.clear();
            }
        }
    }

    if current.len() >= 2 {
        segments.push(current);
    }

    segments
}

fn compose_spill_summary_lines(
    config: &MonitorConfig,
    snapshot: &SpillSnapshot,
    paths: &SpillOutputPaths,
    title: &str,
    verbose_observations: bool,
) -> Vec<String> {
    let observations = snapshot.observations.as_slice();
    let mut lines = Vec::<String>::new();
    let aligned_streams = observations.iter().filter(|obs| obs.aligned).count();
    let aligned_stream_fraction = aligned_streams as f64 / observations.len().max(1) as f64;

    let mut per_device = HashMap::<&str, bool>::new();
    for obs in observations {
        let entry = per_device.entry(obs.bpm_ip.as_str()).or_insert(false);
        if obs.aligned {
            *entry = true;
        }
    }
    let aligned_digitizers = per_device.values().filter(|aligned| **aligned).count();
    let aligned_digitizer_fraction = if per_device.is_empty() {
        0.0
    } else {
        aligned_digitizers as f64 / per_device.len() as f64
    };

    lines.push(title.to_string());
    lines.push(format!("target_ms: {}", snapshot.target_ms));
    lines.push(format!(
        "TBT stream alignment: {}/{} ({:.1}%) within ±{} ms",
        aligned_streams,
        observations.len(),
        aligned_stream_fraction * 100.0,
        config.align_tolerance_ms
    ));
    lines.push(format!(
        "digitizer alignment (at least one aligned TBT stream): {}/{} ({:.1}%)",
        aligned_digitizers,
        per_device.len(),
        aligned_digitizer_fraction * 100.0
    ));
    lines.push(format!(
        "configured digitizers in config: {}",
        config.devices.len()
    ));
    lines.push(format!(
        "alignment threshold: min_aligned_fraction={:.2}",
        config.min_aligned_fraction
    ));
    if let Some(stats) = timeliness_stats(observations, snapshot.target_ms) {
        lines.push(format!(
            "TBT timeliness (obs ms - target_ms): min={} ms max={} ms median|delta|={:.2} ms max|delta|={} ms",
            stats.min_delta_ms,
            stats.max_delta_ms,
            stats.median_abs_delta_ms,
            stats.max_abs_delta_ms
        ));
    }

    if verbose_observations {
        lines.push("TBT latest-id samples:".to_string());
        for obs in observations {
            let delta_ms = signed_delta_ms(obs.ms, snapshot.target_ms);
            lines.push(format!(
                "  {} {} {} [{}; delta_ms={}]",
                obs.bpm_ip,
                obs.stream_key,
                obs.id,
                if obs.aligned { "aligned" } else { "off-target" },
                delta_ms
            ));
        }
    }

    if let Some(analysis) = snapshot.h_analysis.as_ref() {
        lines.push(format!(
            "H plane: traces used {}/{} consensus_n={} Qx={} conf={} sliding_windows={}",
            analysis.traces_used,
            analysis.traces_total,
            analysis.consensus_turns,
            analysis
                .injection_peak
                .as_ref()
                .map(|peak| format!("{:.6}", peak.tune))
                .unwrap_or_else(|| "NA".to_string()),
            analysis
                .injection_peak
                .as_ref()
                .map(|peak| format!("{:.2}", peak.confidence))
                .unwrap_or_else(|| "NA".to_string()),
            analysis.sliding.len()
        ));
    } else {
        lines.push("H plane: no usable data".to_string());
    }

    if let Some(analysis) = snapshot.v_analysis.as_ref() {
        lines.push(format!(
            "V plane: traces used {}/{} consensus_n={} Qy={} conf={} sliding_windows={}",
            analysis.traces_used,
            analysis.traces_total,
            analysis.consensus_turns,
            analysis
                .injection_peak
                .as_ref()
                .map(|peak| format!("{:.6}", peak.tune))
                .unwrap_or_else(|| "NA".to_string()),
            analysis
                .injection_peak
                .as_ref()
                .map(|peak| format!("{:.2}", peak.confidence))
                .unwrap_or_else(|| "NA".to_string()),
            analysis.sliding.len()
        ));
    } else {
        lines.push("V plane: no usable data".to_string());
    }

    lines.push("output files:".to_string());
    lines.push(format!("  {}", paths.spectrum_h.display()));
    lines.push(format!("  {}", paths.spectrum_v.display()));
    lines.push(format!("  {}", paths.spectrogram_h.display()));
    lines.push(format!("  {}", paths.spectrogram_v.display()));
    lines.push(format!("  {}", paths.tune_vs_time.display()));
    lines.push(format!("  {}", paths.tune_validation.display()));
    lines.push(format!("  {}", paths.sliding_tune_csv.display()));

    if snapshot.warnings.is_empty() {
        lines.push("warnings: none".to_string());
    } else {
        lines.push(format!("warnings ({}):", snapshot.warnings.len()));
        for warning in &snapshot.warnings {
            lines.push(format!("  - {}", warning));
        }
    }

    lines
}

fn print_summary(
    config: &MonitorConfig,
    snapshot: &SpillSnapshot,
    paths: &SpillOutputPaths,
    title: &str,
    verbose_observations: bool,
) -> Vec<String> {
    let lines = compose_spill_summary_lines(config, snapshot, paths, title, verbose_observations);
    for line in &lines {
        println!("{line}");
    }
    lines
}

#[derive(Debug, Clone, Copy)]
struct PlotBounds {
    left: usize,
    right: usize,
    top: usize,
    bottom: usize,
}

#[derive(Debug, Clone)]
struct RgbImage {
    width: usize,
    height: usize,
    data: Vec<u8>,
}

impl RgbImage {
    fn new(width: usize, height: usize) -> Self {
        Self {
            width,
            height,
            data: vec![255; width * height * 3],
        }
    }

    fn fill(&mut self, color: [u8; 3]) {
        for pixel in self.data.chunks_exact_mut(3) {
            pixel.copy_from_slice(&color);
        }
    }

    fn set_pixel(&mut self, x: i32, y: i32, color: [u8; 3]) {
        if x < 0 || y < 0 {
            return;
        }
        let x = x as usize;
        let y = y as usize;
        if x >= self.width || y >= self.height {
            return;
        }

        let idx = (y * self.width + x) * 3;
        self.data[idx..idx + 3].copy_from_slice(&color);
    }

    fn draw_line(&mut self, mut x0: i32, mut y0: i32, x1: i32, y1: i32, color: [u8; 3]) {
        let dx = (x1 - x0).abs();
        let sx = if x0 < x1 { 1 } else { -1 };
        let dy = -(y1 - y0).abs();
        let sy = if y0 < y1 { 1 } else { -1 };
        let mut err = dx + dy;

        loop {
            self.set_pixel(x0, y0, color);
            if x0 == x1 && y0 == y1 {
                break;
            }
            let e2 = 2 * err;
            if e2 >= dy {
                err += dy;
                x0 += sx;
            }
            if e2 <= dx {
                err += dx;
                y0 += sy;
            }
        }
    }
}

fn draw_axes(image: &mut RgbImage, bounds: PlotBounds, color: [u8; 3]) {
    let x0 = bounds.left as i32;
    let y0 = (image.height - bounds.bottom) as i32;
    let x1 = (image.width - bounds.right) as i32;
    let y1 = bounds.top as i32;

    image.draw_line(x0, y0, x1, y0, color);
    image.draw_line(x0, y0, x0, y1, color);
}

fn draw_spectrum_ticks(image: &mut RgbImage, bounds: PlotBounds, max_y: f64) {
    let axis_color = [80, 80, 80];
    let label_color = [0, 0, 0];
    let tick_len = 6;
    let scale = 2;

    let y_axis = (image.height - bounds.bottom) as i32;
    let x_axis = bounds.left as i32;

    for i in 0..=5 {
        let x_norm = i as f64 / 5.0;
        let x = x_from_norm(image, bounds, x_norm);
        image.draw_line(x, y_axis, x, y_axis + tick_len, axis_color);
        let label = format_number_label(x_norm);
        let w = text_width_px(&label, scale);
        draw_text_small(
            image,
            x - w / 2,
            y_axis + tick_len + 4,
            &label,
            label_color,
            scale,
        );
    }

    for i in 0..=5 {
        let y_norm = i as f64 / 5.0;
        let y = y_from_norm(image, bounds, y_norm);
        image.draw_line(x_axis - tick_len, y, x_axis, y, axis_color);
        let label = format_number_label(max_y * y_norm);
        let w = text_width_px(&label, scale);
        draw_text_small(
            image,
            x_axis - tick_len - w - 6,
            y - 4,
            &label,
            label_color,
            scale,
        );
    }
}

fn draw_trace_ticks(
    image: &mut RgbImage,
    bounds: PlotBounds,
    x_max: f64,
    y_min: f64,
    y_max: f64,
    y_grid_step: f64,
) {
    let axis_color = [80, 80, 80];
    let label_color = [0, 0, 0];
    let grid_color = [220, 220, 220];
    let tick_len = 6;
    let scale = 2;

    let y_axis = (image.height - bounds.bottom) as i32;
    let x_axis = bounds.left as i32;
    let x_right = (image.width - bounds.right) as i32;

    for i in 0..=5 {
        let x_norm = i as f64 / 5.0;
        let x = x_from_norm(image, bounds, x_norm);
        image.draw_line(x, y_axis, x, y_axis + tick_len, axis_color);
        let value = x_max * x_norm;
        let label = format_number_label(value);
        let w = text_width_px(&label, scale);
        draw_text_small(
            image,
            x - w / 2,
            y_axis + tick_len + 4,
            &label,
            label_color,
            scale,
        );
    }

    let mut y_ticks = vec![y_min, y_max];
    if y_grid_step.is_finite() && y_grid_step > 0.0 {
        let mut value = (y_min / y_grid_step).ceil() * y_grid_step;
        while value < y_max {
            if value > y_min && value < y_max {
                y_ticks.push(value);
            }
            value += y_grid_step;
        }
    }
    y_ticks.sort_by(|a, b| a.partial_cmp(b).unwrap_or(Ordering::Equal));
    y_ticks.dedup_by(|a, b| (*a - *b).abs() <= 1e-9);

    for value in y_ticks {
        let y_norm = ((value - y_min) / (y_max - y_min).max(1e-12)).clamp(0.0, 1.0);
        let y = y_from_norm(image, bounds, y_norm);
        image.draw_line(x_axis - tick_len, y, x_axis, y, axis_color);
        image.draw_line(x_axis + 1, y, x_right, y, grid_color);
        let label = format_number_label(value);
        let w = text_width_px(&label, scale);
        draw_text_small(
            image,
            x_axis - tick_len - w - 6,
            y - 4,
            &label,
            label_color,
            scale,
        );
    }
}

fn draw_xy_ticks(
    image: &mut RgbImage,
    bounds: PlotBounds,
    x_min: f64,
    x_max: f64,
    y_min: f64,
    y_max: f64,
) {
    let axis_color = [80, 80, 80];
    let label_color = [0, 0, 0];
    let tick_len = 6;
    let scale = 2;

    let y_axis = (image.height - bounds.bottom) as i32;
    let x_axis = bounds.left as i32;
    let x_span = (x_max - x_min).abs().max(1e-12);
    let y_span = (y_max - y_min).abs().max(1e-12);

    for i in 0..=5 {
        let x_norm = i as f64 / 5.0;
        let x = x_from_norm(image, bounds, x_norm);
        image.draw_line(x, y_axis, x, y_axis + tick_len, axis_color);
        let value = x_min + x_span * x_norm;
        let label = format_number_label(value);
        let w = text_width_px(&label, scale);
        draw_text_small(
            image,
            x - w / 2,
            y_axis + tick_len + 4,
            &label,
            label_color,
            scale,
        );
    }

    for i in 0..=5 {
        let y_norm = i as f64 / 5.0;
        let y = y_from_norm(image, bounds, y_norm);
        image.draw_line(x_axis - tick_len, y, x_axis, y, axis_color);
        let value = y_min + y_span * y_norm;
        let label = format_number_label(value);
        let w = text_width_px(&label, scale);
        draw_text_small(
            image,
            x_axis - tick_len - w - 6,
            y - 4,
            &label,
            label_color,
            scale,
        );
    }
}

fn draw_polyline_xy(
    image: &mut RgbImage,
    bounds: PlotBounds,
    points: &[(f64, f64)],
    x_min: f64,
    x_max: f64,
    y_min: f64,
    y_max: f64,
    color: [u8; 3],
) {
    if points.len() < 2 {
        return;
    }
    let x_span = (x_max - x_min).abs().max(1e-12);
    let y_span = (y_max - y_min).abs().max(1e-12);

    let normalized = points
        .iter()
        .map(|(x, y)| {
            let nx = ((*x - x_min) / x_span).clamp(0.0, 1.0);
            let ny = ((*y - y_min) / y_span).clamp(0.0, 1.0);
            (nx, ny)
        })
        .collect::<Vec<_>>();

    draw_polyline_normalized(image, bounds, &normalized, color);
}

fn draw_peak_label(image: &mut RgbImage, bounds: PlotBounds, tune: f64, color: [u8; 3]) {
    let x = x_from_norm(image, bounds, tune.clamp(0.0, 1.0));
    let y_top = bounds.top as i32;
    image.draw_line(x - 4, y_top + 8, x + 4, y_top + 8, color);
    image.draw_line(x, y_top + 4, x, y_top + 12, color);

    let label = format_number_label(tune);
    let w = text_width_px(&label, 2);
    let mut x_label = x - (w / 2);
    let min_x = bounds.left as i32;
    let max_x = (image.width - bounds.right) as i32 - w - 2;
    if x_label < min_x {
        x_label = min_x;
    }
    if x_label > max_x {
        x_label = max_x;
    }

    draw_text_small(image, x_label, y_top + 16, &label, color, 2);
}

fn draw_line_legend(image: &mut RgbImage, origin: (i32, i32), entries: &[([u8; 3], &str)]) {
    let mut y = origin.1;
    for (color, label) in entries {
        image.draw_line(origin.0, y + 6, origin.0 + 28, y + 6, *color);
        draw_text_small(image, origin.0 + 36, y + 1, label, *color, 2);
        y += 18;
    }
}

fn draw_vertical_marker(image: &mut RgbImage, bounds: PlotBounds, x_norm: f64, color: [u8; 3]) {
    let x = x_from_norm(image, bounds, x_norm);
    let y0 = bounds.top as i32;
    let y1 = (image.height - bounds.bottom) as i32;
    image.draw_line(x, y0, x, y1, color);
}

fn draw_polyline_normalized(
    image: &mut RgbImage,
    bounds: PlotBounds,
    points: &[(f64, f64)],
    color: [u8; 3],
) {
    if points.len() < 2 {
        return;
    }

    let mut prev = map_point(image, bounds, points[0]);
    for point in &points[1..] {
        let next = map_point(image, bounds, *point);
        image.draw_line(prev.0, prev.1, next.0, next.1, color);
        prev = next;
    }
}

fn map_point(image: &RgbImage, bounds: PlotBounds, point: (f64, f64)) -> (i32, i32) {
    let x = x_from_norm(image, bounds, point.0);
    let y = y_from_norm(image, bounds, point.1);
    (x, y)
}

fn x_from_norm(image: &RgbImage, bounds: PlotBounds, x_norm: f64) -> i32 {
    let width = (image.width - bounds.left - bounds.right).max(1) as f64;
    (bounds.left as f64 + x_norm.clamp(0.0, 1.0) * width).round() as i32
}

fn y_from_norm(image: &RgbImage, bounds: PlotBounds, y_norm: f64) -> i32 {
    let height = (image.height - bounds.top - bounds.bottom).max(1) as f64;
    let y = bounds.top as f64 + (1.0 - y_norm.clamp(0.0, 1.0)) * height;
    y.round() as i32
}

fn format_number_label(value: f64) -> String {
    let abs = value.abs();
    let raw = if abs >= 10_000.0 {
        format!("{value:.0}")
    } else if abs >= 1_000.0 {
        format!("{value:.1}")
    } else if abs >= 100.0 {
        format!("{value:.2}")
    } else if abs >= 1.0 {
        format!("{value:.3}")
    } else {
        format!("{value:.5}")
    };
    trim_label_zeros(&raw)
}

fn trim_label_zeros(input: &str) -> String {
    if !input.contains('.') {
        return input.to_string();
    }
    let mut out = input.trim_end_matches('0').to_string();
    if out.ends_with('.') {
        out.pop();
    }
    if out.is_empty() || out == "-" {
        "0".to_string()
    } else {
        out
    }
}

fn text_width_px(text: &str, scale: i32) -> i32 {
    let step = 6 * scale.max(1);
    text.chars().count() as i32 * step
}

fn draw_text_small(image: &mut RgbImage, x: i32, y: i32, text: &str, color: [u8; 3], scale: i32) {
    let scale = scale.max(1);
    let mut cursor_x = x;
    for ch in text.chars() {
        if ch == ' ' {
            cursor_x += 6 * scale;
            continue;
        }
        if let Some(rows) = glyph_5x7(ch) {
            for (row_idx, row_bits) in rows.iter().enumerate() {
                for col_idx in 0..5 {
                    let mask = 1 << (4 - col_idx);
                    if (row_bits & mask) != 0 {
                        let px = cursor_x + col_idx * scale;
                        let py = y + row_idx as i32 * scale;
                        for dy in 0..scale {
                            for dx in 0..scale {
                                image.set_pixel(px + dx, py + dy, color);
                            }
                        }
                    }
                }
            }
        }
        cursor_x += 6 * scale;
    }
}

fn glyph_5x7(ch: char) -> Option<[u8; 7]> {
    let c = match ch {
        '0'..='9' | '.' | '-' | '+' | '=' | 'A'..='Z' => ch,
        _ => return None,
    };
    Some(match c {
        '0' => [
            0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110,
        ],
        '1' => [
            0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110,
        ],
        '2' => [
            0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0b01000, 0b11111,
        ],
        '3' => [
            0b11110, 0b00001, 0b00001, 0b01110, 0b00001, 0b00001, 0b11110,
        ],
        '4' => [
            0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010,
        ],
        '5' => [
            0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110,
        ],
        '6' => [
            0b00110, 0b01000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110,
        ],
        '7' => [
            0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000,
        ],
        '8' => [
            0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110,
        ],
        '9' => [
            0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00010, 0b11100,
        ],
        '.' => [
            0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b00110, 0b00110,
        ],
        '-' => [
            0b00000, 0b00000, 0b00000, 0b01110, 0b00000, 0b00000, 0b00000,
        ],
        '+' => [
            0b00000, 0b00100, 0b00100, 0b11111, 0b00100, 0b00100, 0b00000,
        ],
        '=' => [
            0b00000, 0b00000, 0b11111, 0b00000, 0b11111, 0b00000, 0b00000,
        ],
        'A' => [
            0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001,
        ],
        'B' => [
            0b11110, 0b10001, 0b10001, 0b11110, 0b10001, 0b10001, 0b11110,
        ],
        'C' => [
            0b01111, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b01111,
        ],
        'D' => [
            0b11110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b11110,
        ],
        'E' => [
            0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b11111,
        ],
        'H' => [
            0b10001, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001,
        ],
        'K' => [
            0b10001, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010, 0b10001,
        ],
        'M' => [
            0b10001, 0b11011, 0b10101, 0b10101, 0b10001, 0b10001, 0b10001,
        ],
        'N' => [
            0b10001, 0b11001, 0b10101, 0b10011, 0b10001, 0b10001, 0b10001,
        ],
        'P' => [
            0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000, 0b10000,
        ],
        'R' => [
            0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001,
        ],
        'S' => [
            0b01111, 0b10000, 0b10000, 0b01110, 0b00001, 0b00001, 0b11110,
        ],
        'T' => [
            0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100,
        ],
        'U' => [
            0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110,
        ],
        'V' => [
            0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01010, 0b00100,
        ],
        _ => return None,
    })
}

fn write_png_rgb(path: &Path, image: &RgbImage) -> Result<()> {
    let mut raw = Vec::with_capacity((image.width * 3 + 1) * image.height);
    let row_len = image.width * 3;

    for row in 0..image.height {
        raw.push(0); // no filter
        let start = row * row_len;
        raw.extend_from_slice(&image.data[start..start + row_len]);
    }

    let mut zlib_data = Vec::new();
    zlib_data.push(0x78);
    zlib_data.push(0x01);

    let mut offset = 0usize;
    while offset < raw.len() {
        let block_len = (raw.len() - offset).min(65_535);
        let is_final = offset + block_len >= raw.len();

        zlib_data.push(if is_final { 0x01 } else { 0x00 });
        let len_u16 = block_len as u16;
        zlib_data.extend_from_slice(&len_u16.to_le_bytes());
        zlib_data.extend_from_slice(&(!len_u16).to_le_bytes());
        zlib_data.extend_from_slice(&raw[offset..offset + block_len]);

        offset += block_len;
    }

    let adler = adler32(&raw);
    zlib_data.extend_from_slice(&adler.to_be_bytes());

    let mut png = Vec::new();
    png.extend_from_slice(&[137, 80, 78, 71, 13, 10, 26, 10]);

    let mut ihdr = Vec::new();
    ihdr.extend_from_slice(&(image.width as u32).to_be_bytes());
    ihdr.extend_from_slice(&(image.height as u32).to_be_bytes());
    ihdr.push(8); // bit depth
    ihdr.push(2); // color type RGB
    ihdr.push(0); // compression method
    ihdr.push(0); // filter method
    ihdr.push(0); // interlace

    write_png_chunk(&mut png, b"IHDR", &ihdr);
    write_png_chunk(&mut png, b"IDAT", &zlib_data);
    write_png_chunk(&mut png, b"IEND", &[]);

    fs::write(path, png).with_context(|| format!("failed writing {}", path.display()))
}

fn write_png_chunk(out: &mut Vec<u8>, chunk_type: &[u8; 4], data: &[u8]) {
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    out.extend_from_slice(chunk_type);
    out.extend_from_slice(data);

    let mut crc_input = Vec::with_capacity(4 + data.len());
    crc_input.extend_from_slice(chunk_type);
    crc_input.extend_from_slice(data);
    let crc = crc32(&crc_input);
    out.extend_from_slice(&crc.to_be_bytes());
}

fn adler32(bytes: &[u8]) -> u32 {
    const MOD_ADLER: u32 = 65_521;
    let mut a = 1u32;
    let mut b = 0u32;

    for &byte in bytes {
        a = (a + byte as u32) % MOD_ADLER;
        b = (b + a) % MOD_ADLER;
    }

    (b << 16) | a
}

fn crc32(bytes: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;

    for &byte in bytes {
        crc ^= byte as u32;
        for _ in 0..8 {
            if crc & 1 == 1 {
                crc = (crc >> 1) ^ 0xEDB8_8320;
            } else {
                crc >>= 1;
            }
        }
    }

    !crc
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::{HashMap, HashSet};

    fn make_trace(plane: Plane, q: f64, n: usize, phase: f64) -> StreamTrace {
        let mut samples = Vec::with_capacity(n);
        for turn in 0..n {
            let angle = 2.0 * std::f64::consts::PI * q * turn as f64 + phase;
            samples.push(angle.sin());
        }

        StreamTrace {
            plane,
            bpm_ip: "test".to_string(),
            stream_key: format!("{:?}:{phase:.3}", plane),
            samples,
        }
    }

    fn make_piecewise_trace(plane: Plane, frequencies: &[f64], window_turns: usize) -> StreamTrace {
        let total_turns = frequencies.len() * window_turns;
        let mut samples = Vec::with_capacity(total_turns);
        let mut phase = 0.0f64;
        for q in frequencies {
            for _ in 0..window_turns {
                phase += 2.0 * std::f64::consts::PI * *q;
                samples.push(phase.sin());
            }
        }
        StreamTrace {
            plane,
            bpm_ip: "test".to_string(),
            stream_key: "HPTEST".to_string(),
            samples,
        }
    }

    fn test_config_with_streams(stream_keys: Vec<String>) -> MonitorConfig {
        MonitorConfig {
            xread_block_ms: 1000,
            reconnect_initial_ms: 2000,
            reconnect_max_ms: 30000,
            min_stream_values: 1,
            injection_start_turn: 0,
            injection_window_turns: 128,
            sliding_window_turns: 128,
            sliding_stride_turns: 64,
            turn_period_us: 1.6,
            plot_time_axes_in_us: false,
            tune_plot_y_min: 0.20,
            tune_plot_y_max: 0.40,
            tune_plot_y_tick_step: 0.02,
            qx_band_min: 0.20,
            qx_band_max: 0.40,
            qy_band_min: 0.20,
            qy_band_max: 0.40,
            min_peak_confidence: 1.1,
            enable_peak_tracking: true,
            qx_track_half_width: 0.02,
            qy_track_half_width: 0.02,
            max_tune_step_per_window: 0.02,
            align_tolerance_ms: 1,
            same_spill_tolerance_ms: 25,
            min_aligned_fraction: 0.70,
            devices: vec![DeviceConfig {
                label: "offline-test".to_string(),
                bpm_ip: "10.0.0.1".to_string(),
                redis: RedisConfig {
                    host: "192.0.2.1".to_string(),
                    port: 6379,
                    db: 0,
                    username: None,
                    password: None,
                },
                trigger_key: "{MUON:BPM:10.0.0.1}:LAST_TRIGGER_TIME".to_string(),
                trigger_fallback_keys: Vec::new(),
                stream_keys,
            }],
        }
    }

    fn f32_sine_payload(tune: f64, turns: usize) -> Vec<u8> {
        let mut payload = Vec::with_capacity(turns * 4);
        for turn in 0..turns {
            let angle = 2.0 * std::f64::consts::PI * tune * turn as f64;
            payload.extend_from_slice(&(angle.sin() as f32).to_le_bytes());
        }
        payload
    }

    fn write_test_captured_bundle(
        root: &Path,
        target_ms: u64,
        stream_defs: &[(&str, &str, f64)],
    ) -> PathBuf {
        let bundle = root.join(format!("spill_{target_ms}"));
        let payload_dir = bundle.join("payloads");
        fs::create_dir_all(&payload_dir).expect("payload dir");

        let mut stream_json = Vec::<String>::new();
        for (idx, (stream_key, plane, tune)) in stream_defs.iter().enumerate() {
            let payload = f32_sine_payload(*tune, 512);
            let file_name = format!("stream_{idx:03}_{plane}_{target_ms}_{idx}.bin");
            fs::write(payload_dir.join(&file_name), &payload).expect("payload write");
            stream_json.push(format!(
                "{{\"device_label\":\"offline-test\",\"bpm_ip\":\"10.0.0.1\",\"stream_key\":\"{}\",\"plane\":\"{}\",\"stream_id\":\"{}-0\",\"stream_ms\":{},\"aligned\":true,\"field_count\":1,\"payload_file\":\"payloads/{}\",\"payload_bytes\":{},\"sample_count\":{},\"checksum_fnv1a64\":\"{}\"}}",
                json_escape(stream_key),
                plane,
                target_ms,
                target_ms,
                file_name,
                payload.len(),
                payload.len() / 4,
                fnv1a64_hex(&payload)
            ));
        }

        let manifest = format!(
            "{{\n  \"schema_version\": 1,\n  \"artifact_type\": \"tbt-monitor.captured-spill\",\n  \"redis_timestamp_ms\": {target_ms},\n  \"target_ms\": {target_ms},\n  \"align_tolerance_ms\": 1,\n  \"min_aligned_fraction\": 0.700000,\n  \"requested_streams\": {},\n  \"latest_observation_count\": {},\n  \"aligned_latest_streams\": {},\n  \"captured_streams\": {},\n  \"payload_checksum_algorithm\": \"fnv1a64\",\n  \"raw_payload_format\": \"redis_stream_field_underscore_little_endian_f32_bytes\",\n  \"stream_inventory\": [],\n  \"latest_observations\": [],\n  \"streams\": [{}],\n  \"warnings\": []\n}}\n",
            stream_defs.len(),
            stream_defs.len(),
            stream_defs.len(),
            stream_defs.len(),
            stream_json.join(",")
        );
        fs::write(bundle.join("manifest.json"), manifest).expect("manifest write");
        bundle
    }

    fn online_style_snapshot_from_fixture(
        config: &MonitorConfig,
        target_ms: u64,
        stream_defs: &[(&str, &str, f64)],
        flash_count: Option<usize>,
    ) -> SpillSnapshot {
        let mut observations = Vec::<TbtObservation>::new();
        let mut traces = Vec::<StreamTrace>::new();

        for (stream_key, plane_label, tune) in stream_defs {
            let plane = match *plane_label {
                "H" => Plane::Horizontal,
                "V" => Plane::Vertical,
                other => panic!("unsupported test plane {other}"),
            };
            let payload = f32_sine_payload(*tune, 512);
            let samples = decode_f32_payload(&[(b"_".to_vec(), payload)])
                .expect("fixture payload should decode");

            observations.push(TbtObservation {
                bpm_ip: "10.0.0.1".to_string(),
                stream_key: (*stream_key).to_string(),
                id: format!("{target_ms}-0"),
                ms: target_ms,
                aligned: true,
            });
            traces.push(StreamTrace {
                plane,
                bpm_ip: "10.0.0.1".to_string(),
                stream_key: (*stream_key).to_string(),
                samples,
            });
        }

        let mut warnings = Vec::<String>::new();
        let horizontal = traces
            .iter()
            .filter(|trace| trace.plane == Plane::Horizontal)
            .cloned()
            .collect::<Vec<_>>();
        let vertical = traces
            .iter()
            .filter(|trace| trace.plane == Plane::Vertical)
            .cloned()
            .collect::<Vec<_>>();

        let h_analysis = analyze_plane(
            Plane::Horizontal,
            horizontal,
            config,
            flash_count,
            &mut warnings,
        )
        .expect("H online-style analysis");
        let v_analysis = analyze_plane(
            Plane::Vertical,
            vertical,
            config,
            flash_count,
            &mut warnings,
        )
        .expect("V online-style analysis");

        SpillSnapshot {
            target_ms,
            observations,
            h_analysis,
            v_analysis,
            warnings,
        }
    }

    fn parity_batch_options(flash_count: Option<usize>) -> BatchOptions {
        BatchOptions {
            count: 1,
            min_confidence: 1.1,
            min_aligned_bpm_count: 1,
            min_per_plane_bpm: 1,
            peak_edge_margin: 0.005,
            record_format: BatchRecordFormat::Both,
            detailed_artifacts: DetailedArtifactsMode::None,
            reference_file: None,
            reference_key: ReferenceKey::TargetMs,
            reference_match_tolerance_ms: 1,
            flash_count,
        }
    }

    fn parity_record_from_snapshot(
        config: &MonitorConfig,
        snapshot: &SpillSnapshot,
        flash_count: Option<usize>,
    ) -> SpillRecord {
        build_spill_record(
            config,
            &parity_batch_options(flash_count),
            snapshot,
            1,
            1,
            snapshot.target_ms,
            "parity-test".to_string(),
        )
        .expect("parity spill record")
    }

    fn assert_opt_f64_close(
        label: &str,
        expected: Option<f64>,
        actual: Option<f64>,
        tolerance: f64,
    ) {
        match (expected, actual) {
            (Some(a), Some(b)) => assert!(
                (a - b).abs() <= tolerance,
                "{label} mismatch: expected {a:.12}, actual {b:.12}, tolerance {tolerance:.3e}"
            ),
            (None, None) => {}
            _ => panic!("{label} presence mismatch: expected {expected:?}, actual {actual:?}"),
        }
    }

    fn assert_string_vec_eq(label: &str, expected: &[String], actual: &[String]) {
        assert_eq!(
            expected, actual,
            "{label} mismatch:\nexpected: {expected:#?}\nactual: {actual:#?}"
        );
    }

    fn assert_parity_record_matches(expected: &SpillRecord, actual: &SpillRecord) {
        assert_eq!(
            expected.target_ms, actual.target_ms,
            "target_ms mismatch: expected {}, actual {}",
            expected.target_ms, actual.target_ms
        );
        assert_eq!(
            expected.aligned_streams, actual.aligned_streams,
            "aligned_streams mismatch"
        );
        assert_eq!(
            expected.used_streams_total, actual.used_streams_total,
            "used_streams_total mismatch"
        );
        assert_eq!(
            expected.used_streams_h, actual.used_streams_h,
            "used_streams_h mismatch"
        );
        assert_eq!(
            expected.used_streams_v, actual.used_streams_v,
            "used_streams_v mismatch"
        );
        assert_eq!(
            expected.consensus_turns_h, actual.consensus_turns_h,
            "consensus_turns_h mismatch"
        );
        assert_eq!(
            expected.consensus_turns_v, actual.consensus_turns_v,
            "consensus_turns_v mismatch"
        );
        assert_eq!(
            expected.flash_count, actual.flash_count,
            "flash_count mismatch"
        );
        assert_eq!(
            expected.quality_label, actual.quality_label,
            "quality_label mismatch"
        );
        assert_eq!(expected.status, actual.status, "status mismatch");

        assert_opt_f64_close(
            "aligned_fraction",
            Some(expected.aligned_fraction),
            Some(actual.aligned_fraction),
            1e-12,
        );
        assert_opt_f64_close(
            "qx_injection",
            expected.qx_injection,
            actual.qx_injection,
            1e-12,
        );
        assert_opt_f64_close(
            "qy_injection",
            expected.qy_injection,
            actual.qy_injection,
            1e-12,
        );
        assert_opt_f64_close("median_qx", expected.median_qx, actual.median_qx, 1e-12);
        assert_opt_f64_close("median_qy", expected.median_qy, actual.median_qy, 1e-12);
        assert_opt_f64_close(
            "median_qx_raw",
            expected.median_qx_raw,
            actual.median_qx_raw,
            1e-12,
        );
        assert_opt_f64_close(
            "median_qy_raw",
            expected.median_qy_raw,
            actual.median_qy_raw,
            1e-12,
        );
        assert_opt_f64_close(
            "median_qx_tracked",
            expected.median_qx_tracked,
            actual.median_qx_tracked,
            1e-12,
        );
        assert_opt_f64_close(
            "median_qy_tracked",
            expected.median_qy_tracked,
            actual.median_qy_tracked,
            1e-12,
        );
        assert_string_vec_eq(
            "quality_flags",
            &expected.quality_flags,
            &actual.quality_flags,
        );
        assert_string_vec_eq("warnings", &expected.warnings, &actual.warnings);
    }

    fn sample_point(center_turn: usize, tune: f64) -> SlidingPoint {
        SlidingPoint {
            center_turn,
            raw_global_tune: Some(tune),
            tracked_local_tune: Some(tune),
            selected_tune: Some(tune),
            raw_global_confidence: Some(2.0),
            selected_confidence: Some(2.0),
            used_global_fallback: false,
            suspicious_step: false,
            step_delta: Some(0.0),
        }
    }

    fn sample_peak(tune: f64, confidence: f64) -> PeakResult {
        PeakResult {
            tune,
            confidence,
            peak_power: 10.0,
            median_power: 1.0,
            prominence: 9.0,
        }
    }

    fn sample_plane_analysis(plane: Plane, tune: f64, center_turn: usize) -> PlaneAnalysis {
        PlaneAnalysis {
            plane,
            traces_total: 1,
            traces_used: 1,
            consensus_turns: 4096,
            participating_bpms: vec!["test".to_string()],
            best_bpm_stream: Some("test_stream".to_string()),
            max_rms_bpm: Some(1.0),
            injection_spectrum: vec![0.0; 32],
            injection_peak: Some(sample_peak(tune, 3.0)),
            sliding: vec![sample_point(center_turn, tune)],
            sliding_spectra: vec![vec![1.0; 32]],
            sliding_fallback_count: 0,
            sliding_suspicious_count: 0,
        }
    }

    #[test]
    fn parse_stream_id_and_ms() {
        assert_eq!(
            parse_stream_id("1772817579168-9866880000000000"),
            Some((1772817579168, 9866880000000000))
        );
        assert_eq!(parse_stream_id("bad-id"), None);
    }

    #[test]
    fn decode_payload_little_endian_f32() {
        let fields = vec![(b"_".to_vec(), vec![0, 0, 0x80, 0x3f, 0, 0, 0, 0x40])];
        let decoded = decode_f32_payload(&fields).expect("payload should decode");
        assert_eq!(decoded.len(), 2);
        assert!((decoded[0] - 1.0).abs() < 1e-9);
        assert!((decoded[1] - 2.0).abs() < 1e-9);
    }

    #[test]
    fn analyze_captured_spill_writes_outputs_without_redis() {
        let dir = std::env::temp_dir().join(format!(
            "tbt-monitor-captured-spill-test-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        let out_dir = dir.join("out");
        fs::create_dir_all(&dir).expect("create temp dir");

        let h_key = "{MUON:BPM:10.0.0.1}:HP101:TBT_POSITION_SCALED".to_string();
        let v_key = "{MUON:BPM:10.0.0.1}:VP101:TBT_POSITION_SCALED".to_string();
        let config = test_config_with_streams(vec![h_key.clone(), v_key.clone()]);
        let bundle = write_test_captured_bundle(
            &dir,
            1772830005123,
            &[(&h_key, "H", 0.25), (&v_key, "V", 0.30)],
        );

        run_analyze_captured_spill(config, &bundle, &out_dir, None)
            .expect("offline captured spill should analyze");

        for name in [
            "spectrum_h.png",
            "spectrum_v.png",
            "spectrogram_h.png",
            "spectrogram_v.png",
            "tune_vs_time.png",
            "tune_validation.png",
            "sliding_tune.csv",
        ] {
            let path = out_dir.join(name);
            let meta = fs::metadata(&path).unwrap_or_else(|_| {
                panic!("expected offline analysis artifact {}", path.display())
            });
            assert!(meta.len() > 0, "expected non-empty artifact {name}");
        }

        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn analyze_captured_spills_writes_batch_outputs_without_redis() {
        let dir = std::env::temp_dir().join(format!(
            "tbt-monitor-captured-spills-test-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        let out_dir = dir.join("out");
        fs::create_dir_all(&dir).expect("create temp dir");

        let h_key = "{MUON:BPM:10.0.0.1}:HP101:TBT_POSITION_SCALED".to_string();
        let v_key = "{MUON:BPM:10.0.0.1}:VP101:TBT_POSITION_SCALED".to_string();
        let config = test_config_with_streams(vec![h_key.clone(), v_key.clone()]);
        write_test_captured_bundle(
            &dir,
            1772830005123,
            &[(&h_key, "H", 0.25), (&v_key, "V", 0.30)],
        );
        write_test_captured_bundle(
            &dir,
            1772830006123,
            &[(&h_key, "H", 0.26), (&v_key, "V", 0.31)],
        );

        let options = BatchOptions {
            count: 2,
            min_confidence: 1.1,
            min_aligned_bpm_count: 1,
            min_per_plane_bpm: 1,
            peak_edge_margin: 0.005,
            record_format: BatchRecordFormat::Both,
            detailed_artifacts: DetailedArtifactsMode::None,
            reference_file: None,
            reference_key: ReferenceKey::TargetMs,
            reference_match_tolerance_ms: 1,
            flash_count: None,
        };

        run_analyze_captured_spills(config, &dir, &out_dir, options)
            .expect("offline captured spill batch should analyze");

        for name in [
            "spills_summary.csv",
            "spills_summary.jsonl",
            "tune_vs_spill.png",
            "confidence_vs_spill.png",
            "composite_waterfall_h.png",
            "composite_waterfall_v.png",
            "batch_summary.md",
            "spill_1_1772830005123_sliding_tune.csv",
            "spill_2_1772830006123_sliding_tune.csv",
        ] {
            let path = out_dir.join(name);
            let meta = fs::metadata(&path)
                .unwrap_or_else(|_| panic!("expected offline batch artifact {}", path.display()));
            assert!(meta.len() > 0, "expected non-empty artifact {name}");
        }

        let csv = fs::read_to_string(out_dir.join("spills_summary.csv")).expect("summary csv");
        assert!(
            csv.contains("captured-spill"),
            "offline batch records should not claim live Redis trigger source"
        );

        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn captured_bundle_matches_online_style_snapshot_for_same_raw_spill() {
        let dir = std::env::temp_dir().join(format!(
            "tbt-monitor-captured-parity-test-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        fs::create_dir_all(&dir).expect("create temp dir");

        let target_ms = 1772830005123;
        let h_key = "{MUON:BPM:10.0.0.1}:HP101:TBT_POSITION_SCALED".to_string();
        let v_key = "{MUON:BPM:10.0.0.1}:VP101:TBT_POSITION_SCALED".to_string();
        let config = test_config_with_streams(vec![h_key.clone(), v_key.clone()]);
        let stream_defs = [(&h_key[..], "H", 0.25), (&v_key[..], "V", 0.30)];
        let flash_count = Some(3);

        let bundle = write_test_captured_bundle(&dir, target_ms, &stream_defs);
        let online_snapshot =
            online_style_snapshot_from_fixture(&config, target_ms, &stream_defs, flash_count);
        let offline_snapshot = analyze_captured_spill_snapshot(&config, &bundle, flash_count)
            .expect("captured bundle should reconstruct offline snapshot");

        assert_eq!(
            online_snapshot.target_ms, offline_snapshot.target_ms,
            "snapshot target_ms mismatch"
        );
        assert_string_vec_eq(
            "snapshot warnings",
            &online_snapshot.warnings,
            &offline_snapshot.warnings,
        );

        let online_record = parity_record_from_snapshot(&config, &online_snapshot, flash_count);
        let offline_record = parity_record_from_snapshot(&config, &offline_snapshot, flash_count);
        assert_parity_record_matches(&online_record, &offline_record);

        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn choose_target_millisecond_uses_mode() {
        let target = choose_target_millisecond(&[10, 11, 11, 12, 12, 12, 5], 0);
        assert_eq!(target, Some(12));
    }

    #[test]
    fn choose_target_millisecond_merges_adjacent_buckets() {
        let target = choose_target_millisecond(&[10, 10, 11, 11, 20, 20, 20], 1);
        assert_eq!(target, Some(11));
    }

    #[test]
    fn consensus_length_works() {
        let traces = vec![
            make_trace(Plane::Horizontal, 0.61, 1024, 0.0),
            make_trace(Plane::Horizontal, 0.61, 1024, 0.2),
            make_trace(Plane::Horizontal, 0.61, 2048, 0.1),
        ];

        assert_eq!(consensus_length(&traces), Some(1024));
    }

    #[test]
    fn fft_peak_recovery_in_band() {
        let q = 0.63;
        let traces = vec![
            make_trace(Plane::Horizontal, q, 1024, 0.0),
            make_trace(Plane::Horizontal, q, 1024, 0.3),
            make_trace(Plane::Horizontal, q, 1024, 0.7),
        ];

        let spectrum = average_spectrum(&traces, 0, 1024).expect("spectrum");
        let peak = pick_peak_in_band(&spectrum, (0.58, 0.72), 2.0).expect("peak in band");

        assert!(
            (peak.tune - q).abs() < 0.01,
            "expected around {q}, got {}",
            peak.tune
        );
    }

    #[test]
    fn pick_peak_rejects_low_confidence() {
        let mut spectrum = vec![1.0f64; 1024];
        spectrum[640] = 1.5; // confidence 1.5 against median 1.0 -> rejected
        let peak = pick_peak_in_band(&spectrum, (0.58, 0.72), 2.0);
        assert!(peak.is_none(), "weak peaks should be rejected");
    }

    #[test]
    fn dc_bin_zeroed_in_average_spectrum() {
        let mut trace = make_trace(Plane::Horizontal, 0.63, 1024, 0.0);
        for value in &mut trace.samples {
            *value += 10.0;
        }
        let spectrum = average_spectrum(&[trace], 0, 1024).expect("spectrum");
        assert_eq!(spectrum[0], 0.0);
        assert!(spectrum.iter().skip(1).any(|v| *v > 0.0));
    }

    #[test]
    fn peak_search_ignores_first_bins() {
        let mut spectrum = vec![0.1f64; 1024];
        spectrum[1] = 100.0;
        spectrum[2] = 90.0;
        spectrum[20] = 50.0;
        let peak = pick_peak_in_band(&spectrum, (0.0, 0.1), 2.0).expect("peak");
        assert!(peak.tune > 0.015, "expected search to ignore bins 0..2");
    }

    #[test]
    fn sliding_tune_tracks_frequency_drift() {
        let n = 4096usize;
        let mut samples = Vec::with_capacity(n);
        let mut phase = 0.0f64;
        for turn in 0..n {
            let q = 0.60 + 0.08 * (turn as f64 / n as f64);
            phase += 2.0 * std::f64::consts::PI * q;
            samples.push(phase.sin());
        }

        let trace = StreamTrace {
            plane: Plane::Horizontal,
            bpm_ip: "test".to_string(),
            stream_key: "HPTEST".to_string(),
            samples,
        };

        let (points, _spectra, diagnostics) = compute_sliding_tunes(
            &[trace],
            n,
            512,
            128,
            None,
            (0.55, 0.75),
            Some(0.60),
            true,
            0.01,
            0.02,
            0.1,
        )
        .expect("sliding");
        assert!(points.len() > 5);
        assert_eq!(diagnostics.total_windows, points.len());

        let first = points
            .iter()
            .find_map(|p| p.selected_tune)
            .expect("first tune");
        let last = points
            .iter()
            .rev()
            .find_map(|p| p.selected_tune)
            .expect("last tune");

        assert!(
            last > first,
            "expected increasing tune trace: first={first} last={last}"
        );
    }

    #[test]
    fn flash_sampling_uses_evenly_spaced_centers() {
        let starts = sliding_window_starts(15_000, 2_048, 256, Some(5));
        let centers = starts
            .iter()
            .map(|start| start + 2_048 / 2)
            .collect::<Vec<_>>();
        assert_eq!(centers, vec![1500, 4500, 7500, 10_500, 13_500]);
    }

    #[test]
    fn flash_sampling_is_bounded_by_window_capacity() {
        let starts = sliding_window_starts(15_000, 2_048, 256, Some(50));
        // Enforce per-spill capacity bound: flashes * window_turns <= sample_count.
        assert_eq!(starts.len(), 15_000 / 2_048);
        let unique = starts.iter().copied().collect::<HashSet<_>>();
        assert_eq!(unique.len(), starts.len());
    }

    #[test]
    fn local_miss_sets_global_fallback_flag() {
        let window = 512usize;
        let trace = make_piecewise_trace(Plane::Horizontal, &[0.62, 0.62, 0.62], window);
        let (points, _spectra, diagnostics) = compute_sliding_tunes(
            &[trace],
            window * 3,
            window,
            window,
            None,
            (0.55, 0.75),
            Some(0.90),
            true,
            0.005,
            0.05,
            0.1,
        )
        .expect("sliding");

        assert_eq!(points.len(), 3);
        assert_eq!(diagnostics.fallback_count, 3);
        assert!(points.iter().all(|p| p.used_global_fallback));
        assert!(points.iter().all(|p| p.tracked_local_tune.is_none()));
    }

    #[test]
    fn suspicious_step_does_not_reseed_tracker_state() {
        let window = 512usize;
        let trace = make_piecewise_trace(Plane::Horizontal, &[0.62, 0.63, 0.64], window);
        let (points, _spectra, diagnostics) = compute_sliding_tunes(
            &[trace],
            window * 3,
            window,
            window,
            None,
            (0.55, 0.75),
            Some(0.90),
            true,
            0.005,
            0.001,
            0.1,
        )
        .expect("sliding");

        assert_eq!(points.len(), 3);
        assert!(diagnostics.suspicious_count >= 2);
        assert_eq!(diagnostics.fallback_count, 3);

        let delta_second = points[1].step_delta.expect("delta2");
        let delta_third = points[2].step_delta.expect("delta3");
        assert!(delta_second > 0.2);
        assert!(delta_third > 0.2);
    }

    #[test]
    fn tracking_disabled_matches_raw_global_peak() {
        let trace = make_trace(Plane::Horizontal, 0.63, 2048, 0.0);
        let (points, _spectra, diagnostics) = compute_sliding_tunes(
            &[trace],
            2048,
            512,
            256,
            None,
            (0.55, 0.75),
            Some(0.63),
            false,
            0.01,
            0.01,
            0.1,
        )
        .expect("sliding");

        assert_eq!(diagnostics.fallback_count, 0);
        assert_eq!(diagnostics.suspicious_count, 0);
        assert_eq!(diagnostics.missing_seed_count, 0);
        for point in &points {
            assert_eq!(point.selected_tune, point.raw_global_tune);
            assert_eq!(point.selected_confidence, point.raw_global_confidence);
        }
    }

    #[test]
    fn missing_seed_uses_raw_only_and_counts_windows() {
        let trace = make_trace(Plane::Vertical, 0.64, 2048, 0.2);
        let (points, _spectra, diagnostics) = compute_sliding_tunes(
            &[trace],
            2048,
            512,
            256,
            None,
            (0.55, 0.75),
            None,
            true,
            0.01,
            0.01,
            0.1,
        )
        .expect("sliding");

        assert_eq!(diagnostics.total_windows, points.len());
        assert_eq!(diagnostics.missing_seed_count, points.len());
        assert_eq!(diagnostics.fallback_count, 0);
        for point in &points {
            assert!(point.tracked_local_tune.is_none());
            assert_eq!(point.selected_tune, point.raw_global_tune);
        }
    }

    #[test]
    fn historical_candidate_ranking_prefers_newest_ms() {
        let mut coverage = HashMap::<u64, HashSet<String>>::new();
        coverage.insert(
            1770,
            HashSet::from(["a".to_string(), "b".to_string(), "c".to_string()]),
        );
        coverage.insert(1780, HashSet::from(["a".to_string(), "b".to_string()]));
        coverage.insert(1760, HashSet::from(["a".to_string()]));

        let observations =
            HashMap::from([(1770u64, 8usize), (1780u64, 2usize), (1760u64, 15usize)]);
        let ranked = rank_historical_candidates(coverage, observations, 1);

        let order = ranked.iter().map(|c| c.target_ms).collect::<Vec<_>>();
        assert_eq!(order, vec![1780, 1770, 1760]);
    }

    #[test]
    fn historical_candidate_ranking_merges_adjacent_milliseconds() {
        let mut coverage = HashMap::<u64, HashSet<String>>::new();
        coverage.insert(
            1000,
            HashSet::from(["s1".to_string(), "s2".to_string(), "s3".to_string()]),
        );
        coverage.insert(
            1001,
            HashSet::from(["s4".to_string(), "s5".to_string(), "s6".to_string()]),
        );

        let observations = HashMap::from([(1000u64, 3usize), (1001u64, 3usize)]);
        let ranked = rank_historical_candidates(coverage, observations, 1);

        assert_eq!(ranked.len(), 1);
        assert_eq!(ranked[0].target_ms, 1001);
        assert_eq!(ranked[0].stream_coverage, 6);
        assert_eq!(ranked[0].observation_count, 6);
    }

    #[test]
    fn historical_candidate_ranking_does_not_merge_across_large_gap() {
        let mut coverage = HashMap::<u64, HashSet<String>>::new();
        coverage.insert(
            1000,
            HashSet::from(["s1".to_string(), "s2".to_string(), "s3".to_string()]),
        );
        coverage.insert(
            1002,
            HashSet::from(["s4".to_string(), "s5".to_string(), "s6".to_string()]),
        );

        let observations = HashMap::from([(1000u64, 3usize), (1002u64, 3usize)]);
        let ranked = rank_historical_candidates(coverage, observations, 1);

        assert_eq!(ranked.len(), 2);
        assert_eq!(ranked[0].target_ms, 1002);
        assert_eq!(ranked[0].stream_coverage, 3);
        assert_eq!(ranked[1].target_ms, 1000);
        assert_eq!(ranked[1].stream_coverage, 3);
    }

    #[test]
    fn batch_summary_carries_historical_diagnostics() {
        let counters = BatchRunCounters {
            unresolved_wakes: 4,
            duplicate_wakes: 1,
            stale_depth_scanned: Some(100),
            historical_candidates_discovered: 35,
            historical_candidates_attempted: 20,
            historical_candidates_skipped: 5,
        };
        let aggregate = summarize_batch(&[], &counters);

        assert_eq!(aggregate.stale_depth_scanned, Some(100));
        assert_eq!(aggregate.historical_candidates_discovered, 35);
        assert_eq!(aggregate.historical_candidates_attempted, 20);
        assert_eq!(aggregate.historical_candidates_skipped, 5);
    }

    #[test]
    fn writes_png_artifacts() {
        let dir = std::env::temp_dir().join(format!(
            "tbt-monitor-tui-test-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        fs::create_dir_all(&dir).expect("create temp dir");

        let spectrum = (0..1024)
            .map(|idx| {
                let x = idx as f64 / 1024.0;
                ((x - 0.62).powi(2) * -600.0).exp()
            })
            .collect::<Vec<_>>();

        let h = dir.join("spectrum_h.png");
        let v = dir.join("spectrum_v.png");
        let t = dir.join("tune_vs_time.png");

        write_spectrum_png(&h, &spectrum, (0.58, 0.74), Some(0.62)).expect("h plot");
        write_spectrum_png(&v, &spectrum, (0.58, 0.74), Some(0.64)).expect("v plot");
        write_tune_trace_png(
            &t,
            &[sample_point(256, 0.61), sample_point(384, 0.615)],
            &[sample_point(256, 0.63), sample_point(384, 0.635)],
            0.58,
            0.74,
            0.01,
            false,
            1.6,
            Some(0.61),
            Some(0.63),
            true,
        )
        .expect("trace plot");

        for file in [&h, &v, &t] {
            let meta = fs::metadata(file).expect("metadata");
            assert!(meta.len() > 0, "expected non-empty png {}", file.display());
        }

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn writes_composite_waterfall_png_with_empty_results() {
        let dir = std::env::temp_dir().join(format!(
            "tbt-monitor-tui-waterfall-test-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        fs::create_dir_all(&dir).expect("create temp dir");

        let config = MonitorConfig {
            xread_block_ms: 1000,
            reconnect_initial_ms: 2000,
            reconnect_max_ms: 30000,
            min_stream_values: 1,
            injection_start_turn: 0,
            injection_window_turns: 1024,
            sliding_window_turns: 2048,
            sliding_stride_turns: 256,
            turn_period_us: 1.6,
            plot_time_axes_in_us: false,
            tune_plot_y_min: 0.58,
            tune_plot_y_max: 0.74,
            tune_plot_y_tick_step: 0.01,
            qx_band_min: 0.58,
            qx_band_max: 0.74,
            qy_band_min: 0.58,
            qy_band_max: 0.74,
            min_peak_confidence: 2.0,
            enable_peak_tracking: true,
            qx_track_half_width: 0.005,
            qy_track_half_width: 0.005,
            max_tune_step_per_window: 0.005,
            align_tolerance_ms: 1,
            same_spill_tolerance_ms: 25,
            min_aligned_fraction: 0.70,
            devices: Vec::new(),
        };

        let h_path = dir.join("composite_waterfall_h.png");
        let v_path = dir.join("composite_waterfall_v.png");

        write_composite_waterfall_png(&h_path, Plane::Horizontal, &config, &[])
            .expect("write horizontal waterfall");
        write_composite_waterfall_png(&v_path, Plane::Vertical, &config, &[])
            .expect("write vertical waterfall");

        for file in [&h_path, &v_path] {
            let meta = fs::metadata(file).expect("metadata");
            assert!(meta.len() > 0, "expected non-empty png {}", file.display());
        }

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn writes_per_spill_spectrogram_png() {
        let dir = std::env::temp_dir().join(format!(
            "tbt-monitor-tui-spectrogram-test-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        fs::create_dir_all(&dir).expect("create temp dir");

        let config = MonitorConfig {
            xread_block_ms: 1000,
            reconnect_initial_ms: 2000,
            reconnect_max_ms: 30000,
            min_stream_values: 1,
            injection_start_turn: 0,
            injection_window_turns: 1024,
            sliding_window_turns: 2048,
            sliding_stride_turns: 256,
            turn_period_us: 1.6,
            plot_time_axes_in_us: false,
            tune_plot_y_min: 0.58,
            tune_plot_y_max: 0.74,
            tune_plot_y_tick_step: 0.01,
            qx_band_min: 0.58,
            qx_band_max: 0.74,
            qy_band_min: 0.58,
            qy_band_max: 0.74,
            min_peak_confidence: 2.0,
            enable_peak_tracking: true,
            qx_track_half_width: 0.005,
            qy_track_half_width: 0.005,
            max_tune_step_per_window: 0.005,
            align_tolerance_ms: 1,
            same_spill_tolerance_ms: 25,
            min_aligned_fraction: 0.70,
            devices: Vec::new(),
        };

        let rows = 48usize;
        let bins = 1024usize;
        let mut spectra = Vec::<Vec<f64>>::new();
        let mut sliding = Vec::<SlidingPoint>::new();
        for idx in 0..rows {
            let tune = 0.595 + 0.015 * (idx as f64 / rows as f64);
            let mut row = vec![1e-3f64; bins];
            for (bin, value) in row.iter_mut().enumerate().take(bins) {
                let t = bin as f64 / bins as f64;
                *value += ((t - tune).powi(2) * -18000.0).exp();
            }
            spectra.push(row);
            sliding.push(SlidingPoint {
                center_turn: 128 + idx * 128,
                raw_global_tune: Some(tune),
                tracked_local_tune: Some(tune),
                selected_tune: Some(tune),
                raw_global_confidence: Some(3.0),
                selected_confidence: Some(3.0),
                used_global_fallback: false,
                suspicious_step: false,
                step_delta: Some(0.0),
            });
        }

        let h_path = dir.join("spectrogram_h.png");
        let v_path = dir.join("spectrogram_v.png");
        write_spectrogram_png(&h_path, Plane::Horizontal, &spectra, &sliding, &config)
            .expect("write H spectrogram");
        write_spectrogram_png(&v_path, Plane::Vertical, &spectra, &sliding, &config)
            .expect("write V spectrogram");

        for file in [&h_path, &v_path] {
            let meta = fs::metadata(file).expect("metadata");
            assert!(meta.len() > 0, "expected non-empty png {}", file.display());
        }

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn writes_tune_validation_png_with_missing_planes() {
        let dir = std::env::temp_dir().join(format!(
            "tbt-monitor-tui-tune-validation-test-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        fs::create_dir_all(&dir).expect("create temp dir");

        let config = MonitorConfig {
            xread_block_ms: 1000,
            reconnect_initial_ms: 2000,
            reconnect_max_ms: 30000,
            min_stream_values: 1,
            injection_start_turn: 0,
            injection_window_turns: 1024,
            sliding_window_turns: 2048,
            sliding_stride_turns: 256,
            turn_period_us: 1.6,
            plot_time_axes_in_us: false,
            tune_plot_y_min: 0.58,
            tune_plot_y_max: 0.74,
            tune_plot_y_tick_step: 0.01,
            qx_band_min: 0.58,
            qx_band_max: 0.74,
            qy_band_min: 0.58,
            qy_band_max: 0.74,
            min_peak_confidence: 2.0,
            enable_peak_tracking: true,
            qx_track_half_width: 0.005,
            qy_track_half_width: 0.005,
            max_tune_step_per_window: 0.005,
            align_tolerance_ms: 1,
            same_spill_tolerance_ms: 25,
            min_aligned_fraction: 0.70,
            devices: Vec::new(),
        };

        let snapshot = SpillSnapshot {
            target_ms: 1772830005123,
            observations: Vec::new(),
            h_analysis: None,
            v_analysis: None,
            warnings: Vec::new(),
        };

        let path = dir.join("tune_validation.png");
        write_tune_validation_png(&path, &snapshot, &config).expect("write tune validation");

        let meta = fs::metadata(&path).expect("metadata");
        assert!(
            meta.len() > 0,
            "expected non-empty tune validation png {}",
            path.display()
        );

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn writes_flash_tune_histogram_png_when_flash_data_exists() {
        let dir = std::env::temp_dir().join(format!(
            "tbt-monitor-tui-flash-hist-test-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        fs::create_dir_all(&dir).expect("create temp dir");

        let config = MonitorConfig {
            xread_block_ms: 1000,
            reconnect_initial_ms: 2000,
            reconnect_max_ms: 30000,
            min_stream_values: 1,
            injection_start_turn: 0,
            injection_window_turns: 1024,
            sliding_window_turns: 2048,
            sliding_stride_turns: 256,
            turn_period_us: 1.6,
            plot_time_axes_in_us: false,
            tune_plot_y_min: 0.58,
            tune_plot_y_max: 0.74,
            tune_plot_y_tick_step: 0.01,
            qx_band_min: 0.58,
            qx_band_max: 0.74,
            qy_band_min: 0.58,
            qy_band_max: 0.74,
            min_peak_confidence: 2.0,
            enable_peak_tracking: true,
            qx_track_half_width: 0.005,
            qy_track_half_width: 0.005,
            max_tune_step_per_window: 0.005,
            align_tolerance_ms: 1,
            same_spill_tolerance_ms: 25,
            min_aligned_fraction: 0.70,
            devices: vec![DeviceConfig {
                label: "test".to_string(),
                bpm_ip: "10.0.0.1".to_string(),
                redis: RedisConfig {
                    host: "127.0.0.1".to_string(),
                    port: 6379,
                    db: 0,
                    username: None,
                    password: None,
                },
                trigger_key: "{MUON:BPM:10.0.0.1}:LAST_TRIGGER_TIME".to_string(),
                trigger_fallback_keys: Vec::new(),
                stream_keys: vec!["{MUON:BPM:10.0.0.1}:HP101:TBT_POSITION_SCALED".to_string()],
            }],
        };

        let snapshot = SpillSnapshot {
            target_ms: 1772830005123,
            observations: vec![TbtObservation {
                bpm_ip: "10.0.0.1".to_string(),
                stream_key: "{MUON:BPM:10.0.0.1}:HP101:TBT_POSITION_SCALED".to_string(),
                id: "1772830005123-0".to_string(),
                ms: 1772830005123,
                aligned: true,
            }],
            h_analysis: Some(sample_plane_analysis(Plane::Horizontal, 0.61, 1500)),
            v_analysis: Some(sample_plane_analysis(Plane::Vertical, 0.63, 1500)),
            warnings: Vec::new(),
        };

        let options = default_free_run_batch_options(1, Some(3));
        let record = build_spill_record(
            &config,
            &options,
            &snapshot,
            1,
            1,
            snapshot.target_ms,
            "unit-test".to_string(),
        )
        .expect("build record");

        let results = vec![BatchSpillResult { record, snapshot }];
        write_tune_histogram_flash_plots(&dir, &results, 3).expect("write flash histograms");

        let flash01 = dir.join("tune_histogram_flash_01.png");
        let meta = fs::metadata(&flash01).expect("flash histogram metadata");
        assert!(meta.len() > 0, "expected non-empty flash histogram png");
        assert!(
            !dir.join("tune_histogram_flash_02.png").exists(),
            "only one flash histogram should be emitted for one sliding point"
        );

        let _ = fs::remove_dir_all(&dir);
    }
}
