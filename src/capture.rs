//! Raw spill capture workflows.
//!
//! This module keeps Redis acquisition separate from tune analysis. Capture
//! commands synchronize on the same stream-id millisecond policy used by the
//! analysis path, then persist complete raw `_` payload bytes and metadata for
//! later offline analysis.

use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Sender};
use std::thread;
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use redis::streams::{StreamReadOptions, StreamReadReply};
use redis::{Commands, Connection};

use crate::config::{DeviceConfig, MonitorConfig, RedisConfig};

const CAPTURE_SCHEMA_VERSION: u32 = 1;
const CAPTURE_ARTIFACT_TYPE: &str = "tbt-monitor.captured-spill";
const RAW_PAYLOAD_FORMAT: &str = "redis_stream_field_underscore_little_endian_f32_bytes";
const PAYLOAD_CHECKSUM_ALGORITHM: &str = "fnv1a64";
const DEFAULT_XRANGE_COUNT: usize = 128;
const FREE_RUN_SETTLE_RETRIES: usize = 3;
const FREE_RUN_SETTLE_DELAY_MS: u64 = 40;
const ADJACENT_BUCKET_TOLERANCE_MS: u64 = 1;

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
struct CapturedSpill {
    schema_version: u32,
    artifact_type: &'static str,
    redis_timestamp_ms: u64,
    target_ms: u64,
    align_tolerance_ms: u64,
    min_aligned_fraction: f64,
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
    pub aligned_latest_streams: usize,
    pub captured_streams: usize,
    pub warning_count: usize,
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

pub fn run_capture_spill(config: MonitorConfig, out_dir: &Path) -> Result<()> {
    let spill = capture_latest_spill_with_retries(&config)?;
    let result = write_capture_bundle(out_dir, &spill)?;
    print_capture_summary(&result, &spill, "capture-spill summary");
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

        let spill = match capture_latest_spill_with_retries(&config) {
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
    let requested_streams = count_requested_tbt_streams(config);
    let stream_inventory = collect_stream_inventory(config);

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
        &latest_observations.iter().map(|o| o.ms).collect::<Vec<_>>(),
        target_bucket_tolerance_ms(config),
    )
    .ok_or_else(|| anyhow!("failed to choose target TBT millisecond"))?;

    for obs in &mut latest_observations {
        obs.aligned = abs_diff_u64(obs.ms, target_ms) <= config.align_tolerance_ms;
    }
    push_alignment_warning(
        &latest_observations,
        config.min_aligned_fraction,
        &mut warnings,
    );

    let streams =
        collect_stream_entries(config, target_ms, config.align_tolerance_ms, &mut warnings)?;
    if streams.is_empty() {
        bail!(
            "no TBT stream entries were found within ±{} ms of target {}",
            config.align_tolerance_ms,
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
        min_aligned_fraction: config.min_aligned_fraction,
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
                "{}: no latest TBT entries found on configured position streams",
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
                plane,
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

    let manifest_path = bundle_dir.join("manifest.json");
    fs::write(&manifest_path, manifest_json(spill, &manifest_streams))
        .with_context(|| format!("failed to write manifest {}", manifest_path.display()))?;

    let summary_path = bundle_dir.join("capture_summary.txt");
    fs::write(
        &summary_path,
        capture_summary_lines(spill, &manifest_streams).join("\n") + "\n",
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
        aligned_latest_streams: spill
            .latest_observations
            .iter()
            .filter(|obs| obs.aligned)
            .count(),
        captured_streams: spill.streams.len(),
        warning_count: spill.warnings.len(),
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
        "capture_index,target_ms,redis_timestamp_ms,bundle_dir,manifest_path,requested_streams,latest_observations,aligned_latest_streams,captured_streams,warning_count,unresolved_wakes,duplicate_wakes"
            .to_string(),
    );

    for (idx, result) in results.iter().enumerate() {
        rows.push(format!(
            "{},{},{},{},{},{},{},{},{},{},{},{}",
            idx,
            result.target_ms,
            result.redis_timestamp_ms,
            csv_escape(&result.bundle_dir.display().to_string()),
            csv_escape(&result.manifest_path.display().to_string()),
            result.requested_streams,
            result.latest_observations,
            result.aligned_latest_streams,
            result.captured_streams,
            result.warning_count,
            unresolved_wakes,
            duplicate_wakes
        ));
    }

    let path = out_dir.join("capture_index.csv");
    fs::write(&path, rows.join("\n") + "\n")
        .with_context(|| format!("failed to write capture index {}", path.display()))
}

fn run_free_run_watch_worker(
    device: DeviceConfig,
    reconnect_initial_ms: u64,
    reconnect_max_ms: u64,
    tx: Sender<FreeRunSignal>,
) -> Result<()> {
    let keys = collect_tbt_stream_keys(&device);
    if keys.is_empty() {
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
        for stream_key in &device.stream_keys {
            let Some(plane) = classify_plane(stream_key) else {
                continue;
            };
            entries.push(CaptureStreamInventoryEntry {
                device_label: device.label.clone(),
                bpm_ip: device.bpm_ip.clone(),
                stream_key: stream_key.clone(),
                plane,
            });
        }
    }
    entries
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
    if key.contains(":HP") && key.ends_with(":TBT_POSITION_SCALED") {
        Some(Plane::Horizontal)
    } else if key.contains(":VP") && key.ends_with(":TBT_POSITION_SCALED") {
        Some(Plane::Vertical)
    } else {
        None
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
            "TBT stream alignment fraction {:.1}% is below configured minimum {:.1}%",
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
    config.align_tolerance_ms.min(ADJACENT_BUCKET_TOLERANCE_MS)
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

fn timeliness_stats(
    observations: &[CaptureObservation],
    target_ms: u64,
) -> Option<(i64, i64, f64, u64)> {
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
    Some((min_delta, max_delta, median(&abs_deltas), max_abs_delta))
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

fn manifest_json(spill: &CapturedSpill, streams: &[CapturedStreamEntry]) -> String {
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
        "  \"min_aligned_fraction\": {:.6},\n",
        spill.min_aligned_fraction
    ));
    out.push_str(&format!(
        "  \"requested_streams\": {},\n",
        spill.requested_streams
    ));
    out.push_str(&format!(
        "  \"latest_observation_count\": {},\n",
        spill.latest_observations.len()
    ));
    out.push_str(&format!(
        "  \"aligned_latest_streams\": {},\n",
        spill
            .latest_observations
            .iter()
            .filter(|obs| obs.aligned)
            .count()
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
        "  \"warnings\": {}\n",
        json_string_array(&spill.warnings)
    ));
    out.push_str("}\n");
    out
}

fn capture_summary_lines(spill: &CapturedSpill, streams: &[CapturedStreamEntry]) -> Vec<String> {
    let mut lines = Vec::new();
    lines.push("captured-spill summary".to_string());
    lines.push(format!("schema_version: {}", spill.schema_version));
    lines.push(format!("artifact_type: {}", spill.artifact_type));
    lines.push(format!("redis_timestamp_ms: {}", spill.redis_timestamp_ms));
    lines.push(format!("target_ms: {}", spill.target_ms));
    lines.push(format!("align_tolerance_ms: {}", spill.align_tolerance_ms));
    lines.push(format!("requested_streams: {}", spill.requested_streams));
    lines.push(format!(
        "latest_observations: {}",
        spill.latest_observations.len()
    ));
    lines.push(format!(
        "aligned_latest_streams: {}",
        spill
            .latest_observations
            .iter()
            .filter(|obs| obs.aligned)
            .count()
    ));
    lines.push(format!("captured_streams: {}", streams.len()));
    if let Some((min_delta, max_delta, median_abs_delta, max_abs_delta)) =
        timeliness_stats(&spill.latest_observations, spill.target_ms)
    {
        lines.push(format!(
            "TBT timeliness (obs ms - target_ms): min={} ms max={} ms median|delta|={:.2} ms max|delta|={} ms",
            min_delta, max_delta, median_abs_delta, max_abs_delta
        ));
    }
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
        "  streams: captured {} of {} configured",
        result.captured_streams, result.requested_streams
    );
    println!(
        "  latest observations: {} (aligned {})",
        result.latest_observations, result.aligned_latest_streams
    );
    if let Some((min_delta, max_delta, median_abs_delta, max_abs_delta)) =
        timeliness_stats(&spill.latest_observations, spill.target_ms)
    {
        println!(
            "  timeliness: min_delta={} ms max_delta={} ms median_abs={:.2} ms max_abs={} ms",
            min_delta, max_delta, median_abs_delta, max_abs_delta
        );
    }
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

fn json_string_array(values: &[String]) -> String {
    let parts = values.iter().map(|v| json_string(v)).collect::<Vec<_>>();
    format!("[{}]", parts.join(","))
}

fn csv_escape(value: &str) -> String {
    if value.contains(',') || value.contains('"') || value.contains('\n') {
        format!("\"{}\"", value.replace('"', "\"\""))
    } else {
        value.to_string()
    }
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
            min_aligned_fraction: 0.70,
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
    fn write_bundle_persists_manifest_payload_and_summary() {
        let dir = temp_dir("bundle");
        let spill = sample_spill();
        let result = write_capture_bundle(&dir, &spill).expect("bundle should write");

        let manifest = fs::read_to_string(&result.manifest_path).expect("manifest should exist");
        assert!(manifest.contains("\"schema_version\": 1"));
        assert!(manifest.contains("\"artifact_type\": \"tbt-monitor.captured-spill\""));
        assert!(manifest.contains("\"stream_inventory\""));
        assert!(manifest.contains("\"payload_checksum_algorithm\": \"fnv1a64\""));
        assert!(manifest.contains("\"payload_file\": \"payloads/stream_000_H_1772830005123_"));
        assert!(result.summary_path.exists());

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
        let result = CaptureWriteResult {
            target_ms: 123,
            redis_timestamp_ms: 123,
            bundle_dir: dir.join("spill_123"),
            manifest_path: dir.join("spill_123/manifest.json"),
            summary_path: dir.join("spill_123/capture_summary.txt"),
            requested_streams: 2,
            latest_observations: 2,
            aligned_latest_streams: 2,
            captured_streams: 2,
            warning_count: 0,
        };

        write_capture_index(&dir, &[result], 1, 2).expect("index should write");
        let index = fs::read_to_string(dir.join("capture_index.csv")).expect("index should read");
        assert!(index.contains("capture_index,target_ms,redis_timestamp_ms"));
        assert!(index.contains("0,123,123"));
        assert!(index.contains(",1,2"));

        let _ = fs::remove_dir_all(dir);
    }
}
