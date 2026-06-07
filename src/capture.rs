//! Raw spill capture workflows.
//!
//! This module keeps Redis acquisition separate from tune analysis. Capture
//! commands synchronize on the same stream-id millisecond policy used by the
//! analysis path, then persist complete raw `_` payload bytes and metadata for
//! later offline analysis.

use std::cmp::Ordering;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Sender};
use std::thread;
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use redis::streams::{StreamReadOptions, StreamReadReply};
use redis::{Commands, Connection};
use serde_json::Value;

use crate::config::{DeviceConfig, MonitorConfig, RedisConfig};

const CAPTURE_SCHEMA_VERSION: u32 = 1;
const CAPTURE_ARTIFACT_TYPE: &str = "tbt-monitor.captured-spill";
const RAW_PAYLOAD_FORMAT: &str = "redis_stream_field_underscore_little_endian_f32_bytes";
const PAYLOAD_CHECKSUM_ALGORITHM: &str = "fnv1a64";
const DEFAULT_XRANGE_COUNT: usize = 128;
const FREE_RUN_SETTLE_RETRIES: usize = 3;
const FREE_RUN_SETTLE_DELAY_MS: u64 = 40;
const DEFAULT_ASSESS_EVENTS: usize = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
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
}

#[derive(Debug, Clone)]
struct CaptureObservation {
    bpm_ip: String,
    stream_key: String,
    id: String,
    ms: u64,
    aligned: bool,
}

#[derive(Debug, Clone)]
struct CaptureStreamInventoryEntry {
    device_label: String,
    bpm_ip: String,
    stream_key: String,
    plane: Plane,
}

#[derive(Debug, Clone)]
struct CapturedStreamEntry {
    device_label: String,
    bpm_ip: String,
    stream_key: String,
    plane: Plane,
    stream_id: String,
    stream_ms: u64,
    aligned: bool,
    field_count: usize,
    payload: Option<Vec<u8>>,
    payload_file: Option<String>,
    payload_bytes: usize,
    sample_count: Option<usize>,
    checksum_fnv1a64: Option<String>,
}

#[derive(Debug, Clone)]
struct CaptureStreamSpec {
    stream_key: String,
    plane: Plane,
}

#[derive(Debug, Clone)]
struct CaptureWake {
    bpm_ip: String,
    stream_id: String,
    ms: u64,
}

#[derive(Debug, Clone)]
struct CapturedSpill {
    schema_version: u32,
    artifact_type: &'static str,
    redis_timestamp_ms: u64,
    target_ms: u64,
    align_tolerance_ms: u64,
    same_spill_tolerance_ms: u64,
    min_aligned_fraction: f64,
    wake: Option<CaptureWake>,
    requested_streams: usize,
    stream_inventory: Vec<CaptureStreamInventoryEntry>,
    latest_observations: Vec<CaptureObservation>,
    streams: Vec<CapturedStreamEntry>,
    warnings: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct CaptureWriteResult {
    pub target_ms: u64,
    pub redis_timestamp_ms: u64,
    pub bundle_dir: PathBuf,
    pub manifest_path: PathBuf,
    pub summary_path: PathBuf,
    pub requested_streams: usize,
    pub latest_observations: usize,
    pub latest_same_spill_streams: usize,
    pub captured_streams: usize,
    pub warning_count: usize,
    diagnostics: CaptureDiagnostics,
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
struct TimingSummary {
    count: usize,
    min_delta_ms: Option<i64>,
    max_delta_ms: Option<i64>,
    median_abs_delta_ms: Option<f64>,
    max_abs_delta_ms: Option<u64>,
    same_spill_count: usize,
    stale_count: usize,
    ahead_count: usize,
    delta_counts: Vec<TimingDeltaCount>,
}

#[derive(Debug, Clone)]
struct TimingDeltaCount {
    delta_ms: i64,
    count: usize,
}

#[derive(Debug, Clone)]
struct StreamDiagnosticRow {
    device_label: String,
    bpm_ip: String,
    plane: String,
    stream_key: String,
    captured_status: String,
    latest_poll_status: String,
    primary_reason: String,
    captured_stream_id: Option<String>,
    captured_ms: Option<u64>,
    captured_delta_ms: Option<i64>,
    latest_id: Option<String>,
    latest_ms: Option<u64>,
    latest_delta_ms: Option<i64>,
    payload_bytes: Option<usize>,
    sample_count: Option<usize>,
}

#[derive(Debug, Clone)]
struct DigitizerDiagnosticRow {
    device_label: String,
    bpm_ip: String,
    status: String,
    configured_streams: usize,
    complete_streams: usize,
    same_spill_streams: usize,
    missing_capture_streams: usize,
    stale_capture_streams: usize,
    ahead_capture_streams: usize,
    payload_issue_streams: usize,
    latest_stale_streams: usize,
    latest_missing_streams: usize,
    latest_ahead_streams: usize,
    suspect: bool,
    latest_poll_suspect: bool,
}

#[derive(Debug, Clone)]
struct CaptureDiagnostics {
    same_spill_tolerance_ms: u64,
    status: String,
    requested_streams: usize,
    captured_streams: usize,
    complete_streams: usize,
    same_spill_streams: usize,
    missing_streams: usize,
    stale_capture_streams: usize,
    ahead_capture_streams: usize,
    payload_issue_streams: usize,
    latest_stale_streams: usize,
    latest_missing_streams: usize,
    latest_ahead_streams: usize,
    suspect_digitizers: usize,
    latest_poll_suspect_digitizers: usize,
    wake_delta_ms: Option<i64>,
    captured_timing: TimingSummary,
    latest_timing: TimingSummary,
    streams: Vec<StreamDiagnosticRow>,
    digitizers: Vec<DigitizerDiagnosticRow>,
}

#[derive(Debug, Clone)]
struct AssessStreamRow {
    assessment_index: usize,
    assessment_kind: String,
    target_ms: Option<u64>,
    device_label: String,
    bpm_ip: String,
    plane: String,
    stream_key: String,
    latest_status: String,
    latest_id: Option<String>,
    latest_ms: Option<u64>,
    latest_delta_ms: Option<i64>,
}

#[derive(Debug, Clone)]
struct AssessDigitizerRow {
    assessment_index: usize,
    assessment_kind: String,
    target_ms: Option<u64>,
    device_label: String,
    bpm_ip: String,
    status: String,
    configured_streams: usize,
    same_spill_streams: usize,
    latest_stale_streams: usize,
    latest_missing_streams: usize,
    latest_ahead_streams: usize,
    suspect: bool,
}

#[derive(Debug, Clone)]
struct AssessSnapshot {
    assessment_index: usize,
    assessment_kind: String,
    target_ms: Option<u64>,
    stream_rows: Vec<AssessStreamRow>,
    digitizer_rows: Vec<AssessDigitizerRow>,
    warnings: Vec<String>,
}

pub fn run_capture_spill(config: MonitorConfig, out_dir: &Path) -> Result<()> {
    let spill = capture_latest_spill_with_retries(&config)?;
    let result = write_capture_bundle(out_dir, &spill)?;
    print_capture_summary(&result, &spill, "capture-spill summary");
    write_capture_index(out_dir, std::slice::from_ref(&result), 0, 0)?;
    write_capture_diagnostics_outputs(out_dir, std::slice::from_ref(&result))?;
    Ok(())
}

pub fn run_capture_spills(
    config: MonitorConfig,
    out_dir: &Path,
    free_run: bool,
    count: Option<usize>,
) -> Result<()> {
    if !free_run {
        bail!("capture-spills requires --free-run; add --count N for a bounded run");
    }
    if matches!(count, Some(0)) {
        bail!("--count must be >= 1 for capture-spills");
    }
    run_capture_spills_free_run(config, out_dir, count)
}

pub fn run_diagnose_captures(
    bundles_dir: &Path,
    out_dir: &Path,
    same_spill_tolerance_ms: Option<u64>,
) -> Result<()> {
    fs::create_dir_all(out_dir)
        .with_context(|| format!("failed to create output directory {}", out_dir.display()))?;
    let manifests = discover_capture_manifests(bundles_dir)?;
    if manifests.is_empty() {
        bail!(
            "no captured-spill manifests found under {}",
            bundles_dir.display()
        );
    }

    let mut results = Vec::<CaptureWriteResult>::new();
    for manifest_path in manifests {
        let bundle_dir = manifest_path
            .parent()
            .ok_or_else(|| anyhow!("manifest path {} has no parent", manifest_path.display()))?
            .to_path_buf();
        let spill =
            load_spill_from_manifest_for_diagnostics(&manifest_path, same_spill_tolerance_ms)?;
        let diagnostics = build_capture_diagnostics(&spill, &spill.streams);
        results.push(CaptureWriteResult {
            target_ms: spill.target_ms,
            redis_timestamp_ms: spill.redis_timestamp_ms,
            bundle_dir,
            manifest_path: manifest_path.clone(),
            summary_path: manifest_path
                .parent()
                .unwrap_or_else(|| Path::new(""))
                .join("capture_summary.txt"),
            requested_streams: spill.requested_streams,
            latest_observations: spill.latest_observations.len(),
            latest_same_spill_streams: spill
                .latest_observations
                .iter()
                .filter(|obs| obs.aligned)
                .count(),
            captured_streams: spill.streams.len(),
            warning_count: spill.warnings.len(),
            diagnostics,
        });
    }

    results.sort_by_key(|result| result.target_ms);
    write_capture_index(out_dir, &results, 0, 0)?;
    write_capture_diagnostics_outputs(out_dir, &results)?;
    println!(
        "diagnose-captures: wrote diagnostics for {} manifests to {}",
        results.len(),
        out_dir.display()
    );
    Ok(())
}

pub fn run_assess(config: MonitorConfig, out_dir: &Path, events: Option<usize>) -> Result<()> {
    let event_count = events.unwrap_or(DEFAULT_ASSESS_EVENTS);
    fs::create_dir_all(out_dir)
        .with_context(|| format!("failed to create output directory {}", out_dir.display()))?;

    let mut snapshots = Vec::<AssessSnapshot>::new();
    snapshots.push(collect_assess_snapshot(&config, 0, "initial_snapshot")?);

    if event_count > 0 {
        let (tx, rx) = mpsc::channel::<FreeRunSignal>();
        for device in config.devices.clone() {
            let tx_worker = tx.clone();
            let reconnect_initial_ms = config.reconnect_initial_ms;
            let reconnect_max_ms = config.reconnect_max_ms;
            thread::spawn(move || {
                if let Err(err) = run_free_run_watch_worker(
                    device,
                    reconnect_initial_ms,
                    reconnect_max_ms,
                    tx_worker,
                ) {
                    eprintln!("assess watch worker exited: {err}");
                }
            });
        }
        drop(tx);

        let mut seen_targets = HashSet::<u64>::new();
        for snapshot in &snapshots {
            if let Some(target_ms) = snapshot.target_ms {
                seen_targets.insert(target_ms);
            }
        }
        while snapshots.len() < event_count + 1 {
            let signal = rx
                .recv()
                .with_context(|| "assess event channel closed before requested events")?;
            let snapshot = collect_assess_snapshot(
                &config,
                snapshots.len(),
                &format!("event_after_{}_{}", signal.bpm_ip, signal.event.id),
            )?;
            if let Some(target_ms) = snapshot.target_ms {
                if target_seen_within_tolerance(
                    &seen_targets,
                    target_ms,
                    config.same_spill_tolerance_ms,
                ) {
                    continue;
                }
                seen_targets.insert(target_ms);
            }
            snapshots.push(snapshot);
        }
    }

    write_assess_outputs(out_dir, &snapshots)?;
    println!(
        "assess: wrote {} snapshots to {}",
        snapshots.len(),
        out_dir.display()
    );
    Ok(())
}

fn collect_assess_snapshot(
    config: &MonitorConfig,
    assessment_index: usize,
    assessment_kind: &str,
) -> Result<AssessSnapshot> {
    let mut warnings = Vec::<String>::new();
    let mut observations = collect_latest_tbt_observations(config, &mut warnings)?;
    let target_ms = choose_target_millisecond(
        &observations
            .iter()
            .filter(|obs| classify_plane(&obs.stream_key).is_some())
            .map(|obs| obs.ms)
            .collect::<Vec<_>>(),
        target_bucket_tolerance_ms(config),
    );
    if let Some(target_ms) = target_ms {
        for obs in &mut observations {
            obs.aligned = abs_diff_u64(obs.ms, target_ms) <= config.same_spill_tolerance_ms;
        }
    } else {
        warnings.push("no latest TBT observations were available".to_string());
    }
    Ok(build_assess_snapshot(
        config,
        assessment_index,
        assessment_kind,
        target_ms,
        observations,
        warnings,
    ))
}

fn build_assess_snapshot(
    config: &MonitorConfig,
    assessment_index: usize,
    assessment_kind: &str,
    target_ms: Option<u64>,
    observations: Vec<CaptureObservation>,
    warnings: Vec<String>,
) -> AssessSnapshot {
    let inventory = collect_stream_inventory(config);
    let latest_by_key = observations
        .iter()
        .map(|obs| (stream_key_tuple(&obs.bpm_ip, &obs.stream_key), obs))
        .collect::<HashMap<_, _>>();
    let mut stream_rows = Vec::<AssessStreamRow>::new();
    for entry in &inventory {
        let key = stream_key_tuple(&entry.bpm_ip, &entry.stream_key);
        let obs = latest_by_key.get(&key).copied();
        let (latest_status, latest_delta_ms) = match (obs, target_ms) {
            (Some(obs), Some(target_ms)) => {
                latest_status(Some(obs), target_ms, config.same_spill_tolerance_ms, false)
            }
            (Some(_), None) => ("COMPLETE".to_string(), None),
            (None, _) => ("LATEST_MISSING".to_string(), None),
        };
        stream_rows.push(AssessStreamRow {
            assessment_index,
            assessment_kind: assessment_kind.to_string(),
            target_ms,
            device_label: entry.device_label.clone(),
            bpm_ip: entry.bpm_ip.clone(),
            plane: entry.plane.label().to_string(),
            stream_key: entry.stream_key.clone(),
            latest_status,
            latest_id: obs.map(|obs| obs.id.clone()),
            latest_ms: obs.map(|obs| obs.ms),
            latest_delta_ms,
        });
    }

    let mut digitizer_map = HashMap::<String, AssessDigitizerRow>::new();
    for row in &stream_rows {
        let entry = digitizer_map
            .entry(row.bpm_ip.clone())
            .or_insert_with(|| AssessDigitizerRow {
                assessment_index,
                assessment_kind: assessment_kind.to_string(),
                target_ms,
                device_label: row.device_label.clone(),
                bpm_ip: row.bpm_ip.clone(),
                status: "Complete".to_string(),
                configured_streams: 0,
                same_spill_streams: 0,
                latest_stale_streams: 0,
                latest_missing_streams: 0,
                latest_ahead_streams: 0,
                suspect: false,
            });
        entry.configured_streams += 1;
        match row.latest_status.as_str() {
            "COMPLETE" => entry.same_spill_streams += 1,
            "LATEST_STALE" | "LATEST_STALE_BUT_CAPTURED_OK" => entry.latest_stale_streams += 1,
            "LATEST_MISSING" => entry.latest_missing_streams += 1,
            "LATEST_AHEAD" => entry.latest_ahead_streams += 1,
            _ => {}
        }
    }
    let mut digitizer_rows = digitizer_map.into_values().collect::<Vec<_>>();
    digitizer_rows.sort_by(|a, b| a.bpm_ip.cmp(&b.bpm_ip));
    for row in &mut digitizer_rows {
        row.suspect = row.latest_stale_streams > 0
            || row.latest_missing_streams > 0
            || row.latest_ahead_streams > 0;
        row.status = if row.suspect {
            "Partial".to_string()
        } else {
            "Complete".to_string()
        };
    }

    AssessSnapshot {
        assessment_index,
        assessment_kind: assessment_kind.to_string(),
        target_ms,
        stream_rows,
        digitizer_rows,
        warnings,
    }
}

fn discover_capture_manifests(root: &Path) -> Result<Vec<PathBuf>> {
    if root.is_file() {
        return Ok(
            if root.file_name().is_some_and(|name| name == "manifest.json") {
                vec![root.to_path_buf()]
            } else {
                Vec::new()
            },
        );
    }
    if root.join("manifest.json").is_file() {
        return Ok(vec![root.join("manifest.json")]);
    }
    let mut manifests = Vec::<PathBuf>::new();
    for entry in fs::read_dir(root)
        .with_context(|| format!("failed to read directory {}", root.display()))?
    {
        let entry = entry?;
        let path = entry.path();
        if path.is_dir() && path.join("manifest.json").is_file() {
            manifests.push(path.join("manifest.json"));
        }
    }
    manifests.sort();
    Ok(manifests)
}

fn load_spill_from_manifest_for_diagnostics(
    manifest_path: &Path,
    same_spill_tolerance_override: Option<u64>,
) -> Result<CapturedSpill> {
    let raw = fs::read_to_string(manifest_path)
        .with_context(|| format!("failed to read manifest {}", manifest_path.display()))?;
    let value: Value = serde_json::from_str(&raw)
        .with_context(|| format!("failed to parse manifest {}", manifest_path.display()))?;
    let obj = value
        .as_object()
        .ok_or_else(|| anyhow!("manifest {} is not a JSON object", manifest_path.display()))?;

    let target_ms = value_u64(obj.get("target_ms")).ok_or_else(|| {
        anyhow!(
            "manifest {} does not include numeric target_ms",
            manifest_path.display()
        )
    })?;
    let align_tolerance_ms = value_u64(obj.get("align_tolerance_ms")).unwrap_or(1);
    let same_spill_tolerance_ms = same_spill_tolerance_override
        .or_else(|| value_u64(obj.get("same_spill_tolerance_ms")))
        .unwrap_or(25);
    let inventory = value_array(obj.get("stream_inventory"))
        .iter()
        .filter_map(|entry| parse_inventory_value(entry))
        .collect::<Vec<_>>();
    let latest_observations = value_array(obj.get("latest_observations"))
        .iter()
        .filter_map(|entry| {
            parse_latest_observation_value(entry, target_ms, same_spill_tolerance_ms)
        })
        .collect::<Vec<_>>();
    let streams = value_array(obj.get("streams"))
        .iter()
        .filter_map(|entry| parse_captured_stream_value(entry, target_ms, same_spill_tolerance_ms))
        .collect::<Vec<_>>();

    let requested_streams = value_usize(obj.get("requested_streams")).unwrap_or(inventory.len());
    Ok(CapturedSpill {
        schema_version: value_u64(obj.get("schema_version")).unwrap_or(1) as u32,
        artifact_type: CAPTURE_ARTIFACT_TYPE,
        redis_timestamp_ms: value_u64(obj.get("redis_timestamp_ms")).unwrap_or(target_ms),
        target_ms,
        align_tolerance_ms,
        same_spill_tolerance_ms,
        min_aligned_fraction: value_f64(obj.get("min_aligned_fraction")).unwrap_or(0.70),
        wake: parse_wake_value(obj.get("wake")),
        requested_streams,
        stream_inventory: inventory,
        latest_observations,
        streams,
        warnings: value_array(obj.get("warnings"))
            .iter()
            .filter_map(|value| value.as_str().map(|s| s.to_string()))
            .collect(),
    })
}

fn parse_inventory_value(value: &Value) -> Option<CaptureStreamInventoryEntry> {
    let obj = value.as_object()?;
    let stream_key = obj.get("stream_key")?.as_str()?.to_string();
    let plane = obj
        .get("plane")
        .and_then(|value| value.as_str())
        .and_then(parse_plane_label)
        .or_else(|| classify_plane(&stream_key))?;
    Some(CaptureStreamInventoryEntry {
        device_label: obj
            .get("device_label")
            .and_then(|value| value.as_str())
            .unwrap_or("")
            .to_string(),
        bpm_ip: obj.get("bpm_ip")?.as_str()?.to_string(),
        stream_key,
        plane,
    })
}

fn parse_latest_observation_value(
    value: &Value,
    target_ms: u64,
    tolerance_ms: u64,
) -> Option<CaptureObservation> {
    let obj = value.as_object()?;
    let ms = value_u64(obj.get("ms"))?;
    Some(CaptureObservation {
        bpm_ip: obj.get("bpm_ip")?.as_str()?.to_string(),
        stream_key: obj.get("stream_key")?.as_str()?.to_string(),
        id: obj.get("id")?.as_str()?.to_string(),
        ms,
        aligned: abs_diff_u64(ms, target_ms) <= tolerance_ms,
    })
}

fn parse_captured_stream_value(
    value: &Value,
    target_ms: u64,
    tolerance_ms: u64,
) -> Option<CapturedStreamEntry> {
    let obj = value.as_object()?;
    let stream_key = obj.get("stream_key")?.as_str()?.to_string();
    let plane = obj
        .get("plane")
        .and_then(|value| value.as_str())
        .and_then(parse_plane_label)
        .or_else(|| classify_plane(&stream_key))?;
    let stream_ms = value_u64(obj.get("stream_ms"))?;
    Some(CapturedStreamEntry {
        device_label: obj
            .get("device_label")
            .and_then(|value| value.as_str())
            .unwrap_or("")
            .to_string(),
        bpm_ip: obj.get("bpm_ip")?.as_str()?.to_string(),
        stream_key,
        plane,
        stream_id: obj.get("stream_id")?.as_str()?.to_string(),
        stream_ms,
        aligned: abs_diff_u64(stream_ms, target_ms) <= tolerance_ms,
        field_count: value_usize(obj.get("field_count")).unwrap_or(0),
        payload: None,
        payload_file: obj
            .get("payload_file")
            .and_then(|value| value.as_str())
            .map(|s| s.to_string()),
        payload_bytes: value_usize(obj.get("payload_bytes")).unwrap_or(0),
        sample_count: value_usize(obj.get("sample_count")),
        checksum_fnv1a64: obj
            .get("checksum_fnv1a64")
            .and_then(|value| value.as_str())
            .map(|s| s.to_string()),
    })
}

fn parse_wake_value(value: Option<&Value>) -> Option<CaptureWake> {
    let obj = value?.as_object()?;
    let ms = value_u64(obj.get("ms"))?;
    Some(CaptureWake {
        bpm_ip: obj
            .get("bpm_ip")
            .and_then(|value| value.as_str())
            .unwrap_or("")
            .to_string(),
        stream_id: obj
            .get("stream_id")
            .and_then(|value| value.as_str())
            .unwrap_or("")
            .to_string(),
        ms,
    })
}

fn parse_plane_label(value: &str) -> Option<Plane> {
    match value {
        "H" => Some(Plane::Horizontal),
        "V" => Some(Plane::Vertical),
        _ => None,
    }
}

fn value_array(value: Option<&Value>) -> Vec<Value> {
    value
        .and_then(|value| value.as_array())
        .cloned()
        .unwrap_or_default()
}

fn value_u64(value: Option<&Value>) -> Option<u64> {
    value.and_then(|value| value.as_u64())
}

fn value_usize(value: Option<&Value>) -> Option<usize> {
    value_u64(value).and_then(|value| usize::try_from(value).ok())
}

fn value_f64(value: Option<&Value>) -> Option<f64> {
    value.and_then(|value| value.as_f64())
}

fn run_capture_spills_free_run(
    config: MonitorConfig,
    out_dir: &Path,
    count: Option<usize>,
) -> Result<()> {
    if config.devices.is_empty() {
        bail!("config has no devices for capture-spills");
    }

    fs::create_dir_all(out_dir)
        .with_context(|| format!("failed to create output directory {}", out_dir.display()))?;

    println!(
        "capture-spills free-run mode: watching {} devices, writing raw captured-spill bundles",
        config.devices.len()
    );
    if let Some(limit) = count {
        println!("capture stop condition: {limit} successful captures");
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
                eprintln!("capture watch worker exited: {err}");
            }
        });
    }
    drop(tx);

    let dedupe_tolerance_ms = target_bucket_tolerance_ms(&config);
    let mut seen_target_ms = HashSet::<u64>::new();
    let mut results = Vec::<CaptureWriteResult>::new();
    let mut successful = 0usize;
    let mut duplicate_wakes = 0usize;
    let mut unresolved_wakes = 0usize;

    loop {
        let signal = match rx.recv() {
            Ok(signal) => signal,
            Err(err) => bail!("capture-spills event channel closed: {err}"),
        };

        let mut spill = match capture_latest_spill_with_retries(&config) {
            Ok(spill) => spill,
            Err(err) => {
                unresolved_wakes += 1;
                eprintln!(
                    "[capture-spills] snapshot after {} {} failed: {}",
                    signal.bpm_ip, signal.event.id, err
                );
                continue;
            }
        };
        spill.wake = Some(CaptureWake {
            bpm_ip: signal.bpm_ip.clone(),
            stream_id: signal.event.id.clone(),
            ms: signal.event.ms,
        });

        if target_seen_within_tolerance(&seen_target_ms, spill.target_ms, dedupe_tolerance_ms) {
            duplicate_wakes += 1;
            continue;
        }
        seen_target_ms.insert(spill.target_ms);

        match write_capture_bundle(out_dir, &spill) {
            Ok(result) => {
                print_capture_summary(
                    &result,
                    &spill,
                    &format!(
                        "[capture-spills] wake {} {} (ms {}) -> target {}",
                        signal.bpm_ip, signal.event.id, signal.event.ms, spill.target_ms
                    ),
                );
                results.push(result);
                successful += 1;
                write_capture_index(out_dir, &results, unresolved_wakes, duplicate_wakes)?;
                write_capture_diagnostics_outputs(out_dir, &results)?;

                if let Some(limit) = count {
                    println!("[capture-spills] successful captures: {successful}/{limit}");
                    if successful >= limit {
                        println!("[capture-spills] reached requested count ({limit}), exiting");
                        return Ok(());
                    }
                }
            }
            Err(err) => {
                eprintln!(
                    "[capture-spills] failed writing bundle for target {}: {}",
                    spill.target_ms, err
                );
            }
        }
    }
}

fn capture_latest_spill_with_retries(config: &MonitorConfig) -> Result<CapturedSpill> {
    let mut last_error: Option<anyhow::Error> = None;
    for attempt in 0..=FREE_RUN_SETTLE_RETRIES {
        match capture_latest_spill(config) {
            Ok(spill) => return Ok(spill),
            Err(err) => {
                last_error = Some(err);
                if attempt == FREE_RUN_SETTLE_RETRIES {
                    break;
                }
                thread::sleep(Duration::from_millis(FREE_RUN_SETTLE_DELAY_MS));
            }
        }
    }
    Err(last_error.unwrap_or_else(|| anyhow!("spill capture failed without explicit error")))
}

fn capture_latest_spill(config: &MonitorConfig) -> Result<CapturedSpill> {
    let mut warnings = Vec::<String>::new();
    let stream_inventory = collect_stream_inventory(config);
    let requested_streams = stream_inventory.len();

    let mut latest_observations = collect_latest_tbt_observations(config, &mut warnings)?;
    if latest_observations.is_empty() {
        bail!("no latest TBT observations were available from any configured device");
    }
    if latest_observations.len() < requested_streams {
        warnings.push(format!(
            "incomplete TBT poll at target selection: observed {} of {} configured streams",
            latest_observations.len(),
            requested_streams
        ));
    }

    let target_ms = choose_target_millisecond(
        &latest_observations
            .iter()
            .filter(|obs| classify_plane(&obs.stream_key).is_some())
            .map(|obs| obs.ms)
            .collect::<Vec<_>>(),
        target_bucket_tolerance_ms(config),
    )
    .ok_or_else(|| anyhow!("failed to choose target TBT millisecond from position streams"))?;

    for obs in &mut latest_observations {
        obs.aligned = abs_diff_u64(obs.ms, target_ms) <= config.same_spill_tolerance_ms;
    }
    push_alignment_warning(
        &latest_observations,
        config.min_aligned_fraction,
        &mut warnings,
    );

    let streams = collect_stream_entries(
        config,
        target_ms,
        config.same_spill_tolerance_ms,
        &mut warnings,
    )?;
    if streams.is_empty() {
        bail!(
            "no TBT stream entries were found within ±{} ms of target {}",
            config.same_spill_tolerance_ms,
            target_ms
        );
    }
    if streams.len() < requested_streams {
        warnings.push(format!(
            "incomplete near-target capture: captured {} of {} configured streams",
            streams.len(),
            requested_streams
        ));
    }

    Ok(CapturedSpill {
        schema_version: CAPTURE_SCHEMA_VERSION,
        artifact_type: CAPTURE_ARTIFACT_TYPE,
        redis_timestamp_ms: target_ms,
        target_ms,
        align_tolerance_ms: config.align_tolerance_ms,
        same_spill_tolerance_ms: config.same_spill_tolerance_ms,
        min_aligned_fraction: config.min_aligned_fraction,
        wake: None,
        requested_streams,
        stream_inventory,
        latest_observations,
        streams,
        warnings,
    })
}

fn collect_latest_tbt_observations(
    config: &MonitorConfig,
    warnings: &mut Vec<String>,
) -> Result<Vec<CaptureObservation>> {
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
        for spec in collect_capture_stream_specs(config, device) {
            let stream_key = &spec.stream_key;

            match fetch_latest_entry(&mut conn, stream_key) {
                Ok(Some((id, _))) => {
                    let Some((ms, _)) = parse_stream_id(&id) else {
                        warnings.push(format!(
                            "{}: TBT id {} for {} could not be parsed",
                            device.bpm_ip, id, stream_key
                        ));
                        continue;
                    };
                    observations.push(CaptureObservation {
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
                "{}: no latest TBT entries found on configured capture streams",
                device.bpm_ip
            ));
        }
    }

    Ok(observations)
}

fn collect_stream_entries(
    config: &MonitorConfig,
    target_ms: u64,
    tolerance_ms: u64,
    warnings: &mut Vec<String>,
) -> Result<Vec<CapturedStreamEntry>> {
    let mut streams = Vec::new();

    for device in &config.devices {
        let mut conn = match connect_device(&device.redis) {
            Ok(conn) => conn,
            Err(err) => {
                warnings.push(format!(
                    "{}: failed to connect for stream capture: {err}",
                    device.bpm_ip
                ));
                continue;
            }
        };

        for spec in collect_capture_stream_specs(config, device) {
            let stream_key = &spec.stream_key;

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

            let payload = payload_field(&fields).map(|bytes| bytes.to_vec());
            let payload_bytes = payload.as_ref().map(|bytes| bytes.len()).unwrap_or(0);
            let sample_count = payload.as_ref().and_then(|bytes| {
                if bytes.len() % 4 == 0 {
                    Some(bytes.len() / 4)
                } else {
                    None
                }
            });

            if payload.is_none() {
                warnings.push(format!(
                    "{}: captured {} ({id}) without '_' payload field",
                    device.bpm_ip, stream_key
                ));
            } else if sample_count.is_none() {
                warnings.push(format!(
                    "{}: captured {} ({id}) payload length {} is not divisible by 4",
                    device.bpm_ip, stream_key, payload_bytes
                ));
            }

            let checksum_fnv1a64 = payload.as_ref().map(|bytes| fnv1a64_hex(bytes));
            streams.push(CapturedStreamEntry {
                device_label: device.label.clone(),
                bpm_ip: device.bpm_ip.clone(),
                stream_key: stream_key.clone(),
                plane: spec.plane,
                stream_id: id,
                stream_ms: ms,
                aligned: abs_diff_u64(ms, target_ms) <= tolerance_ms,
                field_count: fields.len(),
                payload,
                payload_file: None,
                payload_bytes,
                sample_count,
                checksum_fnv1a64,
            });
        }
    }

    Ok(streams)
}

fn write_capture_bundle(out_dir: &Path, spill: &CapturedSpill) -> Result<CaptureWriteResult> {
    let bundle_name = format!("spill_{}", spill.target_ms);
    let bundle_dir = out_dir.join(&bundle_name);
    let payload_dir = bundle_dir.join("payloads");

    fs::create_dir_all(&payload_dir).with_context(|| {
        format!(
            "failed to create payload directory {}",
            payload_dir.display()
        )
    })?;

    let mut manifest_streams = spill.streams.clone();
    for (idx, stream) in manifest_streams.iter_mut().enumerate() {
        let Some(payload) = stream.payload.as_ref() else {
            continue;
        };
        let file_name = payload_file_name(idx, stream);
        let path = payload_dir.join(&file_name);
        fs::write(&path, payload)
            .with_context(|| format!("failed to write payload {}", path.display()))?;
        stream.payload_file = Some(format!("payloads/{file_name}"));
    }

    let diagnostics = build_capture_diagnostics(spill, &manifest_streams);
    let manifest_path = bundle_dir.join("manifest.json");
    fs::write(
        &manifest_path,
        manifest_json(spill, &manifest_streams, &diagnostics),
    )
    .with_context(|| format!("failed to write manifest {}", manifest_path.display()))?;

    let summary_path = bundle_dir.join("capture_summary.txt");
    fs::write(
        &summary_path,
        capture_summary_lines(spill, &manifest_streams, &diagnostics).join("\n") + "\n",
    )
    .with_context(|| format!("failed to write summary {}", summary_path.display()))?;

    Ok(CaptureWriteResult {
        target_ms: spill.target_ms,
        redis_timestamp_ms: spill.redis_timestamp_ms,
        bundle_dir,
        manifest_path,
        summary_path,
        requested_streams: spill.requested_streams,
        latest_observations: spill.latest_observations.len(),
        latest_same_spill_streams: spill
            .latest_observations
            .iter()
            .filter(|obs| obs.aligned)
            .count(),
        captured_streams: spill.streams.len(),
        warning_count: spill.warnings.len(),
        diagnostics,
    })
}

fn write_capture_index(
    out_dir: &Path,
    results: &[CaptureWriteResult],
    unresolved_wakes: usize,
    duplicate_wakes: usize,
) -> Result<()> {
    let mut rows = Vec::<String>::new();
    rows.push(
        "capture_index,target_ms,redis_timestamp_ms,bundle_dir,manifest_path,requested_streams,latest_observations,latest_same_spill_streams,captured_streams,warning_count,unresolved_wakes,duplicate_wakes,status,complete_streams,same_spill_streams,missing_streams,stale_capture_streams,ahead_capture_streams,payload_issue_streams,suspect_digitizers,latest_poll_suspect_digitizers,captured_delta_min_ms,captured_delta_max_ms,captured_delta_median_abs_ms,latest_delta_min_ms,latest_delta_max_ms,latest_delta_median_abs_ms"
            .to_string(),
    );

    for (idx, result) in results.iter().enumerate() {
        rows.push(format!(
            "{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{}",
            idx,
            result.target_ms,
            result.redis_timestamp_ms,
            csv_escape(&result.bundle_dir.display().to_string()),
            csv_escape(&result.manifest_path.display().to_string()),
            result.requested_streams,
            result.latest_observations,
            result.latest_same_spill_streams,
            result.captured_streams,
            result.warning_count,
            unresolved_wakes,
            duplicate_wakes,
            csv_escape(&result.diagnostics.status),
            result.diagnostics.complete_streams,
            result.diagnostics.same_spill_streams,
            result.diagnostics.missing_streams,
            result.diagnostics.stale_capture_streams,
            result.diagnostics.ahead_capture_streams,
            result.diagnostics.payload_issue_streams,
            result.diagnostics.suspect_digitizers,
            result.diagnostics.latest_poll_suspect_digitizers,
            csv_opt_i64(result.diagnostics.captured_timing.min_delta_ms),
            csv_opt_i64(result.diagnostics.captured_timing.max_delta_ms),
            csv_opt_f64(result.diagnostics.captured_timing.median_abs_delta_ms),
            csv_opt_i64(result.diagnostics.latest_timing.min_delta_ms),
            csv_opt_i64(result.diagnostics.latest_timing.max_delta_ms),
            csv_opt_f64(result.diagnostics.latest_timing.median_abs_delta_ms)
        ));
    }

    let path = out_dir.join("capture_index.csv");
    fs::write(&path, rows.join("\n") + "\n")
        .with_context(|| format!("failed to write capture index {}", path.display()))
}

fn write_capture_diagnostics_outputs(out_dir: &Path, results: &[CaptureWriteResult]) -> Result<()> {
    write_capture_spill_diagnostics_csv(out_dir, results)?;
    write_capture_stream_diagnostics_csv(out_dir, results)?;
    write_capture_timestamp_distribution_csv(out_dir, results)?;
    write_capture_digitizer_diagnostics_csv(out_dir, results)?;
    write_capture_quality_summary_json(out_dir, results)?;
    write_capture_quality_report_md(out_dir, results)?;
    Ok(())
}

fn write_capture_spill_diagnostics_csv(
    out_dir: &Path,
    results: &[CaptureWriteResult],
) -> Result<()> {
    let mut rows = vec![
        "capture_index,target_ms,status,requested_streams,captured_streams,complete_streams,same_spill_streams,missing_streams,stale_capture_streams,ahead_capture_streams,payload_issue_streams,latest_stale_streams,latest_missing_streams,latest_ahead_streams,suspect_digitizers,latest_poll_suspect_digitizers,wake_delta_ms,captured_delta_min_ms,captured_delta_max_ms,captured_delta_median_abs_ms,captured_delta_max_abs_ms,latest_delta_min_ms,latest_delta_max_ms,latest_delta_median_abs_ms,latest_delta_max_abs_ms"
            .to_string(),
    ];
    for (idx, result) in results.iter().enumerate() {
        let d = &result.diagnostics;
        rows.push(format!(
            "{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{}",
            idx,
            result.target_ms,
            csv_escape(&d.status),
            d.requested_streams,
            d.captured_streams,
            d.complete_streams,
            d.same_spill_streams,
            d.missing_streams,
            d.stale_capture_streams,
            d.ahead_capture_streams,
            d.payload_issue_streams,
            d.latest_stale_streams,
            d.latest_missing_streams,
            d.latest_ahead_streams,
            d.suspect_digitizers,
            d.latest_poll_suspect_digitizers,
            csv_opt_i64(d.wake_delta_ms),
            csv_opt_i64(d.captured_timing.min_delta_ms),
            csv_opt_i64(d.captured_timing.max_delta_ms),
            csv_opt_f64(d.captured_timing.median_abs_delta_ms),
            csv_opt_u64(d.captured_timing.max_abs_delta_ms),
            csv_opt_i64(d.latest_timing.min_delta_ms),
            csv_opt_i64(d.latest_timing.max_delta_ms),
            csv_opt_f64(d.latest_timing.median_abs_delta_ms),
            csv_opt_u64(d.latest_timing.max_abs_delta_ms)
        ));
    }
    fs::write(
        out_dir.join("capture_spill_diagnostics.csv"),
        rows.join("\n") + "\n",
    )
    .with_context(|| "failed to write capture_spill_diagnostics.csv")
}

fn write_capture_stream_diagnostics_csv(
    out_dir: &Path,
    results: &[CaptureWriteResult],
) -> Result<()> {
    let mut rows = vec![
        "capture_index,target_ms,device_label,bpm_ip,plane,stream_key,captured_status,latest_poll_status,primary_reason,captured_stream_id,captured_ms,captured_delta_ms,latest_id,latest_ms,latest_delta_ms,payload_bytes,sample_count"
            .to_string(),
    ];
    for (idx, result) in results.iter().enumerate() {
        for row in &result.diagnostics.streams {
            rows.push(format!(
                "{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{}",
                idx,
                result.target_ms,
                csv_escape(&row.device_label),
                csv_escape(&row.bpm_ip),
                csv_escape(&row.plane),
                csv_escape(&row.stream_key),
                csv_escape(&row.captured_status),
                csv_escape(&row.latest_poll_status),
                csv_escape(&row.primary_reason),
                csv_escape(row.captured_stream_id.as_deref().unwrap_or("")),
                csv_opt_u64(row.captured_ms),
                csv_opt_i64(row.captured_delta_ms),
                csv_escape(row.latest_id.as_deref().unwrap_or("")),
                csv_opt_u64(row.latest_ms),
                csv_opt_i64(row.latest_delta_ms),
                csv_opt_usize(row.payload_bytes),
                csv_opt_usize(row.sample_count)
            ));
        }
    }
    fs::write(
        out_dir.join("capture_stream_diagnostics.csv"),
        rows.join("\n") + "\n",
    )
    .with_context(|| "failed to write capture_stream_diagnostics.csv")
}

fn write_capture_timestamp_distribution_csv(
    out_dir: &Path,
    results: &[CaptureWriteResult],
) -> Result<()> {
    let mut rows = vec![
        "capture_index,target_ms,source,delta_ms,stream_count,total_observed_streams,fraction_observed"
            .to_string(),
    ];
    for (idx, result) in results.iter().enumerate() {
        append_timestamp_distribution_rows(
            &mut rows,
            idx,
            result.target_ms,
            "captured_payload",
            &result.diagnostics.captured_timing,
        );
        append_timestamp_distribution_rows(
            &mut rows,
            idx,
            result.target_ms,
            "latest_id_snapshot",
            &result.diagnostics.latest_timing,
        );
    }
    fs::write(
        out_dir.join("capture_timestamp_distribution.csv"),
        rows.join("\n") + "\n",
    )
    .with_context(|| "failed to write capture_timestamp_distribution.csv")
}

fn append_timestamp_distribution_rows(
    rows: &mut Vec<String>,
    capture_index: usize,
    target_ms: u64,
    source: &str,
    summary: &TimingSummary,
) {
    for bucket in &summary.delta_counts {
        let fraction = if summary.count == 0 {
            0.0
        } else {
            bucket.count as f64 / summary.count as f64
        };
        rows.push(format!(
            "{},{},{},{},{},{},{:.6}",
            capture_index,
            target_ms,
            csv_escape(source),
            bucket.delta_ms,
            bucket.count,
            summary.count,
            fraction
        ));
    }
}

fn write_capture_digitizer_diagnostics_csv(
    out_dir: &Path,
    results: &[CaptureWriteResult],
) -> Result<()> {
    let mut rows = vec![
        "capture_index,target_ms,device_label,bpm_ip,status,configured_streams,complete_streams,same_spill_streams,missing_capture_streams,stale_capture_streams,ahead_capture_streams,payload_issue_streams,latest_stale_streams,latest_missing_streams,latest_ahead_streams,suspect,latest_poll_suspect"
            .to_string(),
    ];
    for (idx, result) in results.iter().enumerate() {
        for row in &result.diagnostics.digitizers {
            rows.push(format!(
                "{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{}",
                idx,
                result.target_ms,
                csv_escape(&row.device_label),
                csv_escape(&row.bpm_ip),
                csv_escape(&row.status),
                row.configured_streams,
                row.complete_streams,
                row.same_spill_streams,
                row.missing_capture_streams,
                row.stale_capture_streams,
                row.ahead_capture_streams,
                row.payload_issue_streams,
                row.latest_stale_streams,
                row.latest_missing_streams,
                row.latest_ahead_streams,
                row.suspect,
                row.latest_poll_suspect
            ));
        }
    }
    fs::write(
        out_dir.join("capture_digitizer_diagnostics.csv"),
        rows.join("\n") + "\n",
    )
    .with_context(|| "failed to write capture_digitizer_diagnostics.csv")
}

fn write_capture_quality_summary_json(
    out_dir: &Path,
    results: &[CaptureWriteResult],
) -> Result<()> {
    let mut capture_suspects = HashMap::<String, usize>::new();
    let mut latest_suspects = HashMap::<String, usize>::new();
    for result in results {
        for row in &result.diagnostics.digitizers {
            if row.suspect {
                *capture_suspects.entry(row.bpm_ip.clone()).or_insert(0) += 1;
            }
            if row.latest_poll_suspect {
                *latest_suspects.entry(row.bpm_ip.clone()).or_insert(0) += 1;
            }
        }
    }

    let total = results.len();
    let complete = results
        .iter()
        .filter(|result| result.diagnostics.status == "Complete")
        .count();
    let mut out = String::new();
    out.push_str("{\n");
    out.push_str(&format!("  \"spill_count\": {},\n", total));
    out.push_str(&format!("  \"complete_spills\": {},\n", complete));
    out.push_str(&format!("  \"partial_spills\": {},\n", total - complete));
    out.push_str(&format!(
        "  \"same_spill_tolerance_ms\": {},\n",
        results
            .first()
            .map(|result| result.diagnostics.same_spill_tolerance_ms)
            .unwrap_or(0)
    ));
    out.push_str("  \"capture_suspect_digitizers\": [\n");
    let capture_items = sorted_count_items(&capture_suspects);
    for (idx, (bpm_ip, count)) in capture_items.iter().enumerate() {
        out.push_str(&format!(
            "    {{\"bpm_ip\": {}, \"spill_count\": {}}}{}\n",
            json_string(bpm_ip),
            count,
            comma(idx, capture_items.len())
        ));
    }
    out.push_str("  ],\n");
    out.push_str("  \"latest_poll_suspect_digitizers\": [\n");
    let latest_items = sorted_count_items(&latest_suspects);
    for (idx, (bpm_ip, count)) in latest_items.iter().enumerate() {
        out.push_str(&format!(
            "    {{\"bpm_ip\": {}, \"spill_count\": {}}}{}\n",
            json_string(bpm_ip),
            count,
            comma(idx, latest_items.len())
        ));
    }
    out.push_str("  ],\n");
    out.push_str("  \"captured_payload_delta_distribution\": [\n");
    let captured_distribution =
        aggregate_timing_distribution(results, |result| &result.diagnostics.captured_timing);
    write_delta_distribution_json_items(&mut out, &captured_distribution, "    ");
    out.push_str("  ],\n");
    out.push_str("  \"latest_id_snapshot_delta_distribution\": [\n");
    let latest_distribution =
        aggregate_timing_distribution(results, |result| &result.diagnostics.latest_timing);
    write_delta_distribution_json_items(&mut out, &latest_distribution, "    ");
    out.push_str("  ],\n");
    out.push_str("  \"strict_fail_preview\": {\n");
    out.push_str(&format!(
        "    \"would_fail_on_capture_quality\": {},\n",
        total != complete
    ));
    out.push_str(&format!(
        "    \"would_fail_on_latest_poll_quality\": {}\n",
        !latest_items.is_empty()
    ));
    out.push_str("  }\n");
    out.push_str("}\n");

    fs::write(out_dir.join("capture_quality_summary.json"), out)
        .with_context(|| "failed to write capture_quality_summary.json")
}

fn aggregate_timing_distribution<F>(
    results: &[CaptureWriteResult],
    timing_selector: F,
) -> Vec<TimingDeltaCount>
where
    F: Fn(&CaptureWriteResult) -> &TimingSummary,
{
    let mut counts = BTreeMap::<i64, usize>::new();
    for result in results {
        for bucket in &timing_selector(result).delta_counts {
            *counts.entry(bucket.delta_ms).or_insert(0) += bucket.count;
        }
    }
    counts
        .into_iter()
        .map(|(delta_ms, count)| TimingDeltaCount { delta_ms, count })
        .collect()
}

fn write_delta_distribution_json_items(
    out: &mut String,
    distribution: &[TimingDeltaCount],
    indent: &str,
) {
    for (idx, bucket) in distribution.iter().enumerate() {
        out.push_str(&format!(
            "{}{{\"delta_ms\": {}, \"stream_count\": {}}}{}\n",
            indent,
            bucket.delta_ms,
            bucket.count,
            comma(idx, distribution.len())
        ));
    }
}

fn format_delta_distribution(summary: &TimingSummary) -> String {
    if summary.delta_counts.is_empty() {
        return "none".to_string();
    }
    summary
        .delta_counts
        .iter()
        .map(|bucket| format!("{} ms: {}", bucket.delta_ms, bucket.count))
        .collect::<Vec<_>>()
        .join(", ")
}

fn format_aggregate_delta_distribution(distribution: &[TimingDeltaCount]) -> String {
    if distribution.is_empty() {
        return "none".to_string();
    }
    distribution
        .iter()
        .map(|bucket| format!("{} ms: {}", bucket.delta_ms, bucket.count))
        .collect::<Vec<_>>()
        .join(", ")
}

fn timing_range_text(summary: &TimingSummary) -> String {
    format!(
        "min={} ms max={} ms median_abs={} ms max_abs={} ms",
        opt_i64(summary.min_delta_ms),
        opt_i64(summary.max_delta_ms),
        summary
            .median_abs_delta_ms
            .map(|value| format!("{value:.2}"))
            .unwrap_or_else(|| "NA".to_string()),
        opt_u64(summary.max_abs_delta_ms)
    )
}

fn timing_bucket_counts_text(summary: &TimingSummary) -> String {
    format!(
        "same_spill={} stale={} ahead={} observed={}",
        summary.same_spill_count, summary.stale_count, summary.ahead_count, summary.count
    )
}

fn write_capture_quality_report_md(out_dir: &Path, results: &[CaptureWriteResult]) -> Result<()> {
    let total = results.len();
    let complete = results
        .iter()
        .filter(|result| result.diagnostics.status == "Complete")
        .count();
    let mut capture_suspects = HashMap::<String, usize>::new();
    let mut latest_suspects = HashMap::<String, usize>::new();
    for result in results {
        for row in &result.diagnostics.digitizers {
            if row.suspect {
                *capture_suspects.entry(row.bpm_ip.clone()).or_insert(0) += 1;
            }
            if row.latest_poll_suspect {
                *latest_suspects.entry(row.bpm_ip.clone()).or_insert(0) += 1;
            }
        }
    }

    let mut lines = Vec::<String>::new();
    lines.push("# Capture Quality Report".to_string());
    lines.push(String::new());
    lines.push(format!("- spills assessed: `{total}`"));
    lines.push(format!("- complete captured spills: `{complete}`"));
    lines.push(format!("- partial captured spills: `{}`", total - complete));
    if let Some(first) = results.first() {
        lines.push(format!(
            "- same-spill tolerance: `±{} ms`",
            first.diagnostics.same_spill_tolerance_ms
        ));
    }
    let captured_distribution =
        aggregate_timing_distribution(results, |result| &result.diagnostics.captured_timing);
    let latest_distribution =
        aggregate_timing_distribution(results, |result| &result.diagnostics.latest_timing);
    let captured_observed = captured_distribution
        .iter()
        .map(|bucket| bucket.count)
        .sum::<usize>();
    let latest_observed = latest_distribution
        .iter()
        .map(|bucket| bucket.count)
        .sum::<usize>();
    lines.push(String::new());
    lines.push("## Timestamp Delta Distribution".to_string());
    lines.push(String::new());
    lines.push("Delta is `stream_timestamp_ms - target_ms`.".to_string());
    lines.push(String::new());
    lines.push(format!(
        "- captured payload timestamps: `{captured_observed}` observed stream timestamps"
    ));
    lines.push(format!(
        "- captured payload delta_ms: `{}`",
        format_aggregate_delta_distribution(&captured_distribution)
    ));
    lines.push(format!(
        "- latest-ID snapshot timestamps: `{latest_observed}` observed stream timestamps"
    ));
    lines.push(format!(
        "- latest-ID snapshot delta_ms: `{}`",
        format_aggregate_delta_distribution(&latest_distribution)
    ));
    lines.push(String::new());
    lines.push("## Capture Suspect Digitizers".to_string());
    if capture_suspects.is_empty() {
        lines.push(String::new());
        lines.push("None.".to_string());
    } else {
        lines.push(String::new());
        for (bpm_ip, count) in sorted_count_items(&capture_suspects) {
            lines.push(format!("- `{bpm_ip}` in `{count}` spills"));
        }
    }
    lines.push(String::new());
    lines.push("## Latest-Poll Suspect Digitizers".to_string());
    if latest_suspects.is_empty() {
        lines.push(String::new());
        lines.push("None.".to_string());
    } else {
        lines.push(String::new());
        for (bpm_ip, count) in sorted_count_items(&latest_suspects) {
            lines.push(format!("- `{bpm_ip}` in `{count}` spills"));
        }
    }
    lines.push(String::new());
    lines.push("Latest-poll suspects are timing diagnostics. They do not make a captured artifact partial when the captured payload is complete and same-spill.".to_string());

    fs::write(
        out_dir.join("capture_quality_report.md"),
        lines.join("\n") + "\n",
    )
    .with_context(|| "failed to write capture_quality_report.md")
}

fn write_assess_outputs(out_dir: &Path, snapshots: &[AssessSnapshot]) -> Result<()> {
    write_assess_streams_csv(out_dir, snapshots)?;
    write_assess_digitizers_csv(out_dir, snapshots)?;
    write_assess_summary_json(out_dir, snapshots)?;
    write_assess_report_md(out_dir, snapshots)?;
    Ok(())
}

fn write_assess_streams_csv(out_dir: &Path, snapshots: &[AssessSnapshot]) -> Result<()> {
    let mut rows = vec![
        "assessment_index,assessment_kind,target_ms,device_label,bpm_ip,plane,stream_key,latest_status,latest_id,latest_ms,latest_delta_ms"
            .to_string(),
    ];
    for snapshot in snapshots {
        for row in &snapshot.stream_rows {
            rows.push(format!(
                "{},{},{},{},{},{},{},{},{},{},{}",
                row.assessment_index,
                csv_escape(&row.assessment_kind),
                csv_opt_u64(row.target_ms),
                csv_escape(&row.device_label),
                csv_escape(&row.bpm_ip),
                csv_escape(&row.plane),
                csv_escape(&row.stream_key),
                csv_escape(&row.latest_status),
                csv_escape(row.latest_id.as_deref().unwrap_or("")),
                csv_opt_u64(row.latest_ms),
                csv_opt_i64(row.latest_delta_ms)
            ));
        }
    }
    fs::write(out_dir.join("assess_streams.csv"), rows.join("\n") + "\n")
        .with_context(|| "failed to write assess_streams.csv")
}

fn write_assess_digitizers_csv(out_dir: &Path, snapshots: &[AssessSnapshot]) -> Result<()> {
    let mut rows = vec![
        "assessment_index,assessment_kind,target_ms,device_label,bpm_ip,status,configured_streams,same_spill_streams,latest_stale_streams,latest_missing_streams,latest_ahead_streams,suspect"
            .to_string(),
    ];
    for snapshot in snapshots {
        for row in &snapshot.digitizer_rows {
            rows.push(format!(
                "{},{},{},{},{},{},{},{},{},{},{},{}",
                row.assessment_index,
                csv_escape(&row.assessment_kind),
                csv_opt_u64(row.target_ms),
                csv_escape(&row.device_label),
                csv_escape(&row.bpm_ip),
                csv_escape(&row.status),
                row.configured_streams,
                row.same_spill_streams,
                row.latest_stale_streams,
                row.latest_missing_streams,
                row.latest_ahead_streams,
                row.suspect
            ));
        }
    }
    fs::write(
        out_dir.join("assess_digitizers.csv"),
        rows.join("\n") + "\n",
    )
    .with_context(|| "failed to write assess_digitizers.csv")
}

fn write_assess_summary_json(out_dir: &Path, snapshots: &[AssessSnapshot]) -> Result<()> {
    let mut suspect_counts = HashMap::<String, usize>::new();
    for snapshot in snapshots {
        for row in &snapshot.digitizer_rows {
            if row.suspect {
                *suspect_counts.entry(row.bpm_ip.clone()).or_insert(0) += 1;
            }
        }
    }
    let items = sorted_count_items(&suspect_counts);
    let mut out = String::new();
    out.push_str("{\n");
    out.push_str(&format!("  \"assessment_count\": {},\n", snapshots.len()));
    out.push_str("  \"suspect_digitizers\": [\n");
    for (idx, (bpm_ip, count)) in items.iter().enumerate() {
        out.push_str(&format!(
            "    {{\"bpm_ip\": {}, \"assessment_count\": {}}}{}\n",
            json_string(bpm_ip),
            count,
            comma(idx, items.len())
        ));
    }
    out.push_str("  ],\n");
    out.push_str("  \"snapshots\": [\n");
    for (idx, snapshot) in snapshots.iter().enumerate() {
        let suspect_digitizers = snapshot
            .digitizer_rows
            .iter()
            .filter(|row| row.suspect)
            .count();
        let latest_stale_streams = snapshot
            .stream_rows
            .iter()
            .filter(|row| {
                matches!(
                    row.latest_status.as_str(),
                    "LATEST_STALE" | "LATEST_STALE_BUT_CAPTURED_OK"
                )
            })
            .count();
        out.push_str(&format!(
            "    {{\"assessment_index\": {}, \"assessment_kind\": {}, \"target_ms\": {}, \"suspect_digitizers\": {}, \"latest_stale_streams\": {}, \"warning_count\": {}}}{}\n",
            snapshot.assessment_index,
            json_string(&snapshot.assessment_kind),
            json_opt_u64(snapshot.target_ms),
            suspect_digitizers,
            latest_stale_streams,
            snapshot.warnings.len(),
            comma(idx, snapshots.len())
        ));
    }
    out.push_str("  ]\n");
    out.push_str("}\n");
    fs::write(out_dir.join("assess_summary.json"), out)
        .with_context(|| "failed to write assess_summary.json")
}

fn write_assess_report_md(out_dir: &Path, snapshots: &[AssessSnapshot]) -> Result<()> {
    let mut suspect_counts = HashMap::<String, usize>::new();
    for snapshot in snapshots {
        for row in &snapshot.digitizer_rows {
            if row.suspect {
                *suspect_counts.entry(row.bpm_ip.clone()).or_insert(0) += 1;
            }
        }
    }
    let mut lines = Vec::<String>::new();
    lines.push("# Assess Report".to_string());
    lines.push(String::new());
    lines.push(format!("- snapshots: `{}`", snapshots.len()));
    lines.push(String::new());
    lines.push("## Suspect Digitizers".to_string());
    if suspect_counts.is_empty() {
        lines.push(String::new());
        lines.push("None.".to_string());
    } else {
        lines.push(String::new());
        for (bpm_ip, count) in sorted_count_items(&suspect_counts) {
            lines.push(format!("- `{bpm_ip}` in `{count}` assessments"));
        }
    }
    lines.push(String::new());
    lines.push("## Snapshot Summary".to_string());
    lines.push(String::new());
    for snapshot in snapshots {
        let suspect_digitizers = snapshot
            .digitizer_rows
            .iter()
            .filter(|row| row.suspect)
            .count();
        let stale_streams = snapshot
            .stream_rows
            .iter()
            .filter(|row| {
                matches!(
                    row.latest_status.as_str(),
                    "LATEST_STALE" | "LATEST_STALE_BUT_CAPTURED_OK"
                )
            })
            .count();
        lines.push(format!(
            "- `{}` target={} suspect_digitizers={} stale_streams={} warnings={}",
            snapshot.assessment_kind,
            snapshot
                .target_ms
                .map(|value| value.to_string())
                .unwrap_or_else(|| "NA".to_string()),
            suspect_digitizers,
            stale_streams,
            snapshot.warnings.len()
        ));
    }
    fs::write(out_dir.join("assess_report.md"), lines.join("\n") + "\n")
        .with_context(|| "failed to write assess_report.md")
}

fn sorted_count_items(map: &HashMap<String, usize>) -> Vec<(String, usize)> {
    let mut items = map
        .iter()
        .map(|(key, value)| (key.clone(), *value))
        .collect::<Vec<_>>();
    items.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    items
}

fn run_free_run_watch_worker(
    device: DeviceConfig,
    reconnect_initial_ms: u64,
    reconnect_max_ms: u64,
    tx: Sender<FreeRunSignal>,
) -> Result<()> {
    let keys = collect_tbt_stream_keys(&device);
    if keys.is_empty() {
        bail!(
            "{} has no configured position TBT stream keys",
            device.bpm_ip
        );
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
                    "[capture-spills {}] connect failed: {} (reconnect in {} ms)",
                    device.bpm_ip, err, reconnect_delay_ms
                );
                thread::sleep(Duration::from_millis(reconnect_delay_ms));
                reconnect_delay_ms = reconnect_delay_ms.saturating_mul(2).min(reconnect_cap_ms);
                continue;
            }
        };

        let mut read_ids = initialize_read_ids(&mut conn, &keys, &device.bpm_ip);
        loop {
            match wait_for_next_device_event(&mut conn, &keys, &mut read_ids) {
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
                Ok(None) => {}
                Err(err) => {
                    eprintln!(
                        "[capture-spills {}] read failed: {} (reconnect in {} ms)",
                        device.bpm_ip, err, reconnect_delay_ms
                    );
                    thread::sleep(Duration::from_millis(reconnect_delay_ms));
                    reconnect_delay_ms = reconnect_delay_ms.saturating_mul(2).min(reconnect_cap_ms);
                    break;
                }
            }
        }
    }
}

fn collect_stream_inventory(config: &MonitorConfig) -> Vec<CaptureStreamInventoryEntry> {
    let mut entries = Vec::new();
    for device in &config.devices {
        for spec in collect_capture_stream_specs(config, device) {
            entries.push(CaptureStreamInventoryEntry {
                device_label: device.label.clone(),
                bpm_ip: device.bpm_ip.clone(),
                stream_key: spec.stream_key,
                plane: spec.plane,
            });
        }
    }
    entries
}

fn collect_capture_stream_specs(
    config: &MonitorConfig,
    device: &DeviceConfig,
) -> Vec<CaptureStreamSpec> {
    let mut specs = Vec::new();
    let mut seen = HashSet::<String>::new();
    for key in &device.stream_keys {
        let Some(plane) = classify_plane(key) else {
            continue;
        };
        if seen.insert(key.clone()) {
            specs.push(CaptureStreamSpec {
                stream_key: key.clone(),
                plane,
            });
        }
        if let Some(aux_key) =
            derive_intensity_stream_key(key, config.capture_intensity_variant.as_deref())
        {
            if seen.insert(aux_key.clone()) {
                specs.push(CaptureStreamSpec {
                    stream_key: aux_key,
                    plane,
                });
            }
        }
    }
    specs
}

fn collect_tbt_stream_keys(device: &DeviceConfig) -> Vec<String> {
    let mut keys = Vec::new();
    let mut seen = HashSet::<String>::new();
    for key in &device.stream_keys {
        if classify_plane(key).is_none() {
            continue;
        }
        if seen.insert(key.clone()) {
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
                    "[capture-spills {}] failed reading baseline id for {}: {}",
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

type XRangeReply = Vec<(String, Vec<(Vec<u8>, Vec<u8>)>)>;

fn connect_device(redis_cfg: &RedisConfig) -> Result<Connection> {
    let client = redis::Client::open(redis_cfg.to_url())
        .with_context(|| format!("failed to open redis client {}", redis_cfg.to_url()))?;
    client
        .get_connection()
        .with_context(|| format!("failed to connect to {}", redis_cfg.display_addr()))
}

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

fn payload_field(fields: &[(Vec<u8>, Vec<u8>)]) -> Option<&[u8]> {
    fields
        .iter()
        .find(|(k, _)| k.as_slice() == b"_")
        .map(|(_, v)| v.as_slice())
}

fn classify_plane(key: &str) -> Option<Plane> {
    if key.contains(":HP") && is_position_stream_key(key) {
        Some(Plane::Horizontal)
    } else if key.contains(":VP") && is_position_stream_key(key) {
        Some(Plane::Vertical)
    } else {
        None
    }
}

fn is_position_stream_key(key: &str) -> bool {
    key.ends_with(":TBT_POSITION_SCALED") || key.ends_with(":TBT_POSITION_RAW")
}

fn derive_intensity_stream_key(position_key: &str, variant: Option<&str>) -> Option<String> {
    let variant = variant?;
    let intensity_suffix = match variant {
        "raw" => "TBT_INTENSITY_RAW",
        "scaled" => "TBT_INTENSITY_SCALED",
        "scaled_9a" => "TBT_INTENSITY_SCALED_9A",
        "downsampled" => "TBT_INTENSITY_DOWNSAMPLED",
        _ => return None,
    };
    position_key
        .strip_suffix(":TBT_POSITION_SCALED")
        .or_else(|| position_key.strip_suffix(":TBT_POSITION_RAW"))
        .map(|prefix| format!("{prefix}:{intensity_suffix}"))
}

fn choose_target_millisecond(values: &[u64], merge_tolerance_ms: u64) -> Option<u64> {
    if values.is_empty() {
        return None;
    }

    let mut counts = HashMap::<u64, usize>::new();
    for value in values {
        *counts.entry(*value).or_insert(0) += 1;
    }

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

fn push_alignment_warning(
    observations: &[CaptureObservation],
    min_aligned_fraction: f64,
    warnings: &mut Vec<String>,
) {
    if observations.is_empty() {
        return;
    }
    let aligned = observations.iter().filter(|obs| obs.aligned).count();
    let aligned_fraction = aligned as f64 / observations.len() as f64;
    if aligned_fraction < min_aligned_fraction {
        warnings.push(format!(
            "latest-ID same-spill fraction {:.1}% is below configured minimum {:.1}%",
            aligned_fraction * 100.0,
            min_aligned_fraction * 100.0
        ));
    }
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

fn target_bucket_tolerance_ms(config: &MonitorConfig) -> u64 {
    config.same_spill_tolerance_ms
}

fn target_seen_within_tolerance(seen: &HashSet<u64>, target_ms: u64, tolerance_ms: u64) -> bool {
    seen.iter()
        .any(|seen_ms| abs_diff_u64(*seen_ms, target_ms) <= tolerance_ms)
}

fn abs_diff_u64(a: u64, b: u64) -> u64 {
    a.max(b) - a.min(b)
}

fn signed_delta_ms(ms: u64, target_ms: u64) -> i64 {
    if ms >= target_ms {
        ms.saturating_sub(target_ms).min(i64::MAX as u64) as i64
    } else {
        -(target_ms.saturating_sub(ms).min(i64::MAX as u64) as i64)
    }
}

fn timing_summary(deltas: &[i64], tolerance_ms: u64) -> TimingSummary {
    if deltas.is_empty() {
        return TimingSummary {
            count: 0,
            min_delta_ms: None,
            max_delta_ms: None,
            median_abs_delta_ms: None,
            max_abs_delta_ms: None,
            same_spill_count: 0,
            stale_count: 0,
            ahead_count: 0,
            delta_counts: Vec::new(),
        };
    }
    let abs_deltas = deltas
        .iter()
        .map(|delta| delta.unsigned_abs() as f64)
        .collect::<Vec<_>>();
    let mut counts = BTreeMap::<i64, usize>::new();
    let mut same_spill_count = 0usize;
    let mut stale_count = 0usize;
    let mut ahead_count = 0usize;
    for delta in deltas {
        *counts.entry(*delta).or_insert(0) += 1;
        if delta.unsigned_abs() <= tolerance_ms {
            same_spill_count += 1;
        } else if *delta < 0 {
            stale_count += 1;
        } else {
            ahead_count += 1;
        }
    }
    TimingSummary {
        count: deltas.len(),
        min_delta_ms: deltas.iter().copied().min(),
        max_delta_ms: deltas.iter().copied().max(),
        median_abs_delta_ms: Some(median(&abs_deltas)),
        max_abs_delta_ms: deltas.iter().map(|delta| delta.unsigned_abs()).max(),
        same_spill_count,
        stale_count,
        ahead_count,
        delta_counts: counts
            .into_iter()
            .map(|(delta_ms, count)| TimingDeltaCount { delta_ms, count })
            .collect(),
    }
}

fn stream_key_tuple(bpm_ip: &str, stream_key: &str) -> String {
    format!("{bpm_ip}\n{stream_key}")
}

fn captured_status(
    stream: Option<&CapturedStreamEntry>,
    target_ms: u64,
    tolerance_ms: u64,
) -> (String, Option<i64>) {
    let Some(stream) = stream else {
        return ("MISSING_CAPTURE".to_string(), None);
    };
    let delta = signed_delta_ms(stream.stream_ms, target_ms);
    if stream.payload_file.is_none() || stream.payload_bytes == 0 {
        return ("PAYLOAD_MISSING".to_string(), Some(delta));
    }
    if stream.sample_count.is_none() {
        return ("PAYLOAD_MALFORMED".to_string(), Some(delta));
    }
    if delta < -(tolerance_ms as i64) {
        return ("STALE_CAPTURE".to_string(), Some(delta));
    }
    if delta > tolerance_ms as i64 {
        return ("AHEAD_CAPTURE".to_string(), Some(delta));
    }
    ("COMPLETE".to_string(), Some(delta))
}

fn latest_status(
    observation: Option<&CaptureObservation>,
    target_ms: u64,
    tolerance_ms: u64,
    captured_ok: bool,
) -> (String, Option<i64>) {
    let Some(observation) = observation else {
        return ("LATEST_MISSING".to_string(), None);
    };
    let delta = signed_delta_ms(observation.ms, target_ms);
    if delta < -(tolerance_ms as i64) {
        if captured_ok {
            return ("LATEST_STALE_BUT_CAPTURED_OK".to_string(), Some(delta));
        }
        return ("LATEST_STALE".to_string(), Some(delta));
    }
    if delta > tolerance_ms as i64 {
        return ("LATEST_AHEAD".to_string(), Some(delta));
    }
    ("COMPLETE".to_string(), Some(delta))
}

fn build_capture_diagnostics(
    spill: &CapturedSpill,
    streams: &[CapturedStreamEntry],
) -> CaptureDiagnostics {
    let target_ms = spill.target_ms;
    let tolerance_ms = spill.same_spill_tolerance_ms;
    let captured_by_key = streams
        .iter()
        .map(|stream| (stream_key_tuple(&stream.bpm_ip, &stream.stream_key), stream))
        .collect::<HashMap<_, _>>();
    let latest_by_key = spill
        .latest_observations
        .iter()
        .map(|obs| (stream_key_tuple(&obs.bpm_ip, &obs.stream_key), obs))
        .collect::<HashMap<_, _>>();

    let mut stream_rows = Vec::<StreamDiagnosticRow>::new();
    let mut captured_deltas = Vec::<i64>::new();
    let mut latest_deltas = Vec::<i64>::new();

    for inventory in &spill.stream_inventory {
        let key = stream_key_tuple(&inventory.bpm_ip, &inventory.stream_key);
        let captured = captured_by_key.get(&key).copied();
        let latest = latest_by_key.get(&key).copied();
        let (captured_reason, captured_delta) = captured_status(captured, target_ms, tolerance_ms);
        let captured_ok = captured_reason == "COMPLETE";
        let (latest_reason, latest_delta) =
            latest_status(latest, target_ms, tolerance_ms, captured_ok);

        if let Some(delta) = captured_delta {
            captured_deltas.push(delta);
        }
        if let Some(delta) = latest_delta {
            latest_deltas.push(delta);
        }

        let primary_reason = if captured_reason != "COMPLETE" {
            captured_reason.clone()
        } else {
            latest_reason.clone()
        };

        stream_rows.push(StreamDiagnosticRow {
            device_label: inventory.device_label.clone(),
            bpm_ip: inventory.bpm_ip.clone(),
            plane: inventory.plane.label().to_string(),
            stream_key: inventory.stream_key.clone(),
            captured_status: captured_reason,
            latest_poll_status: latest_reason,
            primary_reason,
            captured_stream_id: captured.map(|stream| stream.stream_id.clone()),
            captured_ms: captured.map(|stream| stream.stream_ms),
            captured_delta_ms: captured_delta,
            latest_id: latest.map(|obs| obs.id.clone()),
            latest_ms: latest.map(|obs| obs.ms),
            latest_delta_ms: latest_delta,
            payload_bytes: captured.map(|stream| stream.payload_bytes),
            sample_count: captured.and_then(|stream| stream.sample_count),
        });
    }

    let mut digitizer_map = HashMap::<String, DigitizerDiagnosticRow>::new();
    for row in &stream_rows {
        let entry =
            digitizer_map
                .entry(row.bpm_ip.clone())
                .or_insert_with(|| DigitizerDiagnosticRow {
                    device_label: row.device_label.clone(),
                    bpm_ip: row.bpm_ip.clone(),
                    status: "Complete".to_string(),
                    configured_streams: 0,
                    complete_streams: 0,
                    same_spill_streams: 0,
                    missing_capture_streams: 0,
                    stale_capture_streams: 0,
                    ahead_capture_streams: 0,
                    payload_issue_streams: 0,
                    latest_stale_streams: 0,
                    latest_missing_streams: 0,
                    latest_ahead_streams: 0,
                    suspect: false,
                    latest_poll_suspect: false,
                });
        entry.configured_streams += 1;
        if row.captured_status == "COMPLETE" {
            entry.complete_streams += 1;
        }
        if row
            .captured_delta_ms
            .is_some_and(|delta| delta.unsigned_abs() <= tolerance_ms)
        {
            entry.same_spill_streams += 1;
        }
        match row.captured_status.as_str() {
            "MISSING_CAPTURE" => entry.missing_capture_streams += 1,
            "STALE_CAPTURE" => entry.stale_capture_streams += 1,
            "AHEAD_CAPTURE" => entry.ahead_capture_streams += 1,
            "PAYLOAD_MISSING" | "PAYLOAD_MALFORMED" => entry.payload_issue_streams += 1,
            _ => {}
        }
        match row.latest_poll_status.as_str() {
            "LATEST_STALE" | "LATEST_STALE_BUT_CAPTURED_OK" => entry.latest_stale_streams += 1,
            "LATEST_MISSING" => entry.latest_missing_streams += 1,
            "LATEST_AHEAD" => entry.latest_ahead_streams += 1,
            _ => {}
        }
    }

    let mut digitizers = digitizer_map.into_values().collect::<Vec<_>>();
    digitizers.sort_by(|a, b| a.bpm_ip.cmp(&b.bpm_ip));
    for digitizer in &mut digitizers {
        digitizer.suspect = digitizer.missing_capture_streams > 0
            || digitizer.stale_capture_streams > 0
            || digitizer.ahead_capture_streams > 0
            || digitizer.payload_issue_streams > 0;
        digitizer.latest_poll_suspect = digitizer.latest_stale_streams > 0
            || digitizer.latest_missing_streams > 0
            || digitizer.latest_ahead_streams > 0;
        digitizer.status = if digitizer.suspect {
            "Partial".to_string()
        } else {
            "Complete".to_string()
        };
    }

    let complete_streams = stream_rows
        .iter()
        .filter(|row| row.captured_status == "COMPLETE")
        .count();
    let same_spill_streams = stream_rows
        .iter()
        .filter(|row| {
            row.captured_delta_ms
                .is_some_and(|delta| delta.unsigned_abs() <= tolerance_ms)
        })
        .count();
    let missing_streams = stream_rows
        .iter()
        .filter(|row| row.captured_status == "MISSING_CAPTURE")
        .count();
    let stale_capture_streams = stream_rows
        .iter()
        .filter(|row| row.captured_status == "STALE_CAPTURE")
        .count();
    let ahead_capture_streams = stream_rows
        .iter()
        .filter(|row| row.captured_status == "AHEAD_CAPTURE")
        .count();
    let payload_issue_streams = stream_rows
        .iter()
        .filter(|row| {
            matches!(
                row.captured_status.as_str(),
                "PAYLOAD_MISSING" | "PAYLOAD_MALFORMED"
            )
        })
        .count();
    let latest_stale_streams = stream_rows
        .iter()
        .filter(|row| {
            matches!(
                row.latest_poll_status.as_str(),
                "LATEST_STALE" | "LATEST_STALE_BUT_CAPTURED_OK"
            )
        })
        .count();
    let latest_missing_streams = stream_rows
        .iter()
        .filter(|row| row.latest_poll_status == "LATEST_MISSING")
        .count();
    let latest_ahead_streams = stream_rows
        .iter()
        .filter(|row| row.latest_poll_status == "LATEST_AHEAD")
        .count();
    let suspect_digitizers = digitizers.iter().filter(|row| row.suspect).count();
    let latest_poll_suspect_digitizers = digitizers
        .iter()
        .filter(|row| row.latest_poll_suspect)
        .count();
    let status = if missing_streams == 0
        && stale_capture_streams == 0
        && ahead_capture_streams == 0
        && payload_issue_streams == 0
        && complete_streams == spill.requested_streams
    {
        "Complete"
    } else {
        "Partial"
    }
    .to_string();

    CaptureDiagnostics {
        same_spill_tolerance_ms: tolerance_ms,
        status,
        requested_streams: spill.requested_streams,
        captured_streams: streams.len(),
        complete_streams,
        same_spill_streams,
        missing_streams,
        stale_capture_streams,
        ahead_capture_streams,
        payload_issue_streams,
        latest_stale_streams,
        latest_missing_streams,
        latest_ahead_streams,
        suspect_digitizers,
        latest_poll_suspect_digitizers,
        wake_delta_ms: spill
            .wake
            .as_ref()
            .map(|wake| signed_delta_ms(wake.ms, spill.target_ms)),
        captured_timing: timing_summary(&captured_deltas, tolerance_ms),
        latest_timing: timing_summary(&latest_deltas, tolerance_ms),
        streams: stream_rows,
        digitizers,
    }
}

fn median(values: &[f64]) -> f64 {
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(Ordering::Equal));
    let mid = sorted.len() / 2;
    if sorted.len() % 2 == 0 {
        (sorted[mid - 1] + sorted[mid]) * 0.5
    } else {
        sorted[mid]
    }
}

fn payload_file_name(idx: usize, stream: &CapturedStreamEntry) -> String {
    format!(
        "stream_{idx:03}_{}_{}_{}.bin",
        stream.plane.label(),
        stream.stream_ms,
        sanitize_path_component(&stream.stream_key, 96)
    )
}

fn sanitize_path_component(value: &str, max_len: usize) -> String {
    let mut out = String::new();
    let mut last_was_underscore = false;
    for ch in value.chars() {
        let safe = if ch.is_ascii_alphanumeric() || matches!(ch, '-' | '.') {
            ch
        } else {
            '_'
        };
        if safe == '_' {
            if last_was_underscore {
                continue;
            }
            last_was_underscore = true;
        } else {
            last_was_underscore = false;
        }
        if out.len() + safe.len_utf8() > max_len {
            break;
        }
        out.push(safe);
    }
    out.trim_matches('_').to_string()
}

fn fnv1a64_hex(bytes: &[u8]) -> String {
    let mut hash = 0xcbf29ce484222325u64;
    for byte in bytes {
        hash ^= *byte as u64;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}

fn manifest_json(
    spill: &CapturedSpill,
    streams: &[CapturedStreamEntry],
    diagnostics: &CaptureDiagnostics,
) -> String {
    let mut out = String::new();
    out.push_str("{\n");
    out.push_str(&format!(
        "  \"schema_version\": {},\n",
        spill.schema_version
    ));
    out.push_str(&format!(
        "  \"artifact_type\": {},\n",
        json_string(spill.artifact_type)
    ));
    out.push_str(&format!(
        "  \"redis_timestamp_ms\": {},\n",
        spill.redis_timestamp_ms
    ));
    out.push_str(&format!("  \"target_ms\": {},\n", spill.target_ms));
    out.push_str(&format!(
        "  \"align_tolerance_ms\": {},\n",
        spill.align_tolerance_ms
    ));
    out.push_str(&format!(
        "  \"same_spill_tolerance_ms\": {},\n",
        spill.same_spill_tolerance_ms
    ));
    out.push_str(&format!(
        "  \"min_aligned_fraction\": {:.6},\n",
        spill.min_aligned_fraction
    ));
    match spill.wake.as_ref() {
        Some(wake) => out.push_str(&format!(
            "  \"wake\": {{\"bpm_ip\": {}, \"stream_id\": {}, \"ms\": {}, \"delta_ms\": {}}},\n",
            json_string(&wake.bpm_ip),
            json_string(&wake.stream_id),
            wake.ms,
            signed_delta_ms(wake.ms, spill.target_ms)
        )),
        None => out.push_str("  \"wake\": null,\n"),
    }
    out.push_str(&format!(
        "  \"requested_streams\": {},\n",
        spill.requested_streams
    ));
    out.push_str(&format!(
        "  \"latest_observation_count\": {},\n",
        spill.latest_observations.len()
    ));
    let latest_same_spill_streams = spill
        .latest_observations
        .iter()
        .filter(|obs| obs.aligned)
        .count();
    out.push_str(&format!(
        "  \"latest_same_spill_streams\": {},\n",
        latest_same_spill_streams
    ));
    out.push_str(&format!(
        "  \"aligned_latest_streams\": {},\n",
        latest_same_spill_streams
    ));
    out.push_str(&format!("  \"captured_streams\": {},\n", streams.len()));
    out.push_str(&format!(
        "  \"payload_checksum_algorithm\": {},\n",
        json_string(PAYLOAD_CHECKSUM_ALGORITHM)
    ));
    out.push_str(&format!(
        "  \"raw_payload_format\": {},\n",
        json_string(RAW_PAYLOAD_FORMAT)
    ));
    out.push_str("  \"stream_inventory\": [\n");
    for (idx, entry) in spill.stream_inventory.iter().enumerate() {
        out.push_str(&format!(
            "    {{\"device_label\": {}, \"bpm_ip\": {}, \"stream_key\": {}, \"plane\": {}}}{}\n",
            json_string(&entry.device_label),
            json_string(&entry.bpm_ip),
            json_string(&entry.stream_key),
            json_string(entry.plane.label()),
            comma(idx, spill.stream_inventory.len())
        ));
    }
    out.push_str("  ],\n");
    out.push_str("  \"latest_observations\": [\n");
    for (idx, obs) in spill.latest_observations.iter().enumerate() {
        out.push_str(&format!(
            "    {{\"bpm_ip\": {}, \"stream_key\": {}, \"id\": {}, \"ms\": {}, \"aligned\": {}}}{}\n",
            json_string(&obs.bpm_ip),
            json_string(&obs.stream_key),
            json_string(&obs.id),
            obs.ms,
            obs.aligned,
            comma(idx, spill.latest_observations.len())
        ));
    }
    out.push_str("  ],\n");
    out.push_str("  \"streams\": [\n");
    for (idx, stream) in streams.iter().enumerate() {
        out.push_str(&format!(
            "    {{\"device_label\": {}, \"bpm_ip\": {}, \"stream_key\": {}, \"plane\": {}, \"stream_id\": {}, \"stream_ms\": {}, \"aligned\": {}, \"field_count\": {}, \"payload_file\": {}, \"payload_bytes\": {}, \"sample_count\": {}, \"checksum_fnv1a64\": {}}}{}\n",
            json_string(&stream.device_label),
            json_string(&stream.bpm_ip),
            json_string(&stream.stream_key),
            json_string(stream.plane.label()),
            json_string(&stream.stream_id),
            stream.stream_ms,
            stream.aligned,
            stream.field_count,
            json_opt_string(stream.payload_file.as_deref()),
            stream.payload_bytes,
            json_opt_usize(stream.sample_count),
            json_opt_string(stream.checksum_fnv1a64.as_deref()),
            comma(idx, streams.len())
        ));
    }
    out.push_str("  ],\n");
    out.push_str(&format!(
        "  \"warnings\": {},\n",
        json_string_array(&spill.warnings)
    ));
    out.push_str("  \"capture_diagnostics\": ");
    out.push_str(&capture_diagnostics_json(diagnostics));
    out.push('\n');
    out.push_str("}\n");
    out
}

fn capture_summary_lines(
    spill: &CapturedSpill,
    streams: &[CapturedStreamEntry],
    diagnostics: &CaptureDiagnostics,
) -> Vec<String> {
    let mut lines = Vec::new();
    lines.push("captured-spill summary".to_string());
    lines.push(format!("schema_version: {}", spill.schema_version));
    lines.push(format!("artifact_type: {}", spill.artifact_type));
    lines.push(format!("redis_timestamp_ms: {}", spill.redis_timestamp_ms));
    lines.push(format!("target_ms: {}", spill.target_ms));
    lines.push(format!("align_tolerance_ms: {}", spill.align_tolerance_ms));
    lines.push(format!(
        "same_spill_tolerance_ms: {}",
        spill.same_spill_tolerance_ms
    ));
    lines.push(format!("capture_status: {}", diagnostics.status));
    lines.push(format!("requested_streams: {}", spill.requested_streams));
    lines.push(format!(
        "latest_observations: {}",
        spill.latest_observations.len()
    ));
    lines.push(format!(
        "latest_same_spill_streams: {}",
        diagnostics.latest_timing.same_spill_count
    ));
    lines.push(format!("captured_streams: {}", streams.len()));
    lines.push(format!(
        "complete_streams: {}",
        diagnostics.complete_streams
    ));
    lines.push(format!(
        "suspect_digitizers: {}",
        diagnostics.suspect_digitizers
    ));
    lines.push(format!(
        "latest_poll_suspect_digitizers: {}",
        diagnostics.latest_poll_suspect_digitizers
    ));
    lines.push(format!(
        "captured_payload_timestamp_counts: {}",
        timing_bucket_counts_text(&diagnostics.captured_timing)
    ));
    lines.push(format!(
        "captured_payload_delta_ms: {}",
        timing_range_text(&diagnostics.captured_timing)
    ));
    lines.push(format!(
        "captured_payload_delta_distribution: {}",
        format_delta_distribution(&diagnostics.captured_timing)
    ));
    lines.push(format!(
        "latest_id_snapshot_timestamp_counts: {}",
        timing_bucket_counts_text(&diagnostics.latest_timing)
    ));
    lines.push(format!(
        "latest_id_snapshot_delta_ms: {}",
        timing_range_text(&diagnostics.latest_timing)
    ));
    lines.push(format!(
        "latest_id_snapshot_delta_distribution: {}",
        format_delta_distribution(&diagnostics.latest_timing)
    ));
    lines.push("payloads:".to_string());
    for stream in streams {
        lines.push(format!(
            "  {} {} {} id={} bytes={} samples={} checksum={} file={}",
            stream.bpm_ip,
            stream.plane.label(),
            stream.stream_key,
            stream.stream_id,
            stream.payload_bytes,
            opt_usize(stream.sample_count),
            stream.checksum_fnv1a64.as_deref().unwrap_or("NA"),
            stream.payload_file.as_deref().unwrap_or("NA")
        ));
    }
    if !spill.warnings.is_empty() {
        lines.push("warnings:".to_string());
        for warning in &spill.warnings {
            lines.push(format!("  - {warning}"));
        }
    }
    lines
}

fn print_capture_summary(result: &CaptureWriteResult, spill: &CapturedSpill, title: &str) {
    println!("{title}");
    println!("  target_ms: {}", result.target_ms);
    println!("  bundle_dir: {}", result.bundle_dir.display());
    println!("  manifest: {}", result.manifest_path.display());
    println!("  summary: {}", result.summary_path.display());
    println!(
        "  capture status: {} (same-spill tolerance ±{} ms)",
        result.diagnostics.status, result.diagnostics.same_spill_tolerance_ms
    );
    println!(
        "  streams: captured {} of {} configured",
        result.captured_streams, result.requested_streams
    );
    println!(
        "  complete streams: {} of {} configured",
        result.diagnostics.complete_streams, result.requested_streams
    );
    if result.diagnostics.suspect_digitizers > 0
        || result.diagnostics.latest_poll_suspect_digitizers > 0
    {
        println!(
            "  suspect digitizers: capture={} latest_poll={}",
            result.diagnostics.suspect_digitizers,
            result.diagnostics.latest_poll_suspect_digitizers
        );
    }
    println!(
        "  captured payload timestamps: {}",
        timing_bucket_counts_text(&result.diagnostics.captured_timing)
    );
    println!(
        "  captured payload delta_ms: {}; distribution: {}",
        timing_range_text(&result.diagnostics.captured_timing),
        format_delta_distribution(&result.diagnostics.captured_timing)
    );
    println!(
        "  latest-ID snapshot timestamps: {}; observations={}",
        timing_bucket_counts_text(&result.diagnostics.latest_timing),
        result.latest_observations
    );
    println!(
        "  latest-ID snapshot delta_ms: {}; distribution: {}",
        timing_range_text(&result.diagnostics.latest_timing),
        format_delta_distribution(&result.diagnostics.latest_timing)
    );
    if !spill.warnings.is_empty() {
        println!("  warnings:");
        for warning in &spill.warnings {
            println!("    - {warning}");
        }
    }
}

fn opt_usize(value: Option<usize>) -> String {
    value
        .map(|v| v.to_string())
        .unwrap_or_else(|| "NA".to_string())
}

fn opt_u64(value: Option<u64>) -> String {
    value
        .map(|v| v.to_string())
        .unwrap_or_else(|| "NA".to_string())
}

fn opt_i64(value: Option<i64>) -> String {
    value
        .map(|v| v.to_string())
        .unwrap_or_else(|| "NA".to_string())
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

fn json_opt_usize(value: Option<usize>) -> String {
    value
        .map(|v| v.to_string())
        .unwrap_or_else(|| "null".to_string())
}

fn json_opt_u64(value: Option<u64>) -> String {
    value
        .map(|v| v.to_string())
        .unwrap_or_else(|| "null".to_string())
}

fn json_opt_i64(value: Option<i64>) -> String {
    value
        .map(|v| v.to_string())
        .unwrap_or_else(|| "null".to_string())
}

fn json_opt_f64(value: Option<f64>) -> String {
    value
        .map(|v| format!("{v:.3}"))
        .unwrap_or_else(|| "null".to_string())
}

fn json_string_array(values: &[String]) -> String {
    let parts = values.iter().map(|v| json_string(v)).collect::<Vec<_>>();
    format!("[{}]", parts.join(","))
}

fn timing_summary_json(summary: &TimingSummary) -> String {
    let mut out = String::new();
    out.push_str("{");
    out.push_str(&format!("\"count\":{},", summary.count));
    out.push_str(&format!(
        "\"min_delta_ms\":{},",
        json_opt_i64(summary.min_delta_ms)
    ));
    out.push_str(&format!(
        "\"max_delta_ms\":{},",
        json_opt_i64(summary.max_delta_ms)
    ));
    out.push_str(&format!(
        "\"median_abs_delta_ms\":{},",
        json_opt_f64(summary.median_abs_delta_ms)
    ));
    out.push_str(&format!(
        "\"max_abs_delta_ms\":{},",
        json_opt_u64(summary.max_abs_delta_ms)
    ));
    out.push_str(&format!(
        "\"same_spill_count\":{},",
        summary.same_spill_count
    ));
    out.push_str(&format!("\"stale_count\":{},", summary.stale_count));
    out.push_str(&format!("\"ahead_count\":{},", summary.ahead_count));
    out.push_str("\"delta_counts\":[");
    for (idx, bucket) in summary.delta_counts.iter().enumerate() {
        out.push_str(&format!(
            "{{\"delta_ms\":{},\"stream_count\":{}}}{}",
            bucket.delta_ms,
            bucket.count,
            comma(idx, summary.delta_counts.len())
        ));
    }
    out.push_str("]}");
    out
}

fn capture_diagnostics_json(diagnostics: &CaptureDiagnostics) -> String {
    let mut out = String::new();
    out.push_str("{\n");
    out.push_str(&format!(
        "    \"same_spill_tolerance_ms\": {},\n",
        diagnostics.same_spill_tolerance_ms
    ));
    out.push_str(&format!(
        "    \"status\": {},\n",
        json_string(&diagnostics.status)
    ));
    out.push_str(&format!(
        "    \"requested_streams\": {},\n",
        diagnostics.requested_streams
    ));
    out.push_str(&format!(
        "    \"captured_streams\": {},\n",
        diagnostics.captured_streams
    ));
    out.push_str(&format!(
        "    \"complete_streams\": {},\n",
        diagnostics.complete_streams
    ));
    out.push_str(&format!(
        "    \"same_spill_streams\": {},\n",
        diagnostics.same_spill_streams
    ));
    out.push_str(&format!(
        "    \"missing_streams\": {},\n",
        diagnostics.missing_streams
    ));
    out.push_str(&format!(
        "    \"stale_capture_streams\": {},\n",
        diagnostics.stale_capture_streams
    ));
    out.push_str(&format!(
        "    \"ahead_capture_streams\": {},\n",
        diagnostics.ahead_capture_streams
    ));
    out.push_str(&format!(
        "    \"payload_issue_streams\": {},\n",
        diagnostics.payload_issue_streams
    ));
    out.push_str(&format!(
        "    \"latest_stale_streams\": {},\n",
        diagnostics.latest_stale_streams
    ));
    out.push_str(&format!(
        "    \"latest_missing_streams\": {},\n",
        diagnostics.latest_missing_streams
    ));
    out.push_str(&format!(
        "    \"latest_ahead_streams\": {},\n",
        diagnostics.latest_ahead_streams
    ));
    out.push_str(&format!(
        "    \"suspect_digitizers\": {},\n",
        diagnostics.suspect_digitizers
    ));
    out.push_str(&format!(
        "    \"latest_poll_suspect_digitizers\": {},\n",
        diagnostics.latest_poll_suspect_digitizers
    ));
    out.push_str(&format!(
        "    \"wake_delta_ms\": {},\n",
        json_opt_i64(diagnostics.wake_delta_ms)
    ));
    out.push_str(&format!(
        "    \"captured_timing\": {},\n",
        timing_summary_json(&diagnostics.captured_timing)
    ));
    out.push_str(&format!(
        "    \"latest_timing\": {},\n",
        timing_summary_json(&diagnostics.latest_timing)
    ));
    out.push_str("    \"streams\": [\n");
    for (idx, row) in diagnostics.streams.iter().enumerate() {
        out.push_str(&format!(
            "      {{\"device_label\": {}, \"bpm_ip\": {}, \"plane\": {}, \"stream_key\": {}, \"captured_status\": {}, \"latest_poll_status\": {}, \"primary_reason\": {}, \"captured_stream_id\": {}, \"captured_ms\": {}, \"captured_delta_ms\": {}, \"latest_id\": {}, \"latest_ms\": {}, \"latest_delta_ms\": {}, \"payload_bytes\": {}, \"sample_count\": {}}}{}\n",
            json_string(&row.device_label),
            json_string(&row.bpm_ip),
            json_string(&row.plane),
            json_string(&row.stream_key),
            json_string(&row.captured_status),
            json_string(&row.latest_poll_status),
            json_string(&row.primary_reason),
            json_opt_string(row.captured_stream_id.as_deref()),
            json_opt_u64(row.captured_ms),
            json_opt_i64(row.captured_delta_ms),
            json_opt_string(row.latest_id.as_deref()),
            json_opt_u64(row.latest_ms),
            json_opt_i64(row.latest_delta_ms),
            json_opt_usize(row.payload_bytes),
            json_opt_usize(row.sample_count),
            comma(idx, diagnostics.streams.len())
        ));
    }
    out.push_str("    ],\n");
    out.push_str("    \"digitizers\": [\n");
    for (idx, row) in diagnostics.digitizers.iter().enumerate() {
        out.push_str(&format!(
            "      {{\"device_label\": {}, \"bpm_ip\": {}, \"status\": {}, \"configured_streams\": {}, \"complete_streams\": {}, \"same_spill_streams\": {}, \"missing_capture_streams\": {}, \"stale_capture_streams\": {}, \"ahead_capture_streams\": {}, \"payload_issue_streams\": {}, \"latest_stale_streams\": {}, \"latest_missing_streams\": {}, \"latest_ahead_streams\": {}, \"suspect\": {}, \"latest_poll_suspect\": {}}}{}\n",
            json_string(&row.device_label),
            json_string(&row.bpm_ip),
            json_string(&row.status),
            row.configured_streams,
            row.complete_streams,
            row.same_spill_streams,
            row.missing_capture_streams,
            row.stale_capture_streams,
            row.ahead_capture_streams,
            row.payload_issue_streams,
            row.latest_stale_streams,
            row.latest_missing_streams,
            row.latest_ahead_streams,
            row.suspect,
            row.latest_poll_suspect,
            comma(idx, diagnostics.digitizers.len())
        ));
    }
    out.push_str("    ]\n");
    out.push_str("  }");
    out
}

fn csv_escape(value: &str) -> String {
    if value.contains(',') || value.contains('"') || value.contains('\n') {
        format!("\"{}\"", value.replace('"', "\"\""))
    } else {
        value.to_string()
    }
}

fn csv_opt_usize(value: Option<usize>) -> String {
    value.map(|v| v.to_string()).unwrap_or_default()
}

fn csv_opt_u64(value: Option<u64>) -> String {
    value.map(|v| v.to_string()).unwrap_or_default()
}

fn csv_opt_i64(value: Option<i64>) -> String {
    value.map(|v| v.to_string()).unwrap_or_default()
}

fn csv_opt_f64(value: Option<f64>) -> String {
    value.map(|v| format!("{v:.3}")).unwrap_or_default()
}

fn comma(idx: usize, len: usize) -> &'static str {
    if idx + 1 == len { "" } else { "," }
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::*;
    use crate::config::{DeviceConfig, RedisConfig};

    fn temp_dir(name: &str) -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock should be after epoch")
            .as_nanos();
        std::env::temp_dir().join(format!("tbt-monitor-{name}-{unique}"))
    }

    fn sample_spill() -> CapturedSpill {
        let payload = vec![0, 0, 0x80, 0x3f, 0, 0, 0, 0x40];
        CapturedSpill {
            schema_version: CAPTURE_SCHEMA_VERSION,
            artifact_type: CAPTURE_ARTIFACT_TYPE,
            redis_timestamp_ms: 1772830005123,
            target_ms: 1772830005123,
            align_tolerance_ms: 1,
            same_spill_tolerance_ms: 25,
            min_aligned_fraction: 0.70,
            wake: None,
            requested_streams: 2,
            stream_inventory: vec![
                CaptureStreamInventoryEntry {
                    device_label: "BPM A".to_string(),
                    bpm_ip: "10.0.0.1".to_string(),
                    stream_key: "{MUON:BPM:10.0.0.1}:HP101:TBT_POSITION_SCALED".to_string(),
                    plane: Plane::Horizontal,
                },
                CaptureStreamInventoryEntry {
                    device_label: "BPM A".to_string(),
                    bpm_ip: "10.0.0.1".to_string(),
                    stream_key: "{MUON:BPM:10.0.0.1}:VP101:TBT_POSITION_SCALED".to_string(),
                    plane: Plane::Vertical,
                },
            ],
            latest_observations: vec![CaptureObservation {
                bpm_ip: "10.0.0.1".to_string(),
                stream_key: "{MUON:BPM:10.0.0.1}:HP101:TBT_POSITION_SCALED".to_string(),
                id: "1772830005123-0".to_string(),
                ms: 1772830005123,
                aligned: true,
            }],
            streams: vec![CapturedStreamEntry {
                device_label: "BPM A".to_string(),
                bpm_ip: "10.0.0.1".to_string(),
                stream_key: "{MUON:BPM:10.0.0.1}:HP101:TBT_POSITION_SCALED".to_string(),
                plane: Plane::Horizontal,
                stream_id: "1772830005123-0".to_string(),
                stream_ms: 1772830005123,
                aligned: true,
                field_count: 1,
                payload_bytes: payload.len(),
                sample_count: Some(2),
                checksum_fnv1a64: Some(fnv1a64_hex(&payload)),
                payload: Some(payload),
                payload_file: None,
            }],
            warnings: vec!["incomplete near-target capture".to_string()],
        }
    }

    fn sample_complete_spill() -> CapturedSpill {
        let mut spill = sample_spill();
        spill.requested_streams = 1;
        spill.stream_inventory.truncate(1);
        spill.latest_observations[0].ms = spill.target_ms - 15_000;
        spill.latest_observations[0].id = format!("{}-0", spill.latest_observations[0].ms);
        spill.streams[0].payload_file = Some("payloads/test.bin".to_string());
        spill
    }

    fn test_config() -> MonitorConfig {
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
            capture_intensity_variant: None,
            devices: vec![DeviceConfig {
                label: "BPM A".to_string(),
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
                stream_keys: vec![
                    "{MUON:BPM:10.0.0.1}:HP101:TBT_POSITION_SCALED".to_string(),
                    "{MUON:BPM:10.0.0.1}:VP102:TBT_POSITION_SCALED".to_string(),
                ],
            }],
        }
    }

    #[test]
    fn choose_target_millisecond_merges_adjacent_buckets() {
        let target = choose_target_millisecond(&[10, 10, 11, 11, 20, 20, 20], 1);
        assert_eq!(target, Some(11));
    }

    #[test]
    fn sanitize_path_component_removes_unsafe_characters() {
        let sanitized =
            sanitize_path_component("{MUON:BPM:10.0.0.1}:HP101:TBT_POSITION_SCALED", 32);
        assert!(sanitized.starts_with("MUON_BPM_10.0.0.1_HP101_TBT"));
        assert!(sanitized.len() <= 32);
        assert!(!sanitized.contains(':'));
        assert!(!sanitized.contains('{'));
    }

    #[test]
    fn raw_position_streams_classify_as_tbt_planes() {
        assert_eq!(
            classify_plane("{MUON:BPM:10.0.0.1}:HP101:TBT_POSITION_RAW"),
            Some(Plane::Horizontal)
        );
        assert_eq!(
            classify_plane("{MUON:BPM:10.0.0.1}:VP102:TBT_POSITION_RAW"),
            Some(Plane::Vertical)
        );
        assert_eq!(
            classify_plane("{MUON:BPM:10.0.0.1}:HP101:TBT_INTENSITY_RAW"),
            None
        );
    }

    #[test]
    fn capture_inventory_derives_raw_intensity_streams() {
        let mut config = test_config();
        config.capture_intensity_variant = Some("raw".to_string());
        config.devices[0].stream_keys = vec![
            "{MUON:BPM:10.0.0.1}:HP101:TBT_POSITION_RAW".to_string(),
            "{MUON:BPM:10.0.0.1}:VP102:TBT_POSITION_RAW".to_string(),
        ];

        let inventory = collect_stream_inventory(&config);
        let keys = inventory
            .iter()
            .map(|entry| entry.stream_key.as_str())
            .collect::<Vec<_>>();

        assert_eq!(inventory.len(), 4);
        assert!(keys.contains(&"{MUON:BPM:10.0.0.1}:HP101:TBT_POSITION_RAW"));
        assert!(keys.contains(&"{MUON:BPM:10.0.0.1}:HP101:TBT_INTENSITY_RAW"));
        assert!(keys.contains(&"{MUON:BPM:10.0.0.1}:VP102:TBT_POSITION_RAW"));
        assert!(keys.contains(&"{MUON:BPM:10.0.0.1}:VP102:TBT_INTENSITY_RAW"));
        assert_eq!(
            collect_tbt_stream_keys(&config.devices[0]),
            vec![
                "{MUON:BPM:10.0.0.1}:HP101:TBT_POSITION_RAW".to_string(),
                "{MUON:BPM:10.0.0.1}:VP102:TBT_POSITION_RAW".to_string()
            ]
        );
    }

    #[test]
    fn write_bundle_persists_manifest_payload_and_summary() {
        let dir = temp_dir("bundle");
        let spill = sample_spill();
        let result = write_capture_bundle(&dir, &spill).expect("bundle should write");

        let manifest = fs::read_to_string(&result.manifest_path).expect("manifest should exist");
        assert!(manifest.contains("\"schema_version\": 1"));
        assert!(manifest.contains("\"artifact_type\": \"tbt-monitor.captured-spill\""));
        assert!(manifest.contains("\"same_spill_tolerance_ms\": 25"));
        assert!(manifest.contains("\"capture_diagnostics\""));
        assert!(manifest.contains("\"delta_counts\""));
        assert!(manifest.contains("\"stream_inventory\""));
        assert!(manifest.contains("\"payload_checksum_algorithm\": \"fnv1a64\""));
        assert!(manifest.contains("\"payload_file\": \"payloads/stream_000_H_1772830005123_"));
        assert!(result.summary_path.exists());
        let summary = fs::read_to_string(&result.summary_path).expect("summary should read");
        assert!(summary.contains("captured_payload_delta_distribution"));
        assert!(summary.contains("latest_id_snapshot_delta_distribution"));

        let payload_path = result
            .bundle_dir
            .join("payloads")
            .read_dir()
            .expect("payload dir should exist")
            .next()
            .expect("payload file should exist")
            .expect("payload dir entry should read")
            .path();
        let payload = fs::read(payload_path).expect("payload should read");
        assert_eq!(payload, vec![0, 0, 0x80, 0x3f, 0, 0, 0, 0x40]);

        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn write_capture_index_records_bundles() {
        let dir = temp_dir("index");
        fs::create_dir_all(&dir).expect("temp dir should be created");
        let spill = sample_spill();
        let diagnostics = build_capture_diagnostics(&spill, &spill.streams);
        let result = CaptureWriteResult {
            target_ms: 123,
            redis_timestamp_ms: 123,
            bundle_dir: dir.join("spill_123"),
            manifest_path: dir.join("spill_123/manifest.json"),
            summary_path: dir.join("spill_123/capture_summary.txt"),
            requested_streams: 2,
            latest_observations: 2,
            latest_same_spill_streams: 2,
            captured_streams: 2,
            warning_count: 0,
            diagnostics,
        };

        write_capture_index(&dir, &[result], 1, 2).expect("index should write");
        let index = fs::read_to_string(dir.join("capture_index.csv")).expect("index should read");
        assert!(index.contains("capture_index,target_ms,redis_timestamp_ms"));
        assert!(index.contains("0,123,123"));
        assert!(index.contains(",1,2"));

        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn same_spill_classification_uses_configured_tolerance() {
        let mut spill = sample_complete_spill();
        spill.streams[0].stream_ms = spill.target_ms + 25;
        spill.streams[0].stream_id = format!("{}-0", spill.streams[0].stream_ms);
        let diagnostics = build_capture_diagnostics(&spill, &spill.streams);
        assert_eq!(diagnostics.status, "Complete");
        assert_eq!(diagnostics.streams[0].captured_status, "COMPLETE");

        spill.streams[0].stream_ms = spill.target_ms + 26;
        spill.streams[0].stream_id = format!("{}-0", spill.streams[0].stream_ms);
        let diagnostics = build_capture_diagnostics(&spill, &spill.streams);
        assert_eq!(diagnostics.status, "Partial");
        assert_eq!(diagnostics.streams[0].captured_status, "AHEAD_CAPTURE");
    }

    #[test]
    fn latest_stale_but_captured_ok_remains_complete() {
        let spill = sample_complete_spill();
        let diagnostics = build_capture_diagnostics(&spill, &spill.streams);
        assert_eq!(diagnostics.status, "Complete");
        assert_eq!(
            diagnostics.streams[0].latest_poll_status,
            "LATEST_STALE_BUT_CAPTURED_OK"
        );
        assert_eq!(diagnostics.captured_timing.same_spill_count, 1);
        assert_eq!(diagnostics.captured_timing.delta_counts[0].delta_ms, 0);
        assert_eq!(diagnostics.captured_timing.delta_counts[0].count, 1);
        assert_eq!(diagnostics.latest_timing.stale_count, 1);
        assert_eq!(diagnostics.latest_timing.delta_counts[0].delta_ms, -15_000);
        assert_eq!(diagnostics.latest_timing.delta_counts[0].count, 1);
        assert_eq!(diagnostics.latest_poll_suspect_digitizers, 1);
        assert_eq!(diagnostics.suspect_digitizers, 0);
    }

    #[test]
    fn stale_capture_marks_spill_partial_and_digitizer_suspect() {
        let mut spill = sample_complete_spill();
        spill.streams[0].stream_ms = spill.target_ms - 26;
        spill.streams[0].stream_id = format!("{}-0", spill.streams[0].stream_ms);
        let diagnostics = build_capture_diagnostics(&spill, &spill.streams);
        assert_eq!(diagnostics.status, "Partial");
        assert_eq!(diagnostics.streams[0].captured_status, "STALE_CAPTURE");
        assert_eq!(diagnostics.suspect_digitizers, 1);
    }

    #[test]
    fn diagnose_captures_regenerates_run_outputs() {
        let dir = temp_dir("diagnose-source");
        let out_dir = temp_dir("diagnose-out");
        let spill = sample_complete_spill();
        write_capture_bundle(&dir, &spill).expect("bundle should write");
        run_diagnose_captures(&dir, &out_dir, Some(25)).expect("diagnostics should regenerate");

        assert!(out_dir.join("capture_spill_diagnostics.csv").exists());
        assert!(out_dir.join("capture_stream_diagnostics.csv").exists());
        assert!(out_dir.join("capture_timestamp_distribution.csv").exists());
        assert!(out_dir.join("capture_digitizer_diagnostics.csv").exists());
        assert!(out_dir.join("capture_quality_summary.json").exists());
        assert!(out_dir.join("capture_quality_report.md").exists());
        let distribution = fs::read_to_string(out_dir.join("capture_timestamp_distribution.csv"))
            .expect("distribution should read");
        assert!(distribution.contains("captured_payload"));
        assert!(distribution.contains("latest_id_snapshot"));

        let _ = fs::remove_dir_all(dir);
        let _ = fs::remove_dir_all(out_dir);
    }

    #[test]
    fn assess_snapshot_flags_stale_latest_digitizer() {
        let config = test_config();
        let target_ms = 1772830005123;
        let observations = vec![
            CaptureObservation {
                bpm_ip: "10.0.0.1".to_string(),
                stream_key: "{MUON:BPM:10.0.0.1}:HP101:TBT_POSITION_SCALED".to_string(),
                id: format!("{}-0", target_ms - 26),
                ms: target_ms - 26,
                aligned: false,
            },
            CaptureObservation {
                bpm_ip: "10.0.0.1".to_string(),
                stream_key: "{MUON:BPM:10.0.0.1}:VP102:TBT_POSITION_SCALED".to_string(),
                id: format!("{target_ms}-0"),
                ms: target_ms,
                aligned: true,
            },
        ];

        let snapshot = build_assess_snapshot(
            &config,
            0,
            "test",
            Some(target_ms),
            observations,
            Vec::new(),
        );
        assert_eq!(snapshot.digitizer_rows.len(), 1);
        assert!(snapshot.digitizer_rows[0].suspect);
        assert_eq!(snapshot.digitizer_rows[0].latest_stale_streams, 1);
    }
}
