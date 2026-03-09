//! Live stream monitoring runtime for the TUI.
//!
//! Runtime model:
//! - One worker thread per device.
//! - Stream-native ingestion via `XREAD BLOCK` (no fixed polling loop).
//! - Worker reconnection with bounded exponential backoff.
//! - Periodic/device-event snapshots emitted through a channel for rendering.
//!
//! Design rationale:
//! Stream-driven reads reduce idle load while preserving low wake latency and ordering
//! semantics from Redis stream IDs. This keeps the monitor aligned with the same timing
//! substrate used later by analysis commands.

use std::cmp::Ordering;
use std::collections::{HashMap, VecDeque};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering as AtomicOrdering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread;
use std::time::{Duration, SystemTime};

use anyhow::{Result, anyhow};
use redis::streams::{StreamId, StreamInfoStreamReply, StreamReadOptions, StreamReadReply};
use redis::{Commands, Connection, RedisError};

use crate::config::{DeviceConfig, MonitorConfig, RedisConfig};

#[derive(Debug, Clone)]
pub struct StreamSnapshot {
    pub key: String,
    pub value_type: String,
    pub last_entry_id: Option<String>,
    pub payload_bytes: Option<usize>,
    pub entries_seen: u64,
    pub has_payload_field: bool,
}

#[derive(Debug, Clone)]
pub struct DeviceUpdate {
    pub device_label: String,
    pub bpm_ip: String,
    pub redis_addr: String,
    pub observed_at: SystemTime,
    pub last_event_id: Option<String>,
    pub recent_event_ids: Vec<String>,
    pub arrival_count: u64,
    pub checked_streams: usize,
    pub active_streams: usize,
    pub valid_streams: usize,
    pub next_reconnect_ms: Option<u64>,
    pub last_error: Option<String>,
    pub stream_states: Vec<StreamSnapshot>,
}

pub struct MonitorRuntime {
    pub updates: Receiver<DeviceUpdate>,
    stop: Arc<AtomicBool>,
    handles: Vec<thread::JoinHandle<()>>,
}

impl MonitorRuntime {
    pub fn start(config: &MonitorConfig) -> Self {
        let (tx, rx) = mpsc::channel();
        let stop = Arc::new(AtomicBool::new(false));

        let mut handles = Vec::with_capacity(config.devices.len());
        for device in config.devices.clone() {
            let tx = tx.clone();
            let stop_signal = Arc::clone(&stop);

            let opts = WorkerOptions {
                xread_block_ms: normalize_block_ms(config.xread_block_ms),
                reconnect_initial_ms: config.reconnect_initial_ms.max(250),
                reconnect_max_ms: config
                    .reconnect_max_ms
                    .max(config.reconnect_initial_ms.max(250)),
                min_stream_values: config.min_stream_values.max(1),
            };

            let handle = thread::spawn(move || run_device_worker(device, opts, tx, stop_signal));
            handles.push(handle);
        }

        Self {
            updates: rx,
            stop,
            handles,
        }
    }

    pub fn shutdown(self) {
        self.stop.store(true, AtomicOrdering::Relaxed);
        for handle in self.handles {
            let _ = handle.join();
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct WorkerOptions {
    xread_block_ms: u64,
    reconnect_initial_ms: u64,
    reconnect_max_ms: u64,
    min_stream_values: usize,
}

#[derive(Debug, Clone)]
struct LocalStreamState {
    key: String,
    value_type: String,
    read_id: String,
    last_entry_id: Option<String>,
    payload_bytes: Option<usize>,
    entries_seen: u64,
    has_payload_field: bool,
}

impl LocalStreamState {
    fn new(key: String) -> Self {
        Self {
            key,
            value_type: "unknown".to_string(),
            read_id: "$".to_string(),
            last_entry_id: None,
            payload_bytes: None,
            entries_seen: 0,
            has_payload_field: false,
        }
    }

    fn is_xread_eligible(&self) -> bool {
        matches!(self.value_type.as_str(), "stream" | "none" | "unknown")
    }

    fn as_snapshot(&self) -> StreamSnapshot {
        StreamSnapshot {
            key: self.key.clone(),
            value_type: self.value_type.clone(),
            last_entry_id: self.last_entry_id.clone(),
            payload_bytes: self.payload_bytes,
            entries_seen: self.entries_seen,
            has_payload_field: self.has_payload_field,
        }
    }
}

struct DeviceStreamState {
    streams: Vec<LocalStreamState>,
    index_by_key: HashMap<String, usize>,
}

impl DeviceStreamState {
    fn new(keys: &[String]) -> Self {
        let mut streams = Vec::with_capacity(keys.len());
        let mut index_by_key = HashMap::with_capacity(keys.len());

        for (idx, key) in keys.iter().enumerate() {
            streams.push(LocalStreamState::new(key.clone()));
            index_by_key.insert(key.clone(), idx);
        }

        Self {
            streams,
            index_by_key,
        }
    }

    fn get_mut(&mut self, key: &str) -> Option<&mut LocalStreamState> {
        let idx = self.index_by_key.get(key).copied()?;
        self.streams.get_mut(idx)
    }

    fn active_keys_and_ids(&self) -> (Vec<&str>, Vec<&str>) {
        let mut keys = Vec::new();
        let mut ids = Vec::new();

        for stream in &self.streams {
            if stream.is_xread_eligible() {
                keys.push(stream.key.as_str());
                ids.push(stream.read_id.as_str());
            }
        }

        (keys, ids)
    }

    fn active_stream_count(&self) -> usize {
        self.streams
            .iter()
            .filter(|stream| stream.is_xread_eligible())
            .count()
    }

    fn checked_stream_count(&self) -> usize {
        self.streams.len()
    }

    fn valid_stream_count(&self, min_stream_values: usize) -> usize {
        self.streams
            .iter()
            .filter(|stream| {
                stream.value_type == "stream"
                    && (stream.last_entry_id.is_some()
                        || stream.entries_seen >= min_stream_values as u64)
            })
            .count()
    }

    fn snapshots(&self) -> Vec<StreamSnapshot> {
        self.streams
            .iter()
            .map(LocalStreamState::as_snapshot)
            .collect()
    }

    fn latest_known_entry_id(&self) -> Option<String> {
        let mut latest: Option<String> = None;

        for stream in &self.streams {
            if let Some(id) = stream.last_entry_id.as_deref() {
                latest = pick_latest_id(latest, id);
            }
        }

        latest
    }
}

struct ReadOutcome {
    entries_read: usize,
    latest_entry_id: Option<String>,
    entry_ids: Vec<String>,
}

fn run_device_worker(
    device: DeviceConfig,
    opts: WorkerOptions,
    tx: Sender<DeviceUpdate>,
    stop: Arc<AtomicBool>,
) {
    let monitored_keys = collect_monitored_keys(&device);

    let mut connection: Option<Connection> = None;
    let mut streams = DeviceStreamState::new(&monitored_keys);
    let mut arrival_count: u64 = 0;
    let mut last_event_id: Option<String> = None;
    let mut recent_event_ids: VecDeque<String> = VecDeque::with_capacity(5);
    let mut reconnect_delay_ms = opts.reconnect_initial_ms;

    while !stop.load(AtomicOrdering::Relaxed) {
        if connection.is_none() {
            match connect_and_initialize(&device.redis, &mut streams) {
                Ok(conn) => {
                    connection = Some(conn);
                    reconnect_delay_ms = opts.reconnect_initial_ms;
                    last_event_id = streams.latest_known_entry_id().or(last_event_id);

                    let _ = tx.send(build_update(
                        &device,
                        &streams,
                        &last_event_id,
                        &recent_event_ids,
                        arrival_count,
                        opts.min_stream_values,
                        None,
                        None,
                    ));
                }
                Err(err) => {
                    let _ = tx.send(build_update(
                        &device,
                        &streams,
                        &last_event_id,
                        &recent_event_ids,
                        arrival_count,
                        opts.min_stream_values,
                        Some(err.to_string()),
                        Some(reconnect_delay_ms),
                    ));

                    sleep_interruptible(&stop, reconnect_delay_ms);
                    reconnect_delay_ms =
                        (reconnect_delay_ms.saturating_mul(2)).min(opts.reconnect_max_ms);
                    continue;
                }
            }
        }

        let conn = match connection.as_mut() {
            Some(conn) => conn,
            None => continue,
        };

        match read_next_stream_events(conn, &mut streams, opts.xread_block_ms) {
            Ok(outcome) => {
                if outcome.entries_read > 0 {
                    arrival_count = arrival_count.saturating_add(outcome.entries_read as u64);
                    if let Some(id) = outcome.latest_entry_id {
                        last_event_id = Some(id);
                    }
                    update_recent_ids(&mut recent_event_ids, &outcome.entry_ids);
                }

                let _ = tx.send(build_update(
                    &device,
                    &streams,
                    &last_event_id,
                    &recent_event_ids,
                    arrival_count,
                    opts.min_stream_values,
                    None,
                    None,
                ));
            }
            Err(err) => {
                if is_nonfatal_read_timeout(&err) {
                    let _ = tx.send(build_update(
                        &device,
                        &streams,
                        &last_event_id,
                        &recent_event_ids,
                        arrival_count,
                        opts.min_stream_values,
                        None,
                        None,
                    ));
                    continue;
                }

                connection = None;
                let _ = tx.send(build_update(
                    &device,
                    &streams,
                    &last_event_id,
                    &recent_event_ids,
                    arrival_count,
                    opts.min_stream_values,
                    Some(err.to_string()),
                    Some(reconnect_delay_ms),
                ));

                sleep_interruptible(&stop, reconnect_delay_ms);
                reconnect_delay_ms =
                    (reconnect_delay_ms.saturating_mul(2)).min(opts.reconnect_max_ms);
            }
        }
    }
}

fn is_nonfatal_read_timeout(err: &anyhow::Error) -> bool {
    err.downcast_ref::<RedisError>()
        .map(RedisError::is_timeout)
        .unwrap_or(false)
}

fn connect_and_initialize(
    redis_cfg: &RedisConfig,
    streams: &mut DeviceStreamState,
) -> Result<Connection> {
    let client = redis::Client::open(redis_cfg.to_url())?;
    let mut conn = client.get_connection()?;

    for stream in &mut streams.streams {
        let value_type: String = redis::cmd("TYPE").arg(&stream.key).query(&mut conn)?;
        stream.value_type = value_type.clone();

        if value_type == "stream" {
            if let Ok(info) = redis::cmd("XINFO")
                .arg("STREAM")
                .arg(&stream.key)
                .query::<StreamInfoStreamReply>(&mut conn)
            {
                if !info.last_generated_id.is_empty() {
                    stream.read_id = info.last_generated_id;
                }
                if !info.last_entry.id.is_empty() {
                    stream.last_entry_id = Some(info.last_entry.id);
                }
            }
        } else if value_type == "none" {
            stream.read_id = "$".to_string();
        }
    }

    if streams.active_stream_count() == 0 {
        return Err(anyhow!(
            "no stream keys are eligible for XREAD after TYPE checks"
        ));
    }

    Ok(conn)
}

fn read_next_stream_events(
    conn: &mut Connection,
    streams: &mut DeviceStreamState,
    block_ms: u64,
) -> Result<ReadOutcome> {
    let (keys, ids) = streams.active_keys_and_ids();
    if keys.is_empty() {
        return Err(anyhow!("no stream keys are eligible for XREAD"));
    }

    let options = StreamReadOptions::default()
        .block(normalize_block_ms(block_ms) as usize)
        .count(64);

    let reply: StreamReadReply = conn.xread_options(&keys, &ids, &options)?;

    let mut entries_read = 0usize;
    let mut latest_id: Option<String> = None;
    let mut entry_ids = Vec::new();

    for key in reply.keys {
        for id in key.ids {
            entries_read = entries_read.saturating_add(1);
            latest_id = pick_latest_id(latest_id, &id.id);
            entry_ids.push(id.id.clone());

            if let Some(stream) = streams.get_mut(&key.key) {
                apply_stream_entry(stream, &id);
            }
        }
    }

    Ok(ReadOutcome {
        entries_read,
        latest_entry_id: latest_id,
        entry_ids,
    })
}

fn apply_stream_entry(stream: &mut LocalStreamState, id: &StreamId) {
    stream.value_type = "stream".to_string();
    stream.read_id = id.id.clone();
    stream.last_entry_id = Some(id.id.clone());
    stream.entries_seen = stream.entries_seen.saturating_add(1);

    if let Some(payload) = id.get::<Vec<u8>>("_") {
        stream.payload_bytes = Some(payload.len());
        stream.has_payload_field = true;
    } else {
        stream.payload_bytes = None;
        stream.has_payload_field = false;
    }
}

fn build_update(
    device: &DeviceConfig,
    streams: &DeviceStreamState,
    last_event_id: &Option<String>,
    recent_event_ids: &VecDeque<String>,
    arrival_count: u64,
    min_stream_values: usize,
    last_error: Option<String>,
    next_reconnect_ms: Option<u64>,
) -> DeviceUpdate {
    DeviceUpdate {
        device_label: device.label.clone(),
        bpm_ip: device.bpm_ip.clone(),
        redis_addr: device.redis.display_addr(),
        observed_at: SystemTime::now(),
        last_event_id: last_event_id.clone(),
        recent_event_ids: recent_event_ids.iter().cloned().collect(),
        arrival_count,
        checked_streams: streams.checked_stream_count(),
        active_streams: streams.active_stream_count(),
        valid_streams: streams.valid_stream_count(min_stream_values),
        next_reconnect_ms,
        last_error,
        stream_states: streams.snapshots(),
    }
}

fn update_recent_ids(history: &mut VecDeque<String>, new_ids: &[String]) {
    for id in new_ids {
        if history.back() == Some(id) {
            continue;
        }
        if history.len() == 5 {
            let _ = history.pop_front();
        }
        history.push_back(id.clone());
    }
}

fn collect_monitored_keys(device: &DeviceConfig) -> Vec<String> {
    let mut keys = Vec::new();
    let mut seen: HashMap<String, bool> = HashMap::new();

    for key in std::iter::once(&device.trigger_key)
        .chain(device.trigger_fallback_keys.iter())
        .chain(device.stream_keys.iter())
    {
        if !key.trim().is_empty() && !seen.contains_key(key) {
            seen.insert(key.clone(), true);
            keys.push(key.clone());
        }
    }

    keys
}

fn sleep_interruptible(stop: &AtomicBool, total_ms: u64) {
    let sleep_ms = total_ms.max(50);
    let mut slept = 0u64;

    while slept < sleep_ms && !stop.load(AtomicOrdering::Relaxed) {
        thread::sleep(Duration::from_millis(100));
        slept += 100;
    }
}

fn pick_latest_id(current: Option<String>, candidate: &str) -> Option<String> {
    match current {
        Some(existing) => {
            if compare_stream_ids(candidate, &existing) == Ordering::Greater {
                Some(candidate.to_string())
            } else {
                Some(existing)
            }
        }
        None => Some(candidate.to_string()),
    }
}

fn compare_stream_ids(a: &str, b: &str) -> Ordering {
    match (parse_stream_id(a), parse_stream_id(b)) {
        (Some(a_parts), Some(b_parts)) => a_parts.cmp(&b_parts),
        _ => a.cmp(b),
    }
}

fn parse_stream_id(id: &str) -> Option<(u64, u64)> {
    let (ms, sub_ms) = id.split_once('-')?;
    let ms = ms.parse::<u64>().ok()?;
    let sub_ms = sub_ms.parse::<u64>().ok()?;
    Some((ms, sub_ms))
}

fn normalize_block_ms(v: u64) -> u64 {
    if v == 0 { 0 } else { v.max(50) }
}
