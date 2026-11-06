//! Headless acquisition, durable settings, bounded scheduling, and publication.
//!
//! Raw Redis readers never write to the node Redis instances. Every position
//! payload retains its own stream ID; intensity is paired only by exact ID.

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, BufWriter, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, anyhow, bail, ensure};
use redis::Commands;
use redis::streams::{StreamReadOptions, StreamReadReply};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::analysis;
use super::redis_io::{self, PREFIX, Publisher, StreamEntry, now_ms};
use super::{
    AcceptedSpec, MeasurementSpec, PlaneResult, SourceProvenance, SpecReceipt, SpecRequest,
    SpillInput, TraceInput, TuneResult,
};

const MAX_SAMPLES: usize = 1_000_000;
const MAX_TOTAL_SAMPLES: usize = 128_000_000;
const MAX_CACHE_BYTES: usize = 512 * 1024 * 1024;
const SOURCE_TIMEOUT: Duration = Duration::from_millis(500);
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(1);

fn software_version() -> String {
    format!(
        "{}+{}",
        env!("CARGO_PKG_VERSION"),
        option_env!("TBT_GIT_REV").unwrap_or("unknown")
    )
}

fn default_port() -> u16 {
    6379
}
fn default_version() -> u32 {
    1
}
fn default_tolerance() -> u64 {
    25
}
fn default_settle() -> u64 {
    150
}
fn default_max_settle() -> u64 {
    1000
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Source {
    pub id: String,
    pub plane: String,
    pub digitizer: String,
    pub ring_order: f64,
    pub host: String,
    #[serde(default = "default_port")]
    pub port: u16,
    #[serde(default)]
    pub db: u32,
    pub position_key: String,
    pub intensity_key: String,
    pub beam_threshold: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TestModeSource {
    pub host: String,
    #[serde(default = "default_port")]
    pub port: u16,
    #[serde(default)]
    pub db: u32,
    pub key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceConfig {
    #[serde(default = "default_version")]
    pub version: u32,
    pub sources: Vec<Source>,
    pub test_mode: Option<TestModeSource>,
    #[serde(default = "default_tolerance")]
    pub source_tolerance_ms: u64,
    #[serde(default = "default_settle")]
    pub settle_ms: u64,
    #[serde(default = "default_max_settle")]
    pub max_settle_ms: u64,
}

impl SourceConfig {
    pub fn load(path: &Path) -> Result<Self> {
        let value: Value = read_json(path)?;
        let config = if value.is_array() {
            Self {
                version: 1,
                sources: serde_json::from_value(value)?,
                test_mode: None,
                source_tolerance_ms: 25,
                settle_ms: 150,
                max_settle_ms: 1000,
            }
        } else {
            serde_json::from_value(value)?
        };
        config.validate()?;
        Ok(config)
    }

    fn validate(&self) -> Result<()> {
        ensure!(
            self.version == 1,
            "unsupported source configuration version"
        );
        ensure!(
            !self.sources.is_empty() && self.sources.len() <= 512,
            "source inventory must contain 1..512 entries"
        );
        ensure!(
            self.source_tolerance_ms == 25,
            "version 1 source tolerance must be 25 ms"
        );
        ensure!(
            self.settle_ms >= 25
                && self.max_settle_ms >= self.settle_ms
                && self.max_settle_ms <= 10000,
            "settle must be 25..max_settle_ms and max_settle_ms <= 10000"
        );
        let mut ids = HashSet::new();
        let mut streams = HashSet::new();
        for source in &self.sources {
            ensure!(
                ids.insert(source.id.clone()),
                "duplicate source identity {}",
                source.id
            );
            ensure!(
                matches!(source.plane.as_str(), "H" | "V"),
                "source {} plane must be H or V",
                source.id
            );
            ensure!(
                source.ring_order.is_finite() && source.beam_threshold.is_finite(),
                "source {} has nonfinite metadata",
                source.id
            );
            validate_host(&source.host)?;
            ensure!(
                !source.position_key.is_empty() && !source.intensity_key.is_empty(),
                "source keys must be nonempty"
            );
            ensure!(
                source.position_key != source.intensity_key,
                "position and intensity must have different keys"
            );
            ensure!(
                streams.insert((source.url(), source.position_key.clone())),
                "duplicate position source {}",
                source.position_key
            );
        }
        if let Some(mode) = &self.test_mode {
            validate_host(&mode.host)?;
            ensure!(!mode.key.is_empty(), "test mode key is empty");
        }
        Ok(())
    }
}

fn validate_host(host: &str) -> Result<()> {
    ensure!(
        !host.is_empty()
            && !host.contains(['/', '@', '?', '#', '\\'])
            && !host.chars().any(char::is_whitespace),
        "invalid Redis host"
    );
    Ok(())
}

fn redis_url(host: &str, port: u16, db: u32) -> String {
    let host = if host.contains(':') && !host.starts_with('[') {
        format!("[{host}]")
    } else {
        host.to_owned()
    };
    format!("redis://{host}:{port}/{db}")
}

impl Source {
    fn url(&self) -> String {
        redis_url(&self.host, self.port, self.db)
    }
    fn cache_key(&self, key: &str) -> String {
        format!("{}|{key}", self.url())
    }
}

pub struct ServeOptions {
    pub sources: PathBuf,
    pub state_dir: PathBuf,
    pub redis_url: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct RequestRecord {
    request: SpecRequest,
    receipt: SpecReceipt,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SettingsState {
    #[serde(flatten)]
    accepted: AcceptedSpec,
    #[serde(default)]
    requests: VecDeque<RequestRecord>,
}

impl SettingsState {
    fn load_or_create(state_dir: &Path) -> Result<Self> {
        let path = state_dir.join("accepted-spec.json");
        if path.exists() {
            let state: Self = read_json(&path)?;
            state.accepted.spec.validate()?;
            Ok(state)
        } else {
            let state = Self {
                accepted: AcceptedSpec {
                    revision: 0,
                    spec: MeasurementSpec::default(),
                },
                requests: VecDeque::new(),
            };
            state.accepted.spec.validate()?;
            atomic_json(&path, &state)?;
            Ok(state)
        }
    }

    /// A successful receipt is created only after durable validated persistence.
    #[cfg(test)]
    fn apply(&mut self, bytes: &[u8], state_dir: &Path) -> SpecReceipt {
        self.apply_with_capacity(
            bytes,
            state_dir,
            &[("H".into(), MAX_SAMPLES, 60), ("V".into(), MAX_SAMPLES, 60)],
        )
    }

    fn apply_with_capacity(
        &mut self,
        bytes: &[u8],
        state_dir: &Path,
        capacity: &[(String, usize, usize)],
    ) -> SpecReceipt {
        let request: SpecRequest = match serde_json::from_slice(bytes) {
            Ok(v) => v,
            Err(e) => {
                return SpecReceipt {
                    request_id: serde_json::from_slice::<Value>(bytes)
                        .ok()
                        .and_then(|v| {
                            v.get("request_id")
                                .and_then(Value::as_str)
                                .map(str::to_owned)
                        })
                        .unwrap_or_default(),
                    accepted: false,
                    revision: self.accepted.revision,
                    message: format!("invalid request: {e}"),
                };
            }
        };
        let reject = |message: String, revision| SpecReceipt {
            request_id: request.request_id.clone(),
            accepted: false,
            revision,
            message,
        };
        if request.request_id.is_empty()
            || request.request_id.len() > 128
            || request.request_id.chars().any(char::is_control)
        {
            return reject(
                "request_id must be 1..128 printable characters".into(),
                self.accepted.revision,
            );
        }
        if let Some(previous) = self
            .requests
            .iter()
            .find(|r| r.request.request_id == request.request_id)
        {
            return if previous.request.expected_revision == request.expected_revision
                && previous.request.spec == request.spec
            {
                previous.receipt.clone()
            } else {
                reject(
                    "request_id was already used for a different request".into(),
                    self.accepted.revision,
                )
            };
        }
        if request.expected_revision != self.accepted.revision {
            return reject(
                format!(
                    "revision conflict: expected {}, current {}",
                    request.expected_revision, self.accepted.revision
                ),
                self.accepted.revision,
            );
        }
        if let Err(error) = request.spec.validate() {
            return reject(
                format!("invalid specification: {error:#}"),
                self.accepted.revision,
            );
        }
        for (plane, turns, channels) in capacity {
            if let Err(error) = request.spec.validate_execution(plane, *turns, *channels) {
                return reject(
                    format!("specification exceeds execution budget: {error:#}"),
                    self.accepted.revision,
                );
            }
        }
        let Some(revision) = self.accepted.revision.checked_add(1) else {
            return reject(
                "configuration revision exhausted".into(),
                self.accepted.revision,
            );
        };
        let receipt = SpecReceipt {
            request_id: request.request_id.clone(),
            accepted: true,
            revision,
            message: "accepted and persisted".into(),
        };
        let mut next = self.clone();
        next.accepted = AcceptedSpec {
            revision,
            spec: request.spec.clone(),
        };
        next.requests.push_back(RequestRecord {
            request,
            receipt: receipt.clone(),
        });
        while next.requests.len() > 128 {
            next.requests.pop_front();
        }
        match atomic_json(&state_dir.join("accepted-spec.json"), &next) {
            Ok(()) => {
                *self = next;
                receipt
            }
            Err(error) => SpecReceipt {
                request_id: receipt.request_id,
                accepted: false,
                revision: self.accepted.revision,
                message: format!("persistence failed; settings unchanged: {error:#}"),
            },
        }
    }
}

fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T> {
    let file = File::open(path).with_context(|| format!("read {}", path.display()))?;
    serde_json::from_reader(BufReader::new(file))
        .with_context(|| format!("decode {}", path.display()))
}

fn atomic_json<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_nanos();
    let temp = parent.join(format!(
        ".{}.{}.{nonce}.tmp",
        path.file_name().unwrap_or_default().to_string_lossy(),
        std::process::id()
    ));
    let result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp)?;
        {
            let mut writer = BufWriter::new(&mut file);
            serde_json::to_writer(&mut writer, value)?;
            writer.write_all(b"\n")?;
            writer.flush()?;
        }
        file.sync_all()?;
        fs::rename(&temp, path)?;
        File::open(parent)?.sync_all()?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result.with_context(|| format!("persist {}", path.display()))
}

#[derive(Clone)]
struct RawEntry {
    id: String,
    ms: u64,
    bytes: Arc<Vec<u8>>,
}

#[derive(Default)]
struct AcquisitionCache {
    entries: HashMap<String, VecDeque<RawEntry>>,
    errors: BTreeMap<String, String>,
    bytes: usize,
}

#[derive(Default)]
struct SourceMailbox {
    newest: Option<SpillInput>,
    superseded: u64,
    error: Option<String>,
}

fn cache_entry(
    cache: &mut AcquisitionCache,
    url: &str,
    key: &str,
    entry: StreamEntry,
) -> Result<()> {
    cache_entry_with_budget(cache, url, key, entry, MAX_CACHE_BYTES)
}

fn cache_entry_with_budget(
    cache: &mut AcquisitionCache,
    url: &str,
    key: &str,
    entry: StreamEntry,
    budget: usize,
) -> Result<()> {
    let (ms, _) = redis_io::stream_id(&entry.0)?;
    let bytes = redis_io::payload(&entry.1)?;
    ensure!(
        bytes.len() % 4 == 0 && bytes.len() <= MAX_SAMPLES * 4,
        "{} payload is not a bounded float32 array",
        key
    );
    let cache_key = format!("{url}|{key}");
    if cache
        .entries
        .get(&cache_key)
        .and_then(|slots| slots.back())
        .is_some_and(|prior| prior.id == entry.0)
    {
        return Ok(());
    }
    while cache.bytes + bytes.len() > budget {
        // Retain every stream's newest payload, but evict historical spare slots
        // before rejecting a large acquisition. This cannot strand new streams.
        let oldest = cache
            .entries
            .iter()
            .filter(|(_, slots)| slots.len() > 1)
            .min_by_key(|(_, slots)| slots.front().unwrap().ms)
            .map(|(key, _)| key.clone());
        let Some(oldest) = oldest.or_else(|| {
            cache
                .entries
                .get(&cache_key)
                .filter(|slots| !slots.is_empty())
                .map(|_| cache_key.clone())
        }) else {
            bail!("raw cache exceeded its {budget} byte budget; capture rejected");
        };
        cache.bytes -= cache
            .entries
            .get_mut(&oldest)
            .unwrap()
            .pop_front()
            .unwrap()
            .bytes
            .len();
    }
    let slots = cache.entries.entry(cache_key).or_default();
    let owned_bytes = entry
        .1
        .into_iter()
        .find(|(name, _)| name == b"_")
        .unwrap()
        .1;
    slots.push_back(RawEntry {
        id: entry.0,
        ms,
        bytes: Arc::new(owned_bytes),
    });
    cache.bytes += slots.back().unwrap().bytes.len();
    while slots.len() > 2 {
        cache.bytes -= slots.pop_front().unwrap().bytes.len();
    }
    Ok(())
}

fn source_reader(url: String, keys: Vec<String>, shared: Arc<Mutex<AcquisitionCache>>) {
    loop {
        let outcome = (|| -> Result<()> {
            let mut conn = redis_io::connect(&url, SOURCE_TIMEOUT)?;
            let mut ids = Vec::with_capacity(keys.len());
            for key in &keys {
                if let Some(entry) = redis_io::latest(&mut conn, key)? {
                    ids.push(entry.0.clone());
                    cache_entry(&mut shared.lock().unwrap(), &url, key, entry)?;
                } else {
                    ids.push("0-0".into());
                }
            }
            shared.lock().unwrap().errors.remove(&url);
            loop {
                let reply: StreamReadReply = conn.xread_options(
                    &keys,
                    &ids,
                    &StreamReadOptions::default().block(100).count(1),
                )?;
                let mut cache = shared.lock().unwrap();
                for stream in reply.keys {
                    let Some(index) = keys.iter().position(|key| key == &stream.key) else {
                        continue;
                    };
                    for mut entry in stream.ids {
                        ids[index] = entry.id.clone();
                        let value = entry
                            .map
                            .remove("_")
                            .ok_or_else(|| anyhow!("{} missing '_' field", stream.key))?;
                        let bytes = match value {
                            redis::Value::BulkString(bytes) => bytes,
                            other => redis::from_redis_value(&other)?,
                        };
                        cache_entry(
                            &mut cache,
                            &url,
                            &stream.key,
                            (entry.id, vec![(b"_".to_vec(), bytes)]),
                        )?;
                    }
                }
            }
        })();
        if let Err(error) = outcome {
            shared
                .lock()
                .unwrap()
                .errors
                .insert(url.clone(), format!("{error:#}"));
        }
        thread::sleep(Duration::from_secs(1));
    }
}

fn decode_samples(bytes: &[u8]) -> Result<Vec<f32>> {
    ensure!(
        bytes.len() % 4 == 0 && bytes.len() <= MAX_SAMPLES * 4,
        "invalid float32 payload length {}",
        bytes.len()
    );
    Ok(bytes
        .chunks_exact(4)
        .map(|v| f32::from_le_bytes(v.try_into().unwrap()))
        .collect())
}

fn read_test_mode(
    source: Option<&TestModeSource>,
    first_id: &str,
    last_id: &str,
) -> Result<(bool, Vec<String>)> {
    let source = source.ok_or_else(|| anyhow!("no TEST_MODE_R source configured"))?;
    let mut conn = redis_io::connect(
        &redis_url(&source.host, source.port, source.db),
        SOURCE_TIMEOUT,
    )?;
    let baseline: redis_io::StreamEntries = redis::cmd("XREVRANGE")
        .arg(&source.key)
        .arg(first_id)
        .arg("-")
        .arg("COUNT")
        .arg(1)
        .query(&mut conn)?;
    let (id, fields) = baseline
        .into_iter()
        .next()
        .ok_or_else(|| anyhow!("TEST_MODE_R has no retained state at source acquisition time"))?;
    let mut synthetic = decode_test_mode(redis_io::payload(&fields)?)?;
    let transitions: redis_io::StreamEntries = redis::cmd("XRANGE")
        .arg(&source.key)
        .arg(format!("({id}"))
        .arg(last_id)
        .arg("COUNT")
        .arg(65)
        .query(&mut conn)?;
    ensure!(
        transitions.len() <= 64,
        "too many TEST_MODE_R transitions within acquisition"
    );
    let mut provenance = vec![format!(
        "synthetic control {} at source acquisition: {id}",
        source.key
    )];
    for (id, fields) in transitions {
        synthetic |= decode_test_mode(redis_io::payload(&fields)?)?;
        provenance.push(format!(
            "synthetic control transition within acquisition: {id}"
        ));
    }
    Ok((synthetic, provenance))
}

fn decode_test_mode(bytes: &[u8]) -> Result<bool> {
    match bytes.len() {
        1 => Ok(bytes[0] != 0),
        2 => Ok(u16::from_le_bytes(bytes.try_into().unwrap()) != 0),
        4 => Ok(u32::from_le_bytes(bytes.try_into().unwrap()) != 0),
        8 => Ok(u64::from_le_bytes(bytes.try_into().unwrap()) != 0),
        _ => bail!("TEST_MODE_R is not a binary integer"),
    }
}

fn acquisition_signature(target: u64, pairs: &[(Source, RawEntry, Option<RawEntry>)]) -> String {
    let mut hash = 0xcbf29ce484222325_u64;
    for (source, entry, _) in pairs {
        for byte in source
            .id
            .bytes()
            .chain([0])
            .chain(entry.id.bytes())
            .chain([0])
        {
            hash = (hash ^ u64::from(byte)).wrapping_mul(0x100000001b3);
        }
    }
    format!("{target}-{hash:016x}")
}

fn content_signature(acquisition: &str, pairs: &[(Source, RawEntry, Option<RawEntry>)]) -> String {
    let mut hash = 0xcbf29ce484222325_u64;
    for (_, _, intensity) in pairs {
        for byte in intensity
            .as_ref()
            .map_or("missing", |entry| entry.id.as_str())
            .bytes()
            .chain([0])
        {
            hash = (hash ^ u64::from(byte)).wrapping_mul(0x100000001b3);
        }
    }
    format!("{acquisition}:{hash:016x}")
}

fn same_input_content(left: &SpillInput, right: &SpillInput) -> bool {
    left.acquisition_id == right.acquisition_id
        && left.synthetic == right.synthetic
        && left.warnings == right.warnings
        && left.traces.len() == right.traces.len()
        && left.traces.iter().zip(&right.traces).all(|(a, b)| {
            a.id == b.id
                && a.source_stream_id == b.source_stream_id
                && a.samples.len() == b.samples.len()
                && a.intensity_stream_id == b.intensity_stream_id
                && a.intensity.as_ref().map(Vec::len) == b.intensity.as_ref().map(Vec::len)
        })
}

fn select_coherent(
    config: &SourceConfig,
    cache: &AcquisitionCache,
) -> Option<(u64, Vec<(Source, RawEntry, Option<RawEntry>)>)> {
    let target = config
        .sources
        .iter()
        .filter_map(|source| {
            cache
                .entries
                .get(&source.cache_key(&source.position_key))?
                .back()
                .map(|e| e.ms)
        })
        .max()?;
    let low = target.saturating_sub(config.source_tolerance_ms);
    let pairs = config
        .sources
        .iter()
        .filter_map(|source| {
            let position = cache
                .entries
                .get(&source.cache_key(&source.position_key))?
                .iter()
                .rev()
                .find(|entry| (low..=target).contains(&entry.ms))?
                .clone();
            let intensity = cache
                .entries
                .get(&source.cache_key(&source.intensity_key))
                .and_then(|entries| entries.iter().find(|entry| entry.id == position.id))
                .cloned();
            Some((source.clone(), position, intensity))
        })
        .collect();
    Some((target, pairs))
}

fn acquisition_loop(config: SourceConfig, mailbox: Arc<Mutex<SourceMailbox>>) {
    let cache = Arc::new(Mutex::new(AcquisitionCache::default()));
    let mut endpoints: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for source in &config.sources {
        let keys = endpoints.entry(source.url()).or_default();
        for key in [&source.position_key, &source.intensity_key] {
            if !keys.contains(key) {
                keys.push(key.clone());
            }
        }
    }
    for (url, keys) in endpoints {
        let shared = Arc::clone(&cache);
        thread::spawn(move || source_reader(url, keys, shared));
    }
    let mut last_completed = String::new();
    let mut mode_unqualified = false;
    let mut mode_retry_at = Instant::now();
    let mut settling: Option<(u64, String, Instant, Instant)> = None;
    loop {
        thread::sleep(Duration::from_millis(25));
        let (selected, errors) = {
            let cache = cache.lock().unwrap();
            (select_coherent(&config, &cache), cache.errors.clone())
        };
        if !errors.is_empty() {
            mailbox.lock().unwrap().error = Some(format!(
                "raw source unavailable: {}",
                errors
                    .iter()
                    .map(|(url, error)| format!("{url}: {error}"))
                    .collect::<Vec<_>>()
                    .join("; ")
            ));
        }
        let Some((target, pairs)) = selected else {
            continue;
        };
        let acquisition = acquisition_signature(target, &pairs);
        let signature = content_signature(&acquisition, &pairs);
        let first_id = pairs
            .iter()
            .map(|(_, entry, _)| entry.id.as_str())
            .min_by_key(|id| redis_io::stream_id(id).unwrap())
            .unwrap();
        let last_id = pairs
            .iter()
            .map(|(_, entry, _)| entry.id.as_str())
            .max_by_key(|id| redis_io::stream_id(id).unwrap())
            .unwrap();
        if signature == last_completed {
            if !mode_unqualified || Instant::now() < mode_retry_at {
                continue;
            }
            mode_retry_at = Instant::now() + Duration::from_secs(1);
            if read_test_mode(config.test_mode.as_ref(), first_id, last_id).is_err() {
                continue;
            }
        }
        let now = Instant::now();
        match settling.as_mut() {
            Some((prior_target, prior_signature, started, changed))
                if target.abs_diff(*prior_target) <= config.source_tolerance_ms =>
            {
                if *prior_signature != signature {
                    *prior_signature = signature.clone();
                    *changed = now;
                }
                *prior_target = target;
                let complete = pairs.len() == config.sources.len()
                    && pairs.iter().all(|(_, _, intensity)| intensity.is_some());
                if !(complete && changed.elapsed() >= Duration::from_millis(config.settle_ms))
                    && started.elapsed() < Duration::from_millis(config.max_settle_ms)
                {
                    continue;
                }
            }
            _ => {
                if settling.is_some() {
                    mailbox.lock().unwrap().superseded += 1;
                }
                settling = Some((target, signature.clone(), now, now));
                continue;
            }
        }
        let captured = (|| -> Result<SpillInput> {
            let mut warnings = Vec::new();
            if pairs.len() != config.sources.len() {
                let present: HashSet<_> = pairs
                    .iter()
                    .map(|(source, _, _)| source.id.as_str())
                    .collect();
                warnings.push(format!(
                    "source settle deadline reached; missing aligned positions: {}",
                    config
                        .sources
                        .iter()
                        .filter(|s| !present.contains(s.id.as_str()))
                        .map(|s| s.id.as_str())
                        .collect::<Vec<_>>()
                        .join(",")
                ));
            }
            for (url, error) in &errors {
                warnings.push(format!("raw source {url}: {error}"));
            }
            let sample_count: usize = pairs
                .iter()
                .map(|(_, position, intensity)| {
                    position.bytes.len() / 4
                        + intensity.as_ref().map_or(0, |entry| entry.bytes.len() / 4)
                })
                .sum();
            ensure!(
                sample_count <= MAX_TOTAL_SAMPLES,
                "spill exceeds total sample limit before decoding"
            );
            let synthetic = match read_test_mode(config.test_mode.as_ref(), first_id, last_id) {
                Ok((value, provenance)) => {
                    mode_unqualified = false;
                    warnings.extend(provenance);
                    value
                }
                Err(error) => {
                    mode_unqualified = true;
                    mode_retry_at = Instant::now() + Duration::from_secs(1);
                    warnings.push(format!(
                        "synthetic status unknown; display suppressed: {error:#}"
                    ));
                    true
                }
            };
            let mut traces = Vec::new();
            for (source, position, intensity) in &pairs {
                if intensity.is_none() {
                    warnings.push(format!(
                        "{} has no intensity entry with exact position ID {}",
                        source.id, position.id
                    ));
                }
                traces.push(TraceInput {
                    id: source.id.clone(),
                    plane: source.plane.clone(),
                    digitizer: source.digitizer.clone(),
                    ring_order: source.ring_order,
                    source_key: source.position_key.clone(),
                    source_stream_id: position.id.clone(),
                    intensity_stream_id: intensity.as_ref().map(|v| v.id.clone()),
                    beam_threshold: source.beam_threshold,
                    samples: decode_samples(&position.bytes)?,
                    intensity: intensity
                        .as_ref()
                        .map(|entry| decode_samples(&entry.bytes))
                        .transpose()?,
                });
            }
            let input = SpillInput {
                schema_version: 1,
                acquisition_id: acquisition.clone(),
                source_time_ms: target,
                captured_at_ms: now_ms(),
                synthetic,
                traces,
                warnings,
            };
            validate_input(&input)?;
            Ok(input)
        })();
        let mut slot = mailbox.lock().unwrap();
        match captured {
            Ok(input) => {
                if slot.newest.replace(input).is_some() {
                    slot.superseded += 1;
                }
                slot.error = None;
            }
            Err(error) => {
                slot.error = Some(format!("capture failed: {error:#}"));
            }
        }
        last_completed = signature;
        settling = None;
    }
}

fn validate_input(input: &SpillInput) -> Result<()> {
    ensure!(input.schema_version == 1, "unsupported spill input schema");
    ensure!(
        !input.acquisition_id.is_empty(),
        "empty acquisition identity"
    );
    ensure!(input.traces.len() <= 512, "too many traces");
    let mut ids = HashSet::new();
    let mut total = 0usize;
    for trace in &input.traces {
        ensure!(ids.insert(&trace.id), "duplicate trace {}", trace.id);
        ensure!(
            matches!(trace.plane.as_str(), "H" | "V"),
            "invalid trace plane"
        );
        ensure!(
            trace.ring_order.is_finite() && trace.beam_threshold.is_finite(),
            "nonfinite trace metadata"
        );
        let (source_ms, _) = redis_io::stream_id(&trace.source_stream_id)?;
        ensure!(
            source_ms.abs_diff(input.source_time_ms) <= 25,
            "{} position is outside the 25 ms acquisition group",
            trace.id
        );
        ensure!(
            trace.samples.len() <= MAX_SAMPLES
                && trace
                    .intensity
                    .as_ref()
                    .is_none_or(|v| v.len() <= MAX_SAMPLES),
            "trace exceeds sample limit"
        );
        // A mismatched pair is retained as provenance, but never offered to analysis.
        ensure!(
            trace.intensity.is_none()
                || trace.intensity_stream_id.as_ref() == Some(&trace.source_stream_id),
            "{} intensity ID does not match position ID",
            trace.id
        );
        total += trace.samples.len() + trace.intensity.as_ref().map_or(0, Vec::len);
    }
    ensure!(
        total <= MAX_TOTAL_SAMPLES,
        "spill exceeds total sample limit"
    );
    Ok(())
}

struct Job {
    input: Arc<SpillInput>,
    accepted: AcceptedSpec,
    generation: u64,
    kind: String,
    superseded: u64,
}
struct Completed {
    input: Arc<SpillInput>,
    result: TuneResult,
    persistence_error: Option<String>,
}

fn compute(job: Job) -> Completed {
    let start = Instant::now();
    let mut warnings = job.input.warnings.clone();
    let planes = match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        analysis::analyze(&job.input, &job.accepted.spec)
    })) {
        Ok(planes) => planes,
        Err(_) => {
            warnings.push("analysis worker panicked; result unavailable".into());
            unavailable_planes("analysis worker panicked")
        }
    };
    let sources = job
        .input
        .traces
        .iter()
        .map(|trace| SourceProvenance {
            id: trace.id.clone(),
            source_key: trace.source_key.clone(),
            source_stream_id: trace.source_stream_id.clone(),
            intensity_stream_id: trace.intensity_stream_id.clone(),
            sample_count: trace.samples.len(),
            intensity_sample_count: trace.intensity.as_ref().map(Vec::len),
        })
        .collect();
    Completed {
        input: Arc::clone(&job.input),
        result: TuneResult {
            schema_version: 1,
            algorithm: "paper-best-n-v1".into(),
            software_version: software_version(),
            generation: job.generation,
            acquisition_id: job.input.acquisition_id.clone(),
            source_time_ms: job.input.source_time_ms,
            processed_at_ms: now_ms(),
            configuration_revision: job.accepted.revision,
            result_kind: job.kind,
            synthetic: job.input.synthetic,
            processing_ms: start.elapsed().as_millis().min(u64::MAX as u128) as u64,
            superseded_acquisitions: job.superseded,
            effective_spec: job.accepted.spec,
            sources,
            planes,
            warnings,
        },
        persistence_error: None,
    }
}

fn unavailable_planes(reason: &str) -> BTreeMap<String, PlaneResult> {
    ["H", "V"]
        .into_iter()
        .map(|plane| {
            (
                plane.into(),
                PlaneResult {
                    state: "unavailable".into(),
                    reasons: vec![reason.into()],
                    ..Default::default()
                },
            )
        })
        .collect()
}

fn status_code(plane: Option<&PlaneResult>, synthetic: bool, stale: bool, enabled: bool) -> u32 {
    if !enabled {
        6
    } else if stale {
        5
    } else if synthetic {
        4
    } else {
        match plane {
            Some(p) if p.state == "valid" && p.q.is_some_and(f64::is_finite) => 1,
            Some(p) if p.reasons.iter().any(|r| r.contains("beam")) => 2,
            Some(p) if p.state == "suppressed" => 3,
            _ => 0,
        }
    }
}

fn result_values(
    result: &TuneResult,
    accepted: &AcceptedSpec,
    busy: bool,
    error: bool,
    include_result: bool,
) -> Result<Vec<(&'static str, Vec<u8>)>> {
    let now = now_ms();
    let no_source = result.source_time_ms == 0;
    let age = if no_source {
        u64::MAX
    } else {
        now.saturating_sub(result.source_time_ms)
    };
    let stale = age > accepted.spec.quality.stale_after_ms;
    let future_source = result.source_time_ms > now.saturating_add(25);
    let state: u32 = if error || future_source {
        4
    } else if busy {
        2
    } else if no_source {
        5
    } else if stale {
        3
    } else {
        1
    };
    let mut values = vec![
        ("SOURCE_AGE_MS_R", age.to_le_bytes().to_vec()),
        ("STATE_R", state.to_le_bytes().to_vec()),
    ];
    if include_result {
        values.insert(0, ("RESULT_R", serde_json::to_vec(result)?));
    }
    for (name, q_key, status_key) in [("H", "H:Q_R", "H:STATUS_R"), ("V", "V:Q_R", "V:STATUS_R")] {
        let plane = result.planes.get(name);
        let enabled = accepted.spec.planes.get(name).is_some_and(|p| p.enabled);
        let synthetic = result.synthetic && accepted.spec.quality.suppress_synthetic;
        let status = if error
            || future_source
            || no_source
            || result.configuration_revision != accepted.revision
        {
            0
        } else {
            status_code(plane, synthetic, stale, enabled)
        };
        // Old-configuration scalars are never presented as the accepted settings.
        let q = if !error && status == 1 && result.configuration_revision == accepted.revision {
            plane.and_then(|p| p.q).unwrap_or(f64::NAN)
        } else {
            f64::NAN
        };
        values.push((q_key, q.to_le_bytes().to_vec()));
        values.push((status_key, status.to_le_bytes().to_vec()));
    }
    Ok(values)
}

fn error_result(
    accepted: &AcceptedSpec,
    generation: u64,
    input: Option<&SpillInput>,
    message: &str,
) -> TuneResult {
    TuneResult {
        schema_version: 1,
        algorithm: "paper-best-n-v1".into(),
        software_version: software_version(),
        generation,
        acquisition_id: input.map_or_else(String::new, |i| i.acquisition_id.clone()),
        source_time_ms: input.map_or(0, |i| i.source_time_ms),
        processed_at_ms: now_ms(),
        configuration_revision: accepted.revision,
        result_kind: "failure".into(),
        synthetic: input.is_some_and(|i| i.synthetic),
        processing_ms: 0,
        superseded_acquisitions: 0,
        effective_spec: accepted.spec.clone(),
        sources: vec![],
        planes: unavailable_planes(message),
        warnings: vec![message.into()],
    }
}

#[derive(Serialize, Deserialize)]
struct Heartbeat {
    published_at_ms: u64,
    process_id: u32,
}

pub fn healthcheck(state_dir: &Path, max_age_ms: u64) -> Result<()> {
    let heartbeat: Heartbeat = read_json(&state_dir.join("heartbeat.json"))?;
    let now = now_ms();
    ensure!(
        heartbeat.published_at_ms <= now && now - heartbeat.published_at_ms <= max_age_ms,
        "processor heartbeat is stale"
    );
    Ok(())
}

fn allocate_generation(path: &Path, generation: &mut u64) -> Result<u64> {
    let next = generation
        .checked_add(1)
        .ok_or_else(|| anyhow!("result generation exhausted"))?;
    atomic_json(path, &next)?;
    *generation = next;
    Ok(next)
}

pub fn serve(options: ServeOptions) -> Result<()> {
    let config = SourceConfig::load(&options.sources)?;
    let inventory = ["H", "V"].map(|plane| {
        (
            plane.to_owned(),
            config
                .sources
                .iter()
                .filter(|source| source.plane == plane)
                .count(),
        )
    });
    let info = redis::Client::open(options.redis_url.as_str())?
        .get_connection_info()
        .clone();
    match &info.addr {
        redis::ConnectionAddr::Tcp(host, 6391)
            if matches!(host.as_str(), "127.0.0.1" | "localhost" | "::1") => {}
        _ => bail!("derived Redis must be the dedicated loopback endpoint on port 6391"),
    }
    fs::create_dir_all(&options.state_dir)?;
    let mut settings = SettingsState::load_or_create(&options.state_dir)?;
    let latest_input_path = options.state_dir.join("latest-input.json");
    let mut latest_input: Option<Arc<SpillInput>> = if latest_input_path.exists() {
        let input: SpillInput = read_json(&latest_input_path)?;
        validate_input(&input)?;
        Some(Arc::new(input))
    } else {
        None
    };
    let result_path = options.state_dir.join("latest-result.json");
    let mut latest_result: Option<TuneResult> = if result_path.exists() {
        Some(read_json(&result_path)?)
    } else {
        None
    };
    let mut generation = latest_result.as_ref().map_or(0, |r| r.generation);
    let generation_path = options.state_dir.join("generation.json");
    if generation_path.exists() {
        generation = generation.max(read_json(&generation_path)?);
    }
    if latest_result
        .as_ref()
        .is_none_or(|result| result.generation < generation)
    {
        if generation > 0 {
            allocate_generation(&generation_path, &mut generation)?;
        }
        latest_result = Some(error_result(
            &settings.accepted,
            generation,
            latest_input.as_deref(),
            if latest_input.is_some() {
                "restoring retained input after an interrupted or failed generation"
            } else {
                "awaiting first coherent source capture"
            },
        ));
    }
    let mut pending = latest_input
        .as_ref()
        .map(|input| (Arc::clone(input), "restored".to_string()));
    let mailbox = Arc::new(Mutex::new(SourceMailbox::default()));
    let source_mailbox = Arc::clone(&mailbox);
    thread::spawn(move || acquisition_loop(config, source_mailbox));
    let (jobs_tx, jobs_rx) = mpsc::sync_channel::<Job>(1);
    let (done_tx, done_rx) = mpsc::sync_channel::<Completed>(1);
    let persisted_input_path = latest_input_path.clone();
    thread::spawn(move || {
        while let Ok(job) = jobs_rx.recv() {
            let mut completed = compute(job);
            // Large raw-array serialization runs off the controls/heartbeat loop.
            if let Err(error) = atomic_json(&persisted_input_path, completed.input.as_ref()) {
                completed.persistence_error =
                    Some(format!("raw input persistence failed: {error:#}"));
            }
            if done_tx.send(completed).is_err() {
                break;
            }
        }
    });
    let mut busy = false;
    let mut superseded = 0u64;
    let mut publisher: Option<Publisher> = None;
    let mut reconnect_at = Instant::now();
    let mut last_heartbeat = Instant::now() - HEARTBEAT_INTERVAL;
    let mut result_dirty = true;
    let mut result_payload_dirty = true;
    let mut spec_dirty = true;
    let mut receipt_queue: VecDeque<SpecReceipt> = VecDeque::new();
    let mut request_cursor = "0-0".to_string();
    let mut last_error: Option<String> = None;
    loop {
        let source_error = {
            let mut slot = mailbox.lock().unwrap();
            superseded = superseded.saturating_add(std::mem::take(&mut slot.superseded));
            if let Some(input) = slot.newest.take() {
                if latest_input
                    .as_ref()
                    .is_none_or(|prior| !same_input_content(prior, &input))
                {
                    let kind = if latest_input
                        .as_ref()
                        .is_some_and(|prior| prior.acquisition_id == input.acquisition_id)
                    {
                        "source_refresh"
                    } else {
                        "live"
                    };
                    let input = Arc::new(input);
                    latest_input = Some(Arc::clone(&input));
                    if pending.replace((input, kind.into())).is_some() {
                        superseded = superseded.saturating_add(1);
                    }
                }
            }
            slot.error.clone()
        };
        if let Some(error) = source_error.filter(|e| Some(e) != last_error.as_ref()) {
            eprintln!("{error}");
            allocate_generation(&generation_path, &mut generation)?;
            latest_result = Some(error_result(
                &settings.accepted,
                generation,
                latest_input.as_deref(),
                &error,
            ));
            last_error = Some(error);
            result_dirty = true;
            result_payload_dirty = true;
        }
        match done_rx.try_recv() {
            Ok(mut complete) => {
                busy = false;
                complete.result.superseded_acquisitions = superseded;
                // A failure snapshot emitted during analysis may have advanced the
                // output generation. Completed jobs must never move it backwards.
                if complete.result.generation < generation {
                    allocate_generation(&generation_path, &mut generation)?;
                    complete.result.generation = generation;
                }
                let persisted = match complete.persistence_error {
                    Some(message) => Err(anyhow!(message)),
                    None => atomic_json(&result_path, &complete.result),
                };
                match persisted {
                    Ok(()) => {
                        latest_result = Some(complete.result);
                        last_error = None;
                    }
                    Err(error) => {
                        let message = format!("completed result persistence failed: {error:#}");
                        latest_result = Some(error_result(
                            &settings.accepted,
                            complete.result.generation,
                            Some(&complete.input),
                            &message,
                        ));
                        last_error = Some(message);
                    }
                }
                result_dirty = true;
                result_payload_dirty = true;
            }
            Err(mpsc::TryRecvError::Disconnected) => bail!("analysis worker stopped"),
            Err(mpsc::TryRecvError::Empty) => {}
        }
        if publisher.is_none() && Instant::now() >= reconnect_at {
            match Publisher::connect(&options.redis_url) {
                Ok(value) => {
                    publisher = Some(value);
                    spec_dirty = true;
                    result_dirty = true;
                    result_payload_dirty = true;
                    if receipt_queue.is_empty() {
                        receipt_queue.push_back(
                            settings
                                .requests
                                .back()
                                .map(|record| record.receipt.clone())
                                .unwrap_or_else(|| SpecReceipt {
                                    request_id: "startup".into(),
                                    accepted: true,
                                    revision: settings.accepted.revision,
                                    message: "persisted default configuration initialized".into(),
                                }),
                        );
                    }
                }
                Err(error) => eprintln!("derived Redis unavailable: {error:#}"),
            }
            reconnect_at = Instant::now() + Duration::from_secs(1);
        }
        if let Some(output) = publisher.as_mut() {
            let read: Result<redis_io::StreamEntries, _> = redis::cmd("XRANGE")
                .arg(format!("{PREFIX}SPEC_S"))
                .arg(format!("({request_cursor}"))
                .arg("+")
                .arg("COUNT")
                .arg(8)
                .query(output.connection());
            match read {
                Ok(entries) => {
                    for (id, fields) in entries {
                        request_cursor = id;
                        let prior_revision = settings.accepted.revision;
                        let receipt = match redis_io::payload(&fields) {
                            Ok(bytes) if bytes.len() <= 1_048_576 => {
                                let capacity = inventory
                                    .iter()
                                    .map(|(plane, channels)| {
                                        (
                                            plane.clone(),
                                            latest_input
                                                .as_ref()
                                                .and_then(|input| {
                                                    input
                                                        .traces
                                                        .iter()
                                                        .filter(|trace| &trace.plane == plane)
                                                        .map(|trace| trace.samples.len())
                                                        .max()
                                                })
                                                .unwrap_or(MAX_SAMPLES),
                                            *channels,
                                        )
                                    })
                                    .collect::<Vec<_>>();
                                settings.apply_with_capacity(bytes, &options.state_dir, &capacity)
                            }
                            Ok(_) => SpecReceipt {
                                request_id: String::new(),
                                accepted: false,
                                revision: prior_revision,
                                message: "request exceeds 1 MiB".into(),
                            },
                            Err(error) => SpecReceipt {
                                request_id: String::new(),
                                accepted: false,
                                revision: prior_revision,
                                message: error.to_string(),
                            },
                        };
                        if settings.accepted.revision != prior_revision {
                            spec_dirty = true;
                            result_dirty = true;
                            if let Some(input) = &latest_input {
                                pending = Some((Arc::clone(input), "reanalysis".into()));
                            }
                        }
                        if receipt_queue.len() == 64 {
                            receipt_queue.pop_front();
                        }
                        receipt_queue.push_back(receipt);
                    }
                }
                Err(error) => {
                    eprintln!("settings read failed: {error}");
                    publisher = None;
                }
            }
        }
        if !busy && let Some((input, kind)) = pending.take() {
            allocate_generation(&generation_path, &mut generation)?;
            jobs_tx
                .send(Job {
                    input,
                    accepted: settings.accepted.clone(),
                    generation,
                    kind,
                    superseded,
                })
                .context("analysis worker stopped")?;
            busy = true;
            result_dirty = true;
        }
        if let Some(output) = publisher.as_mut() {
            let heartbeat_due = last_heartbeat.elapsed() >= HEARTBEAT_INTERVAL;
            let publication = (|| -> Result<()> {
                if spec_dirty {
                    output.publish(&[
                        ("SPEC_R", serde_json::to_vec(&settings.accepted)?),
                        ("SCHEMA_R", serde_json::to_vec(&MeasurementSpec::schema())?),
                        (
                            "DEFAULT_SPEC_R",
                            serde_json::to_vec(&MeasurementSpec::default())?,
                        ),
                    ])?;
                    spec_dirty = false;
                }
                while let Some(receipt) = receipt_queue.front() {
                    output.publish(&[("SPEC_STATUS_R", serde_json::to_vec(receipt)?)])?;
                    receipt_queue.pop_front();
                }
                if result_dirty || heartbeat_due {
                    let mut values = if let Some(result) = &latest_result {
                        result_values(
                            result,
                            &settings.accepted,
                            busy,
                            last_error.is_some(),
                            result_payload_dirty,
                        )?
                    } else {
                        let state: u32 = if busy { 2 } else { 5 };
                        vec![
                            ("SOURCE_AGE_MS_R", u64::MAX.to_le_bytes().to_vec()),
                            ("STATE_R", state.to_le_bytes().to_vec()),
                            ("H:Q_R", f64::NAN.to_le_bytes().to_vec()),
                            ("V:Q_R", f64::NAN.to_le_bytes().to_vec()),
                            ("H:STATUS_R", 0u32.to_le_bytes().to_vec()),
                            ("V:STATUS_R", 0u32.to_le_bytes().to_vec()),
                        ]
                    };
                    let heartbeat_ms = now_ms();
                    values.push(("HEARTBEAT_R", heartbeat_ms.to_le_bytes().to_vec()));
                    output.publish(&values)?;
                    atomic_json(
                        &options.state_dir.join("heartbeat.json"),
                        &Heartbeat {
                            published_at_ms: heartbeat_ms,
                            process_id: std::process::id(),
                        },
                    )?;
                    result_dirty = false;
                    result_payload_dirty = false;
                    last_heartbeat = Instant::now();
                }
                Ok(())
            })();
            if let Err(error) = publication {
                eprintln!("publication failed: {error:#}");
                publisher = None;
            }
        }
        thread::sleep(Duration::from_millis(25));
    }
}

pub fn analyze_file(
    input: &Path,
    source_path: Option<&Path>,
    spec_path: Option<&Path>,
    output: Option<&Path>,
) -> Result<()> {
    let config = source_path.map(SourceConfig::load).transpose()?;
    let input = load_input(input, config.as_ref())?;
    let spec: MeasurementSpec = if let Some(path) = spec_path {
        let value: Value = read_json(path)?;
        serde_json::from_value(value.get("spec").cloned().unwrap_or(value))?
    } else {
        MeasurementSpec::default()
    };
    spec.validate()?;
    let completed = compute(Job {
        input: Arc::new(input),
        accepted: AcceptedSpec { revision: 0, spec },
        generation: 1,
        kind: "replay".into(),
        superseded: 0,
    });
    if let Some(path) = output {
        atomic_json(path, &completed.result)
    } else {
        println!("{}", serde_json::to_string(&completed.result)?);
        Ok(())
    }
}

pub fn load_input(path: &Path, config: Option<&SourceConfig>) -> Result<SpillInput> {
    let path = if path.is_dir() {
        path.join("manifest.json")
    } else {
        path.to_owned()
    };
    // Native retained captures can contain millions of samples. Deserialize
    // directly from the reader instead of materializing a second JSON value tree.
    match read_json::<SpillInput>(&path) {
        Ok(input) => {
            validate_input(&input)?;
            Ok(input)
        }
        Err(native_error) => {
            let value: Value = read_json(&path)?;
            if value.get("artifact_type").and_then(Value::as_str)
                == Some("tbt-monitor.captured-spill")
            {
                load_captured_bundle(&path, value, config)
            } else {
                Err(native_error)
            }
        }
    }
}

fn load_captured_bundle(
    path: &Path,
    manifest: Value,
    config: Option<&SourceConfig>,
) -> Result<SpillInput> {
    let target = manifest
        .get("target_ms")
        .and_then(Value::as_u64)
        .ok_or_else(|| anyhow!("captured manifest has no target_ms"))?;
    let tolerance = manifest
        .get("same_spill_tolerance_ms")
        .and_then(Value::as_u64)
        .unwrap_or(25);
    let streams = manifest
        .get("streams")
        .and_then(Value::as_array)
        .ok_or_else(|| anyhow!("captured manifest has no streams"))?;
    let mut warnings =
        vec!["replayed captured bundle; source acquisition time is unchanged".into()];
    if config.is_none() {
        warnings.push(
            "no source inventory supplied; beam thresholds are unqualified, display suppressed"
                .into(),
        );
    }
    let recorded_synthetic = manifest.get("synthetic").and_then(Value::as_bool);
    if recorded_synthetic.is_none() {
        warnings.push(
            "captured manifest has no acquisition-time synthetic status; display suppressed".into(),
        );
    }
    let mut traces = Vec::new();
    for stream in streams {
        let key = stream
            .get("stream_key")
            .and_then(Value::as_str)
            .unwrap_or("");
        if !key.contains(":TBT_POSITION_") && !key.ends_with(":POSITION_RAW_R") {
            continue;
        }
        let id = stream
            .get("stream_id")
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow!("position has no stream ID"))?;
        let (ms, _) = redis_io::stream_id(id)?;
        if ms.abs_diff(target) > tolerance {
            warnings.push(format!(
                "excluded {key}: outside source alignment tolerance"
            ));
            continue;
        }
        if stream.get("payload_file").and_then(Value::as_str).is_none() {
            warnings.push(format!("excluded {key}: payload missing"));
            continue;
        }
        let source = config.and_then(|c| c.sources.iter().find(|s| s.position_key == key));
        let bpm = source
            .map(|s| s.id.clone())
            .unwrap_or_else(|| key.rsplit(':').nth(1).unwrap_or("unknown").to_owned());
        let intensity_key = source.map(|s| s.intensity_key.clone()).unwrap_or_else(|| {
            key.strip_suffix(":POSITION_RAW_R")
                .map(|prefix| format!("{prefix}:INTENSITY_RAW_R"))
                .unwrap_or_else(|| {
                    format!(
                        "{}:TBT_INTENSITY_RAW",
                        key.rsplit_once(':').map_or(key, |v| v.0)
                    )
                })
        });
        let intensity_entry = streams.iter().find(|entry| {
            entry.get("stream_key").and_then(Value::as_str) == Some(&intensity_key)
                && entry.get("stream_id").and_then(Value::as_str) == Some(id)
                && entry.get("payload_file").and_then(Value::as_str).is_some()
        });
        if intensity_entry.is_none() {
            warnings.push(format!("{bpm} has no matching intensity payload"));
        }
        traces.push(TraceInput {
            id: bpm.clone(),
            plane: source.map(|s| s.plane.clone()).unwrap_or_else(|| {
                stream
                    .get("plane")
                    .and_then(Value::as_str)
                    .unwrap_or(if bpm.starts_with('H') { "H" } else { "V" })
                    .into()
            }),
            digitizer: source.map(|s| s.digitizer.clone()).unwrap_or_else(|| {
                stream
                    .get("bpm_ip")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown")
                    .into()
            }),
            ring_order: source.map_or_else(
                || bpm.get(2..).and_then(|v| v.parse().ok()).unwrap_or(0.0),
                |s| s.ring_order,
            ),
            source_key: key.into(),
            source_stream_id: id.into(),
            intensity_stream_id: intensity_entry.map(|_| id.into()),
            beam_threshold: source.map_or(0.0, |s| s.beam_threshold),
            samples: bundle_samples(path, stream)?,
            intensity: intensity_entry
                .map(|entry| bundle_samples(path, entry))
                .transpose()?,
        });
    }
    let input = SpillInput {
        schema_version: 1,
        acquisition_id: format!("captured:{target}"),
        source_time_ms: target,
        captured_at_ms: manifest
            .get("redis_timestamp_ms")
            .and_then(Value::as_u64)
            .unwrap_or(target),
        synthetic: config.is_none() || recorded_synthetic.unwrap_or(true),
        traces,
        warnings,
    };
    validate_input(&input)?;
    Ok(input)
}

fn bundle_samples(manifest_path: &Path, stream: &Value) -> Result<Vec<f32>> {
    let relative = Path::new(
        stream
            .get("payload_file")
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow!("missing payload path"))?,
    );
    ensure!(
        !relative.is_absolute()
            && relative
                .components()
                .all(|component| matches!(component, Component::Normal(_) | Component::CurDir)),
        "payload path escapes bundle"
    );
    let root = manifest_path.parent().unwrap_or(Path::new("."));
    let resolved = root.join(relative).canonicalize()?;
    ensure!(
        resolved.starts_with(root.canonicalize()?),
        "payload symlink escapes bundle"
    );
    let bytes = fs::read(&resolved)?;
    if let Some(length) = stream.get("payload_bytes").and_then(Value::as_u64) {
        ensure!(
            length == bytes.len() as u64,
            "payload byte length mismatch for {}",
            resolved.display()
        );
    }
    if let Some(checksum) = stream.get("checksum_fnv1a64").and_then(Value::as_str) {
        let hash = bytes.iter().fold(0xcbf29ce484222325u64, |hash, b| {
            (hash ^ u64::from(*b)).wrapping_mul(0x100000001b3)
        });
        ensure!(
            checksum.eq_ignore_ascii_case(&format!("{hash:016x}")),
            "payload checksum mismatch for {}",
            resolved.display()
        );
    }
    let samples = decode_samples(&bytes)?;
    if let Some(length) = stream.get("sample_count").and_then(Value::as_u64) {
        ensure!(
            length == samples.len() as u64,
            "sample count mismatch for {}",
            resolved.display()
        );
    }
    Ok(samples)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "tune-{name}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn cas_rejects_conflict_invalid_and_failed_persistence() {
        let dir = temp_dir("cas");
        let mut state = SettingsState::load_or_create(&dir).unwrap();
        let request = SpecRequest {
            request_id: "first".into(),
            expected_revision: 0,
            spec: MeasurementSpec::default(),
        };
        let bytes = serde_json::to_vec(&request).unwrap();
        assert!(state.apply(&bytes, &dir).accepted);
        assert_eq!(state.accepted.revision, 1);
        assert!(state.apply(&bytes, &dir).accepted); // idempotent original receipt
        let mut conflict = request.clone();
        conflict.request_id = "conflict".into();
        assert!(
            !state
                .apply(&serde_json::to_vec(&conflict).unwrap(), &dir)
                .accepted
        );
        let mut invalid = request.clone();
        invalid.request_id = "invalid".into();
        invalid.expected_revision = 1;
        invalid.spec.planes.h.n = 0;
        assert!(
            !state
                .apply(&serde_json::to_vec(&invalid).unwrap(), &dir)
                .accepted
        );
        let mut failed = request;
        failed.request_id = "failed".into();
        failed.expected_revision = 1;
        assert!(
            !state
                .apply(&serde_json::to_vec(&failed).unwrap(), &dir.join("absent"))
                .accepted
        );
        assert_eq!(state.accepted.revision, 1);
        let mut restored = SettingsState::load_or_create(&dir).unwrap();
        assert_eq!(restored.accepted.revision, 1);
        assert!(restored.apply(&bytes, &dir).accepted);
        assert_eq!(restored.accepted.revision, 1);
        let mut expensive = MeasurementSpec::default();
        expensive.analysis_end_turn = None;
        expensive.selection_end_turn = None;
        expensive.selection.step_turns = 1;
        let rejected = restored.apply(
            &serde_json::to_vec(&SpecRequest {
                request_id: "unbounded".into(),
                expected_revision: 1,
                spec: expensive,
            })
            .unwrap(),
            &dir,
        );
        assert!(!rejected.accepted && rejected.message.contains("execution budget"));
        fs::remove_dir_all(dir).unwrap();
    }

    fn source(id: &str) -> Source {
        Source {
            id: id.into(),
            plane: "H".into(),
            digitizer: "d1".into(),
            ring_order: 0.0,
            host: "127.0.0.1".into(),
            port: 6379,
            db: 0,
            position_key: format!("{id}:P"),
            intensity_key: format!("{id}:I"),
            beam_threshold: 1.0,
        }
    }

    #[test]
    fn coherent_capture_pairs_exact_ids_and_bounds_group_span() {
        let a = source("HP101");
        let b = source("HP102");
        let c = source("HP103");
        let config = SourceConfig {
            version: 1,
            sources: vec![a.clone(), b.clone(), c.clone()],
            test_mode: None,
            source_tolerance_ms: 25,
            settle_ms: 150,
            max_settle_ms: 1000,
        };
        let mut cache = AcquisitionCache::default();
        for (source, pos_id, intensity_id) in [
            (&a, "1000-1", "1000-2"),
            (&b, "1020-1", "1020-1"),
            (&c, "1040-1", "1040-1"),
        ] {
            cache_entry(
                &mut cache,
                &source.url(),
                &source.position_key,
                (
                    pos_id.into(),
                    vec![(b"_".to_vec(), 1f32.to_le_bytes().to_vec())],
                ),
            )
            .unwrap();
            cache_entry(
                &mut cache,
                &source.url(),
                &source.intensity_key,
                (
                    intensity_id.into(),
                    vec![(b"_".to_vec(), 1f32.to_le_bytes().to_vec())],
                ),
            )
            .unwrap();
        }
        let (target, pairs) = select_coherent(&config, &cache).unwrap();
        assert_eq!(target, 1040);
        assert_eq!(pairs.len(), 2);
        assert_eq!(pairs[0].0.id, "HP102");
        let config = SourceConfig {
            sources: vec![a],
            ..config
        };
        let (_, pairs) = select_coherent(&config, &cache).unwrap();
        assert!(pairs[0].2.is_none());
    }

    #[test]
    fn cache_evicts_spare_history_without_stranding_full_inventory() {
        let mut cache = AcquisitionCache::default();
        // Scale sample lengths down while retaining all 120 position/intensity
        // pairs and a budget that holds one complete acquisition plus spare slots.
        for sequence in 1..=3 {
            for bpm in 0..120 {
                for kind in ["position", "intensity"] {
                    cache_entry_with_budget(
                        &mut cache,
                        "redis://test",
                        &format!("{bpm}:{kind}"),
                        (
                            format!("1000-{sequence}"),
                            vec![(b"_".to_vec(), vec![0; 32])],
                        ),
                        12_000,
                    )
                    .unwrap();
                }
            }
            assert_eq!(cache.entries.len(), 240);
            assert!(cache.bytes <= 12_000);
            assert!(
                cache
                    .entries
                    .values()
                    .all(|entries| entries.back().unwrap().id == format!("1000-{sequence}"))
            );
        }
        let expected: usize = cache
            .entries
            .values()
            .flat_map(|entries| entries.iter())
            .map(|entry| entry.bytes.len())
            .sum();
        assert_eq!(cache.bytes, expected);
    }

    #[test]
    fn raw_samples_roundtrip_keeps_length_and_nonfinite_gap() {
        let input = SpillInput {
            schema_version: 1,
            acquisition_id: "test".into(),
            source_time_ms: 123,
            captured_at_ms: 456,
            synthetic: false,
            traces: vec![TraceInput {
                id: "HP101".into(),
                plane: "H".into(),
                digitizer: "d".into(),
                ring_order: 0.0,
                source_key: "p".into(),
                source_stream_id: "123-1".into(),
                intensity_stream_id: None,
                beam_threshold: 1.0,
                samples: vec![1.0, f32::NAN, 3.0],
                intensity: None,
            }],
            warnings: vec![],
        };
        let dir = temp_dir("roundtrip");
        atomic_json(&dir.join("input.json"), &input).unwrap();
        let restored = load_input(&dir.join("input.json"), None).unwrap();
        assert_eq!(restored.source_time_ms, 123);
        assert_eq!(restored.traces[0].samples.len(), 3);
        assert!(restored.traces[0].samples[1].is_nan());
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn stale_or_old_revision_never_publishes_plausible_scalar() {
        let accepted = AcceptedSpec {
            revision: 1,
            spec: MeasurementSpec::default(),
        };
        let mut result = error_result(&accepted, 1, None, "test");
        result.source_time_ms = now_ms();
        result.configuration_revision = 0;
        result.planes.get_mut("H").unwrap().state = "valid".into();
        result.planes.get_mut("H").unwrap().q = Some(0.65);
        let value = result_values(&result, &accepted, true, false, true)
            .unwrap()
            .into_iter()
            .find(|(key, _)| *key == "H:Q_R")
            .unwrap()
            .1;
        assert!(f64::from_le_bytes(value.try_into().unwrap()).is_nan());
        result.configuration_revision = 1;
        result.source_time_ms = 1;
        let value = result_values(&result, &accepted, false, false, true)
            .unwrap()
            .into_iter()
            .find(|(key, _)| *key == "H:Q_R")
            .unwrap()
            .1;
        assert!(f64::from_le_bytes(value.try_into().unwrap()).is_nan());
    }
}
