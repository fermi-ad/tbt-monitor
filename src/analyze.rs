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

use crate::config::{DeviceConfig, MonitorConfig, RedisConfig};

const DEFAULT_XRANGE_COUNT: usize = 128;
const FREE_RUN_SETTLE_RETRIES: usize = 3;
const FREE_RUN_SETTLE_DELAY_MS: u64 = 40;
const DEFAULT_METHOD_WEAK_CONFIDENCE: f64 = 1.5;
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
    tune_vs_time: PathBuf,
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
    source_mode: SpillSourceMode,
) -> Result<()> {
    config.validate()?;
    fs::create_dir_all(out_dir)
        .with_context(|| format!("failed to create output directory {}", out_dir.display()))?;

    if let SpillSourceMode::Historical { stale_depth } = source_mode {
        return run_analyze_spill_historical(config, out_dir, free_run, stale_depth.max(1));
    }

    if free_run {
        return run_analyze_spill_free_run(config, out_dir);
    }

    run_analyze_spill_once(config, out_dir)
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
    source_mode: SpillSourceMode,
) -> Result<()> {
    validate_study_options(&options)?;
    config.validate()?;
    fs::create_dir_all(out_dir)
        .with_context(|| format!("failed to create output directory {}", out_dir.display()))?;

    if let SpillSourceMode::Historical { stale_depth } = source_mode {
        return run_analyze_study_historical(
            config,
            out_dir,
            options,
            free_run,
            stale_depth.max(1),
        );
    }

    if free_run {
        return run_analyze_study_free_run(config, out_dir, options);
    }

    let snapshot = analyze_spill_snapshot(&config)?;
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

    while results.len() < options.count {
        let signal = match rx.recv() {
            Ok(signal) => signal,
            Err(err) => bail!("batch event channel closed: {err}"),
        };
        attempt_index += 1;

        let snapshot = match analyze_spill_snapshot(&config) {
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

        if !seen_target_ms.insert(snapshot.target_ms) {
            counters.duplicate_wakes += 1;
            continue;
        }

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

        println!(
            "[analyze-spills] spill {}/{} target={} quality={} qx={} qy={} conf_h={} conf_v={}",
            spill_index,
            options.count,
            record.target_ms,
            record.quality_label.label(),
            opt_fmt(record.qx_injection),
            opt_fmt(record.qy_injection),
            opt_fmt(record.confidence_h),
            opt_fmt(record.confidence_v),
        );

        results.push(BatchSpillResult { record, snapshot });
    }

    let has_reference_file = !references.is_empty();
    let has_reference_match = results
        .iter()
        .any(|r| r.record.residual_qx.is_some() || r.record.residual_qy.is_some());

    write_batch_records(out_dir, &results, options.record_format)?;
    write_batch_summary_plots(out_dir, &results, &options, has_reference_match)?;
    write_batch_detailed_artifacts(out_dir, &config, &results, options.detailed_artifacts)?;

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

    for candidate in &candidates {
        if results.len() >= options.count {
            break;
        }

        counters.historical_candidates_attempted += 1;
        if !seen_target_ms.insert(candidate.target_ms) {
            counters.duplicate_wakes += 1;
            continue;
        }

        let snapshot = match analyze_spill_snapshot_at_target(&config, candidate.target_ms) {
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

        println!(
            "[analyze-spills no-beam] spill {}/{} target={} coverage={} qx={} qy={} conf_h={} conf_v={}",
            spill_index,
            options.count,
            record.target_ms,
            candidate.stream_coverage,
            opt_fmt(record.qx_injection),
            opt_fmt(record.qy_injection),
            opt_fmt(record.confidence_h),
            opt_fmt(record.confidence_v),
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
    write_batch_summary_plots(out_dir, &results, &options, has_reference_match)?;
    write_batch_detailed_artifacts(out_dir, &config, &results, options.detailed_artifacts)?;

    let aggregate = summarize_batch(&results, &counters);
    print_batch_console_summary(&aggregate);
    write_batch_summary_markdown(out_dir, &aggregate, &options, has_reference_file)?;

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
    )?;
    write_window_sensitivity_png(
        &output_paths.tune_vs_window_length,
        "WINDOW LENGTH",
        &h_length_sweep,
        &v_length_sweep,
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
    write_method_comparison_png(&output_paths.method_comparison, &method_results)?;

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
) -> Result<()> {
    if config.devices.is_empty() {
        bail!("config has no devices for free-run analyze-phase");
    }

    println!(
        "analyze-phase free-run mode: watching {} devices, running global all-stream snapshots",
        config.devices.len()
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

    let mut last_written_target_ms: Option<u64> = None;
    loop {
        let signal = match rx.recv() {
            Ok(signal) => signal,
            Err(err) => bail!("free-run event channel closed: {err}"),
        };

        let snapshot = match analyze_spill_snapshot_with_retries(&config) {
            Ok(snapshot) => snapshot,
            Err(err) => {
                eprintln!(
                    "[analyze-phase free-run] snapshot after {} {} failed: {}",
                    signal.bpm_ip, signal.event.id, err
                );
                continue;
            }
        };

        if last_written_target_ms == Some(snapshot.target_ms) {
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
    Ok(())
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
    let chosen_ms = choose_target_millisecond(&ms_values).unwrap_or(target_ms);
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
        injection_window_turns: config.injection_window_turns,
        sliding_window_turns: config.sliding_window_turns,
        sliding_stride_turns: config.sliding_stride_turns,
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
sliding_window_turns,sliding_stride_turns,qx_band_min,qx_band_max,qy_band_min,qy_band_max,\
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
            "{{\"spill_index\":{},\"attempt_index\":{},\"spill_uid\":{},\"captured_at_utc\":{},\"target_ms\":{},\"trigger_ms\":{},\"trigger_source\":{},\"aligned_fraction\":{:.6},\"aligned_streams\":{},\"requested_streams\":{},\"used_streams_total\":{},\"used_streams_h\":{},\"used_streams_v\":{},\"consensus_turns_h\":{},\"consensus_turns_v\":{},\"consensus_turns_global\":{},\"injection_start_turn\":{},\"injection_window_turns\":{},\"sliding_window_turns\":{},\"sliding_stride_turns\":{},\"qx_band_min\":{:.6},\"qx_band_max\":{:.6},\"qy_band_min\":{:.6},\"qy_band_max\":{:.6},\"qx_injection\":{},\"qy_injection\":{},\"confidence_h\":{},\"confidence_v\":{},\"median_qx\":{},\"median_qy\":{},\"std_qx\":{},\"std_qy\":{},\"min_qx\":{},\"max_qx\":{},\"min_qy\":{},\"max_qy\":{},\"median_qx_raw\":{},\"std_qx_raw\":{},\"min_qx_raw\":{},\"max_qx_raw\":{},\"median_qy_raw\":{},\"std_qy_raw\":{},\"min_qy_raw\":{},\"max_qy_raw\":{},\"median_qx_tracked\":{},\"std_qx_tracked\":{},\"min_qx_tracked\":{},\"max_qx_tracked\":{},\"median_qy_tracked\":{},\"std_qy_tracked\":{},\"min_qy_tracked\":{},\"max_qy_tracked\":{},\"sliding_fallback_count_h\":{},\"sliding_fallback_count_v\":{},\"sliding_suspicious_count_h\":{},\"sliding_suspicious_count_v\":{},\"max_rms_bpm_h\":{},\"max_rms_bpm_v\":{},\"quality_label\":{},\"status\":{},\"quality_flags\":{},\"warnings\":{},\"participating_bpms_h\":{},\"participating_bpms_v\":{},\"best_bpm_stream_h\":{},\"best_bpm_stream_v\":{},\"ref_qx\":{},\"ref_qy\":{},\"residual_qx\":{},\"residual_qy\":{}}}",
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
    if let Some(stale_depth) = aggregate.stale_depth_scanned {
        println!("  stale_depth scanned: {}", stale_depth);
        println!(
            "  historical candidates discovered (unique ms): {}",
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
    if let Some(stale_depth) = aggregate.stale_depth_scanned {
        lines.push(String::new());
        lines.push("## Historical Source Diagnostics".to_string());
        lines.push(format!("- stale_depth scanned: {}", stale_depth));
        lines.push(format!(
            "- historical candidates discovered (unique ms): {}",
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
) -> Result<()> {
    let selected = select_detailed_spill_indices(results, mode);
    for idx in selected {
        let entry = &results[idx];
        let stem = format!(
            "spill_{}_{}",
            entry.record.spill_index, entry.record.target_ms
        );
        let paths = write_spill_outputs(out_dir, Some(&stem), config, &entry.snapshot)?;
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

fn write_batch_summary_plots(
    out_dir: &Path,
    results: &[BatchSpillResult],
    options: &BatchOptions,
    has_reference: bool,
) -> Result<()> {
    write_tune_vs_spill_png(&out_dir.join("tune_vs_spill.png"), results)?;
    write_confidence_vs_spill_png(
        &out_dir.join("confidence_vs_spill.png"),
        results,
        options.min_confidence,
    )?;
    write_alignment_vs_spill_png(&out_dir.join("alignment_vs_spill.png"), results)?;
    write_tune_scatter_png(&out_dir.join("tune_scatter_qx_qy.png"), results)?;
    write_tune_histogram_png(&out_dir.join("tune_histogram.png"), results)?;
    if has_reference {
        write_tune_residuals_png(&out_dir.join("tune_residuals.png"), results)?;
    }
    Ok(())
}

fn write_tune_vs_spill_png(path: &Path, results: &[BatchSpillResult]) -> Result<()> {
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
    let mut y_values = results
        .iter()
        .flat_map(|r| [r.record.qx_injection, r.record.qy_injection])
        .flatten()
        .filter(|v| v.is_finite())
        .collect::<Vec<_>>();
    if y_values.is_empty() {
        y_values.extend([0.0, 1.0]);
    }
    let y_min = y_values.iter().copied().fold(f64::INFINITY, f64::min);
    let y_max = y_values.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let pad = ((y_max - y_min) * 0.1).max(0.01);
    let y0 = (y_min - pad).clamp(0.0, 1.0);
    let y1 = (y_max + pad).clamp(0.0, 1.0);

    draw_xy_ticks(&mut image, bounds, 1.0, x_max, y0, y1);
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
        y0,
        y1,
        [0, 70, 220],
    );
    draw_polyline_xy(
        &mut image,
        bounds,
        &qy_points,
        1.0,
        x_max,
        y0,
        y1,
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
                y0,
                y1,
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
                y0,
                y1,
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
        "TUNE VS SPILL",
        [0, 0, 0],
        2,
    );

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

fn write_tune_scatter_png(path: &Path, results: &[BatchSpillResult]) -> Result<()> {
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
    let mut qy_vals = points.iter().map(|(_, qy, _)| *qy).collect::<Vec<_>>();
    if qx_vals.is_empty() {
        qx_vals.extend([0.0, 1.0]);
        qy_vals.extend([0.0, 1.0]);
    }
    let x_min = qx_vals.iter().copied().fold(f64::INFINITY, f64::min);
    let x_max = qx_vals.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let y_min = qy_vals.iter().copied().fold(f64::INFINITY, f64::min);
    let y_max = qy_vals.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let x_pad = ((x_max - x_min) * 0.1).max(0.01);
    let y_pad = ((y_max - y_min) * 0.1).max(0.01);
    let x0 = (x_min - x_pad).clamp(0.0, 1.0);
    let x1 = (x_max + x_pad).clamp(0.0, 1.0);
    let y0 = (y_min - y_pad).clamp(0.0, 1.0);
    let y1 = (y_max + y_pad).clamp(0.0, 1.0);

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
            let peak = pick_peak_in_band(&spectrum, plane.plane.tune_band(config));
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
            let peak = pick_peak_in_band(&spectrum, plane.plane.tune_band(config));
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
        let peak = spectrum
            .as_ref()
            .and_then(|s| pick_peak_in_band(s, plane.plane.tune_band(config)));

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
) -> Result<()> {
    write_metric_by_bpm_png(tune_path, metrics, |metric| metric.tune, "TUNE BY BPM")?;
    write_metric_by_bpm_png(
        confidence_path,
        metrics,
        |metric| metric.confidence,
        "CONF BY BPM",
    )?;
    Ok(())
}

fn write_metric_by_bpm_png(
    path: &Path,
    metrics: &[BpmMetric],
    value_fn: impl Fn(&BpmMetric) -> Option<f64>,
    title: &str,
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
    let y0 = (y_min - pad).clamp(-1.0e9, 1.0e9);
    let y1 = (y_max + pad).clamp(-1.0e9, 1.0e9);

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
            pick_peak_in_band(&spectrum, band)
        }
        "UNWEIGHTED" => {
            let spectrum = average_spectrum(&plane.traces, start, length).ok()?;
            pick_peak_in_band(&spectrum, band)
        }
        "WEIGHTED" => {
            let mut spectra = Vec::<(String, Vec<f64>)>::new();
            for trace in &plane.traces {
                if let Some(spectrum) = compute_trace_spectrum(trace, start, length) {
                    spectra.push((trace.stream_key.clone(), spectrum));
                }
            }
            let spectrum = average_weighted_spectra(&spectra, weights)?;
            pick_peak_in_band(&spectrum, band)
        }
        _ => None,
    }
}

fn write_method_comparison_png(path: &Path, methods: &[MethodResult]) -> Result<()> {
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

    draw_xy_ticks(&mut image, tune_bounds, 1.0, 3.0, 0.0, 1.0);
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
        0.0,
        1.0,
        [0, 70, 220],
    );
    draw_polyline_xy(
        &mut image,
        tune_bounds,
        &v_tune,
        1.0,
        3.0,
        0.0,
        1.0,
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

    draw_xy_ticks(&mut image, tune_bounds, x_min, x_max, 0.0, 1.0);
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
        0.0,
        1.0,
        [0, 70, 220],
    );
    draw_polyline_xy(
        &mut image,
        tune_bounds,
        &v_tune,
        x_min,
        x_max,
        0.0,
        1.0,
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
    stale_depth: usize,
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
            let snapshot = match analyze_spill_snapshot_at_target(&config, candidate.target_ms) {
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

            let paths = write_spill_outputs(out_dir, None, &config, &snapshot)?;
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
    for candidate in &candidates {
        attempted += 1;
        let snapshot = match analyze_spill_snapshot_at_target(&config, candidate.target_ms) {
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
        match write_spill_outputs(out_dir, Some(&stem), &config, &snapshot) {
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
        "  historical candidates discovered (unique ms): {}",
        candidates.len()
    );
    println!("  candidates attempted: {}", attempted);
    println!("  candidates skipped unresolved: {}", skipped);
    println!("  successful analyses: {}", successful);

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
            let snapshot = match analyze_spill_snapshot_at_target(&config, candidate.target_ms) {
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
    for candidate in &candidates {
        attempted += 1;
        let snapshot = match analyze_spill_snapshot_at_target(&config, candidate.target_ms) {
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
        "  historical candidates discovered (unique ms): {}",
        candidates.len()
    );
    println!("  candidates attempted: {}", attempted);
    println!("  candidates skipped unresolved: {}", skipped);
    println!("  successful analyses: {}", successful);

    if successful == 0 {
        bail!(
            "no historical spill candidates produced usable analyze-phase outputs (attempted {}, stale_depth={})",
            attempted,
            stale_depth
        );
    }

    Ok(())
}

fn run_analyze_spill_once(config: MonitorConfig, out_dir: &Path) -> Result<()> {
    let snapshot = analyze_spill_snapshot(&config)?;
    let paths = write_spill_outputs(out_dir, None, &config, &snapshot)?;

    let _ = print_summary(&config, &snapshot, &paths, "analyze-spill summary", true);

    Ok(())
}

fn run_analyze_spill_free_run(config: MonitorConfig, out_dir: &Path) -> Result<()> {
    if config.devices.is_empty() {
        bail!("config has no devices for free-run analyze-spill");
    }

    println!(
        "analyze-spill free-run mode: watching {} devices, running global all-stream snapshots",
        config.devices.len()
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

    let mut last_written_target_ms: Option<u64> = None;
    loop {
        let signal = match rx.recv() {
            Ok(signal) => signal,
            Err(err) => bail!("free-run event channel closed: {err}"),
        };

        let snapshot = match analyze_spill_snapshot_with_retries(&config) {
            Ok(snapshot) => snapshot,
            Err(err) => {
                eprintln!(
                    "[free-run] snapshot after {} {} failed: {}",
                    signal.bpm_ip, signal.event.id, err
                );
                continue;
            }
        };

        if last_written_target_ms == Some(snapshot.target_ms) {
            continue;
        }

        let stem = format!("spill_{}", snapshot.target_ms);
        match write_spill_outputs(out_dir, Some(&stem), &config, &snapshot) {
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

fn analyze_spill_snapshot_with_retries(config: &MonitorConfig) -> Result<SpillSnapshot> {
    let mut last_error: Option<anyhow::Error> = None;
    for attempt in 0..=FREE_RUN_SETTLE_RETRIES {
        match analyze_spill_snapshot(config) {
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

fn analyze_spill_snapshot(config: &MonitorConfig) -> Result<SpillSnapshot> {
    let mut warnings = Vec::<String>::new();

    let mut tbt_observations = collect_latest_tbt_observations(config, &mut warnings)?;
    if tbt_observations.is_empty() {
        bail!("no latest TBT observations were available from any configured device");
    }

    let target_ms =
        choose_target_millisecond(&tbt_observations.iter().map(|o| o.ms).collect::<Vec<_>>())
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

    let h_analysis = analyze_plane(Plane::Horizontal, horizontal, config, &mut warnings)?;
    let v_analysis = analyze_plane(Plane::Vertical, vertical, config, &mut warnings)?;

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
) -> Result<SpillSnapshot> {
    let mut warnings = Vec::<String>::new();

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

    let h_analysis = analyze_plane(Plane::Horizontal, horizontal, config, &mut warnings)?;
    let v_analysis = analyze_plane(Plane::Vertical, vertical, config, &mut warnings)?;

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

fn write_spill_outputs(
    out_dir: &Path,
    stem: Option<&str>,
    config: &MonitorConfig,
    snapshot: &SpillSnapshot,
) -> Result<SpillOutputPaths> {
    let (h_name, v_name, t_name, s_name) = match stem {
        Some(stem) => (
            format!("{stem}_spectrum_h.png"),
            format!("{stem}_spectrum_v.png"),
            format!("{stem}_tune_vs_time.png"),
            format!("{stem}_sliding_tune.csv"),
        ),
        None => (
            "spectrum_h.png".to_string(),
            "spectrum_v.png".to_string(),
            "tune_vs_time.png".to_string(),
            "sliding_tune.csv".to_string(),
        ),
    };

    let paths = SpillOutputPaths {
        spectrum_h: out_dir.join(h_name),
        spectrum_v: out_dir.join(v_name),
        tune_vs_time: out_dir.join(t_name),
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
    )?;

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

    Ok(rank_historical_candidates(coverage_map, observation_count))
}

fn rank_historical_candidates(
    coverage_map: HashMap<u64, HashSet<String>>,
    observation_count: HashMap<u64, usize>,
) -> Vec<HistoricalCandidate> {
    let mut candidates = observation_count
        .into_iter()
        .map(|(target_ms, count)| HistoricalCandidate {
            target_ms,
            stream_coverage: coverage_map
                .get(&target_ms)
                .map(|set| set.len())
                .unwrap_or(0),
            observation_count: count,
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

    let required_turns = config
        .injection_start_turn
        .saturating_add(config.injection_window_turns)
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
        if let Some(rms) = trace_window_rms(
            trace,
            config.injection_start_turn,
            config.injection_window_turns,
        ) {
            max_rms_bpm = Some(max_rms_bpm.map_or(rms, |v| v.max(rms)));
        }
        if let Some(spectrum) = compute_trace_spectrum(
            trace,
            config.injection_start_turn,
            config.injection_window_turns,
        ) {
            if let Some(peak) = pick_peak_in_band(&spectrum, plane.tune_band(config)) {
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
        config.injection_window_turns,
    )
    .with_context(|| format!("failed injection spectrum for plane {}", plane.label()))?;

    let injection_peak = pick_peak_in_band(&injection_spectrum, plane.tune_band(config));
    if injection_peak.is_none() {
        warnings.push(format!(
            "plane {} had no injection peak in configured tune band",
            plane.label()
        ));
    }

    let (sliding, diagnostics) = compute_sliding_tunes(
        &filtered,
        consensus_turns,
        config.sliding_window_turns,
        config.sliding_stride_turns,
        plane.tune_band(config),
        injection_peak.as_ref().map(|peak| peak.tune),
        config.enable_peak_tracking,
        plane.track_half_width(config),
        config.max_tune_step_per_window,
    )?;

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
        sliding_fallback_count: diagnostics.fallback_count,
        sliding_suspicious_count: diagnostics.suspicious_count,
    }))
}

fn compute_sliding_tunes(
    traces: &[StreamTrace],
    total_turns: usize,
    window_turns: usize,
    stride_turns: usize,
    band: (f64, f64),
    seed_tune: Option<f64>,
    enable_tracking: bool,
    track_half_width: f64,
    max_step_per_window: f64,
) -> Result<(Vec<SlidingPoint>, SlidingDiagnostics)> {
    if window_turns > total_turns {
        return Ok((
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
    let last_start = total_turns - window_turns;
    let mut previous_trusted_tune = seed_tune;
    let mut fallback_count = 0usize;
    let mut suspicious_count = 0usize;
    let mut missing_seed_count = 0usize;

    for start in (0..=last_start).step_by(stride_turns.max(1)) {
        let spectrum = average_spectrum(traces, start, window_turns)?;
        let raw_peak = pick_peak_in_band(&spectrum, band);

        let mut tracked_local_peak: Option<PeakResult> = None;
        let selected_peak: Option<PeakResult>;
        let mut used_global_fallback = false;
        let mut suspicious_step = false;
        let mut step_delta = None;

        if !enable_tracking {
            selected_peak = raw_peak.clone();
        } else if let Some(trusted) = previous_trusted_tune {
            if let Some(local_band) = local_tracking_band(band, trusted, track_half_width) {
                tracked_local_peak = pick_peak_in_band(&spectrum, local_band);
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
    }

    Ok((
        points,
        SlidingDiagnostics {
            fallback_count,
            suspicious_count,
            missing_seed_count,
            total_windows: (last_start / stride_turns.max(1)) + 1,
        },
    ))
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

        let spectrum = spectrum_power(&signal);
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

fn pick_peak_in_band(spectrum: &[f64], band: (f64, f64)) -> Option<PeakResult> {
    let n = spectrum.len();
    if n < 8 {
        return None;
    }

    let mut start_idx = (band.0 * n as f64).floor() as usize;
    let mut end_idx = (band.1 * n as f64).ceil() as usize;

    start_idx = start_idx.clamp(1, n.saturating_sub(2));
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

fn classify_plane(key: &str) -> Option<Plane> {
    if key.contains(":HP") && key.ends_with(":TBT_POSITION_SCALED") {
        Some(Plane::Horizontal)
    } else if key.contains(":VP") && key.ends_with(":TBT_POSITION_SCALED") {
        Some(Plane::Vertical)
    } else {
        None
    }
}

fn choose_target_millisecond(values: &[u64]) -> Option<u64> {
    if values.is_empty() {
        return None;
    }

    let mut counts = HashMap::<u64, usize>::new();
    for value in values {
        *counts.entry(*value).or_insert(0) += 1;
    }

    counts
        .into_iter()
        .max_by(|(ms_a, count_a), (ms_b, count_b)| {
            count_a.cmp(count_b).then_with(|| ms_a.cmp(ms_b))
        })
        .map(|(ms, _)| ms)
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
        .map(|point| point.center_turn as f64)
        .fold(1.0f64, f64::max);

    let mut values = horizontal
        .iter()
        .chain(vertical.iter())
        .filter_map(|point| point.selected_tune)
        .filter(|tune| tune.is_finite())
        .collect::<Vec<_>>();

    let (y_min, y_max) = if values.is_empty() {
        (0.0, 1.0)
    } else {
        values.sort_by(|a, b| a.partial_cmp(b).unwrap_or(Ordering::Equal));
        let min = values[0];
        let max = values[values.len() - 1];
        let pad = ((max - min) * 0.1).max(0.01);
        ((min - pad).clamp(0.0, 1.0), (max + pad).clamp(0.0, 1.0))
    };

    draw_trace_ticks(&mut image, bounds, x_max, y_min, y_max);

    for segment in finite_segments(horizontal) {
        let normalized = segment
            .iter()
            .map(|(x_turn, y_tune)| {
                let x = (x_turn / x_max).clamp(0.0, 1.0);
                let y = ((y_tune - y_min) / (y_max - y_min).max(1e-12)).clamp(0.0, 1.0);
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
                let x = (x_turn / x_max).clamp(0.0, 1.0);
                let y = ((y_tune - y_min) / (y_max - y_min).max(1e-12)).clamp(0.0, 1.0);
                (x, y)
            })
            .collect::<Vec<_>>();
        draw_polyline_normalized(&mut image, bounds, &normalized, [220, 0, 0]);
        if let Some(&(x_last, y_last)) = normalized.last() {
            let (x_px, y_px) = map_point(&image, bounds, (x_last, y_last));
            draw_text_small(&mut image, x_px + 6, y_px - 6, "V", [220, 0, 0], 2);
        }
    }

    let legend_x = image.width as i32 - bounds.right as i32 - 160;
    draw_line_legend(
        &mut image,
        (legend_x, bounds.top as i32 + 8),
        &[([0, 70, 220], "H"), ([220, 0, 0], "V")],
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

    if verbose_observations {
        lines.push("TBT latest-id samples:".to_string());
        for obs in observations {
            lines.push(format!(
                "  {} {} {} [{}]",
                obs.bpm_ip,
                obs.stream_key,
                obs.id,
                if obs.aligned { "aligned" } else { "off-target" }
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
    lines.push(format!("  {}", paths.tune_vs_time.display()));
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

fn draw_trace_ticks(image: &mut RgbImage, bounds: PlotBounds, x_max: f64, y_min: f64, y_max: f64) {
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

    for i in 0..=5 {
        let y_norm = i as f64 / 5.0;
        let y = y_from_norm(image, bounds, y_norm);
        image.draw_line(x_axis - tick_len, y, x_axis, y, axis_color);
        let value = y_min + (y_max - y_min) * y_norm;
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
    fn choose_target_millisecond_uses_mode() {
        let target = choose_target_millisecond(&[10, 11, 11, 12, 12, 12, 5]);
        assert_eq!(target, Some(12));
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
        let peak = pick_peak_in_band(&spectrum, (0.58, 0.72)).expect("peak in band");

        assert!(
            (peak.tune - q).abs() < 0.01,
            "expected around {q}, got {}",
            peak.tune
        );
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

        let (points, diagnostics) = compute_sliding_tunes(
            &[trace],
            n,
            512,
            128,
            (0.55, 0.75),
            Some(0.60),
            true,
            0.01,
            0.02,
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
    fn local_miss_sets_global_fallback_flag() {
        let window = 512usize;
        let trace = make_piecewise_trace(Plane::Horizontal, &[0.62, 0.62, 0.62], window);
        let (points, diagnostics) = compute_sliding_tunes(
            &[trace],
            window * 3,
            window,
            window,
            (0.55, 0.75),
            Some(0.90),
            true,
            0.005,
            0.05,
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
        let (points, diagnostics) = compute_sliding_tunes(
            &[trace],
            window * 3,
            window,
            window,
            (0.55, 0.75),
            Some(0.90),
            true,
            0.005,
            0.001,
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
        let (points, diagnostics) = compute_sliding_tunes(
            &[trace],
            2048,
            512,
            256,
            (0.55, 0.75),
            Some(0.63),
            false,
            0.01,
            0.01,
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
        let (points, diagnostics) = compute_sliding_tunes(
            &[trace],
            2048,
            512,
            256,
            (0.55, 0.75),
            None,
            true,
            0.01,
            0.01,
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
        let ranked = rank_historical_candidates(coverage, observations);

        let order = ranked.iter().map(|c| c.target_ms).collect::<Vec<_>>();
        assert_eq!(order, vec![1780, 1770, 1760]);
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

        write_spectrum_png(&h, &spectrum, (0.58, 0.72), Some(0.62)).expect("h plot");
        write_spectrum_png(&v, &spectrum, (0.58, 0.72), Some(0.64)).expect("v plot");
        write_tune_trace_png(
            &t,
            &[sample_point(256, 0.61), sample_point(384, 0.615)],
            &[sample_point(256, 0.63), sample_point(384, 0.635)],
        )
        .expect("trace plot");

        for file in [&h, &v, &t] {
            let meta = fs::metadata(file).expect("metadata");
            assert!(meta.len() > 0, "expected non-empty png {}", file.display());
        }

        let _ = fs::remove_dir_all(&dir);
    }
}
