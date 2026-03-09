//! Monitor configuration model, parser, serializer, and validation rules.
//!
//! Why this module exists:
//! - Keep all config semantics centralized (defaults, parser behavior, validation).
//! - Make CLI overrides safe by validating a single `MonitorConfig` model.
//! - Keep backward compatibility for historical config keys (for example `poll_ms`).
//!
//! Design constraints:
//! - Human-editable flat text format with repeated `[[device]]` sections.
//! - Strict unknown-key rejection to avoid silent misconfiguration.
//! - Validation guards against analysis/runtime states known to produce unusable data.

use std::fs;
use std::path::Path;

use anyhow::{Context, Result, bail};

#[derive(Debug, Clone)]
pub struct MonitorConfig {
    pub xread_block_ms: u64,
    pub reconnect_initial_ms: u64,
    pub reconnect_max_ms: u64,
    pub min_stream_values: usize,
    pub injection_start_turn: usize,
    pub injection_window_turns: usize,
    pub sliding_window_turns: usize,
    pub sliding_stride_turns: usize,
    pub qx_band_min: f64,
    pub qx_band_max: f64,
    pub qy_band_min: f64,
    pub qy_band_max: f64,
    pub min_peak_confidence: f64,
    pub enable_peak_tracking: bool,
    pub qx_track_half_width: f64,
    pub qy_track_half_width: f64,
    pub max_tune_step_per_window: f64,
    pub align_tolerance_ms: u64,
    pub min_aligned_fraction: f64,
    pub devices: Vec<DeviceConfig>,
}

#[derive(Debug, Clone)]
pub struct DeviceConfig {
    pub label: String,
    pub bpm_ip: String,
    pub redis: RedisConfig,
    pub trigger_key: String,
    pub trigger_fallback_keys: Vec<String>,
    pub stream_keys: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct RedisConfig {
    pub host: String,
    pub port: u16,
    pub db: i64,
    pub username: Option<String>,
    pub password: Option<String>,
}

impl RedisConfig {
    pub fn to_url(&self) -> String {
        let auth = match (&self.username, &self.password) {
            (Some(user), Some(pass)) => format!("{}:{}@", user, pass),
            (Some(user), None) => format!("{}@", user),
            _ => String::new(),
        };
        format!("redis://{}{}:{}/{}", auth, self.host, self.port, self.db)
    }

    pub fn display_addr(&self) -> String {
        format!("{}:{}", self.host, self.port)
    }
}

impl MonitorConfig {
    pub fn validate(&self) -> Result<()> {
        if self.devices.is_empty() {
            bail!("config has no devices");
        }

        if self.xread_block_ms != 0 && self.xread_block_ms < 50 {
            bail!("xread_block_ms must be 0 (infinite) or >= 50");
        }

        if self.reconnect_initial_ms < 250 {
            bail!("reconnect_initial_ms must be >= 250");
        }

        if self.reconnect_max_ms < self.reconnect_initial_ms {
            bail!("reconnect_max_ms must be >= reconnect_initial_ms");
        }

        if self.min_stream_values == 0 {
            bail!("min_stream_values must be >= 1");
        }

        if self.injection_window_turns == 0 {
            bail!("injection_window_turns must be >= 1");
        }

        if self.sliding_window_turns == 0 {
            bail!("sliding_window_turns must be >= 1");
        }

        if self.sliding_stride_turns == 0 {
            bail!("sliding_stride_turns must be >= 1");
        }

        validate_band("qx", self.qx_band_min, self.qx_band_max)?;
        validate_band("qy", self.qy_band_min, self.qy_band_max)?;
        validate_peak_confidence(self.min_peak_confidence)?;

        validate_tracking_param("qx_track_half_width", self.qx_track_half_width)?;
        validate_tracking_param("qy_track_half_width", self.qy_track_half_width)?;
        validate_tracking_param("max_tune_step_per_window", self.max_tune_step_per_window)?;

        if !(0.0..=1.0).contains(&self.min_aligned_fraction) || self.min_aligned_fraction == 0.0 {
            bail!("min_aligned_fraction must be > 0 and <= 1");
        }

        for device in &self.devices {
            if device.stream_keys.is_empty() {
                bail!("device {} has no stream_keys", device.label);
            }
            if device.trigger_key.trim().is_empty() {
                bail!("device {} has an empty trigger_key", device.label);
            }
            if device.redis.host.trim().is_empty() {
                bail!("device {} has an empty redis.host", device.label);
            }
        }

        Ok(())
    }
}

pub fn load_monitor_config(path: &Path) -> Result<MonitorConfig> {
    let raw = fs::read_to_string(path)
        .with_context(|| format!("unable to read config file {}", path.display()))?;

    let mut xread_block_ms = 1_000u64;
    let mut reconnect_initial_ms = 2_000u64;
    let mut reconnect_max_ms = 30_000u64;
    let mut min_stream_values = 1usize;
    let mut injection_start_turn = 0usize;
    let mut injection_window_turns = 1_024usize;
    let mut sliding_window_turns = 2_048usize;
    let mut sliding_stride_turns = 256usize;
    let mut qx_band_min = 0.58f64;
    let mut qx_band_max = 0.72f64;
    let mut qy_band_min = 0.58f64;
    let mut qy_band_max = 0.72f64;
    let mut min_peak_confidence = 2.0f64;
    let mut enable_peak_tracking = true;
    let mut qx_track_half_width = 0.005f64;
    let mut qy_track_half_width = 0.005f64;
    let mut max_tune_step_per_window = 0.005f64;
    let mut align_tolerance_ms = 1u64;
    let mut min_aligned_fraction = 0.70f64;

    let mut devices: Vec<DeviceConfig> = Vec::new();
    let mut current: Option<DeviceConfig> = None;

    for (line_no, raw_line) in raw.lines().enumerate() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }

        if line == "[[device]]" {
            if let Some(dev) = current.take() {
                devices.push(dev);
            }
            current = Some(DeviceConfig {
                label: String::new(),
                bpm_ip: String::new(),
                redis: RedisConfig {
                    host: String::new(),
                    port: 6379,
                    db: 0,
                    username: None,
                    password: None,
                },
                trigger_key: String::new(),
                trigger_fallback_keys: Vec::new(),
                stream_keys: Vec::new(),
            });
            continue;
        }

        let Some((key, value)) = line.split_once('=') else {
            bail!("invalid config line {}: {}", line_no + 1, raw_line);
        };

        let key = key.trim();
        let value = value.trim();

        if let Some(device) = current.as_mut() {
            match key {
                "label" => device.label = value.to_string(),
                "bpm_ip" => device.bpm_ip = value.to_string(),
                "redis_host" => device.redis.host = value.to_string(),
                "redis_port" => {
                    device.redis.port = value
                        .parse::<u16>()
                        .with_context(|| format!("invalid redis_port on line {}", line_no + 1))?
                }
                "redis_db" => {
                    device.redis.db = value
                        .parse::<i64>()
                        .with_context(|| format!("invalid redis_db on line {}", line_no + 1))?
                }
                "redis_username" => {
                    if !value.is_empty() {
                        device.redis.username = Some(value.to_string());
                    }
                }
                "redis_password" => {
                    if !value.is_empty() {
                        device.redis.password = Some(value.to_string());
                    }
                }
                "trigger_key" => device.trigger_key = value.to_string(),
                "trigger_fallback" => device.trigger_fallback_keys.push(value.to_string()),
                "stream_key" => device.stream_keys.push(value.to_string()),
                _ => {
                    bail!("unknown device key '{}' on line {}", key, line_no + 1);
                }
            }
        } else {
            match key {
                // Backward compatibility for v1 configs.
                "poll_ms" => {
                    xread_block_ms = value
                        .parse::<u64>()
                        .with_context(|| format!("invalid poll_ms on line {}", line_no + 1))?
                }
                "xread_block_ms" => {
                    xread_block_ms = value.parse::<u64>().with_context(|| {
                        format!("invalid xread_block_ms on line {}", line_no + 1)
                    })?
                }
                "reconnect_initial_ms" => {
                    reconnect_initial_ms = value.parse::<u64>().with_context(|| {
                        format!("invalid reconnect_initial_ms on line {}", line_no + 1)
                    })?
                }
                "reconnect_max_ms" => {
                    reconnect_max_ms = value.parse::<u64>().with_context(|| {
                        format!("invalid reconnect_max_ms on line {}", line_no + 1)
                    })?
                }
                "min_stream_values" => {
                    min_stream_values = value.parse::<usize>().with_context(|| {
                        format!("invalid min_stream_values on line {}", line_no + 1)
                    })?
                }
                "injection_start_turn" => {
                    injection_start_turn = value.parse::<usize>().with_context(|| {
                        format!("invalid injection_start_turn on line {}", line_no + 1)
                    })?
                }
                "injection_window_turns" => {
                    injection_window_turns = value.parse::<usize>().with_context(|| {
                        format!("invalid injection_window_turns on line {}", line_no + 1)
                    })?
                }
                "sliding_window_turns" => {
                    sliding_window_turns = value.parse::<usize>().with_context(|| {
                        format!("invalid sliding_window_turns on line {}", line_no + 1)
                    })?
                }
                "sliding_stride_turns" => {
                    sliding_stride_turns = value.parse::<usize>().with_context(|| {
                        format!("invalid sliding_stride_turns on line {}", line_no + 1)
                    })?
                }
                "qx_band_min" => {
                    qx_band_min = value
                        .parse::<f64>()
                        .with_context(|| format!("invalid qx_band_min on line {}", line_no + 1))?
                }
                "qx_band_max" => {
                    qx_band_max = value
                        .parse::<f64>()
                        .with_context(|| format!("invalid qx_band_max on line {}", line_no + 1))?
                }
                "qy_band_min" => {
                    qy_band_min = value
                        .parse::<f64>()
                        .with_context(|| format!("invalid qy_band_min on line {}", line_no + 1))?
                }
                "qy_band_max" => {
                    qy_band_max = value
                        .parse::<f64>()
                        .with_context(|| format!("invalid qy_band_max on line {}", line_no + 1))?
                }
                "min_peak_confidence" => {
                    min_peak_confidence = value.parse::<f64>().with_context(|| {
                        format!("invalid min_peak_confidence on line {}", line_no + 1)
                    })?
                }
                "enable_peak_tracking" => {
                    enable_peak_tracking = parse_bool(value).with_context(|| {
                        format!("invalid enable_peak_tracking on line {}", line_no + 1)
                    })?
                }
                "qx_track_half_width" => {
                    qx_track_half_width = value.parse::<f64>().with_context(|| {
                        format!("invalid qx_track_half_width on line {}", line_no + 1)
                    })?
                }
                "qy_track_half_width" => {
                    qy_track_half_width = value.parse::<f64>().with_context(|| {
                        format!("invalid qy_track_half_width on line {}", line_no + 1)
                    })?
                }
                "max_tune_step_per_window" => {
                    max_tune_step_per_window = value.parse::<f64>().with_context(|| {
                        format!("invalid max_tune_step_per_window on line {}", line_no + 1)
                    })?
                }
                "align_tolerance_ms" => {
                    align_tolerance_ms = value.parse::<u64>().with_context(|| {
                        format!("invalid align_tolerance_ms on line {}", line_no + 1)
                    })?
                }
                "min_aligned_fraction" => {
                    min_aligned_fraction = value.parse::<f64>().with_context(|| {
                        format!("invalid min_aligned_fraction on line {}", line_no + 1)
                    })?
                }
                _ => {
                    bail!("unknown top-level key '{}' on line {}", key, line_no + 1);
                }
            }
        }
    }

    if let Some(dev) = current {
        devices.push(dev);
    }

    let config = MonitorConfig {
        xread_block_ms,
        reconnect_initial_ms,
        reconnect_max_ms,
        min_stream_values,
        injection_start_turn,
        injection_window_turns,
        sliding_window_turns,
        sliding_stride_turns,
        qx_band_min,
        qx_band_max,
        qy_band_min,
        qy_band_max,
        min_peak_confidence,
        enable_peak_tracking,
        qx_track_half_width,
        qy_track_half_width,
        max_tune_step_per_window,
        align_tolerance_ms,
        min_aligned_fraction,
        devices,
    };

    config.validate()?;
    Ok(config)
}

pub fn save_monitor_config(path: &Path, config: &MonitorConfig) -> Result<()> {
    config.validate()?;

    let mut out = String::new();
    out.push_str("# tbt-monitor-tui config v2\n");
    out.push_str("# stream-driven mode (XREAD BLOCK)\n");
    out.push_str(&format!("xread_block_ms={}\n", config.xread_block_ms));
    out.push_str(&format!(
        "reconnect_initial_ms={}\n",
        config.reconnect_initial_ms
    ));
    out.push_str(&format!("reconnect_max_ms={}\n", config.reconnect_max_ms));
    out.push_str(&format!("min_stream_values={}\n", config.min_stream_values));
    out.push_str(&format!(
        "injection_start_turn={}\n",
        config.injection_start_turn
    ));
    out.push_str(&format!(
        "injection_window_turns={}\n",
        config.injection_window_turns
    ));
    out.push_str(&format!(
        "sliding_window_turns={}\n",
        config.sliding_window_turns
    ));
    out.push_str(&format!(
        "sliding_stride_turns={}\n",
        config.sliding_stride_turns
    ));
    out.push_str(&format!("qx_band_min={}\n", config.qx_band_min));
    out.push_str(&format!("qx_band_max={}\n", config.qx_band_max));
    out.push_str(&format!("qy_band_min={}\n", config.qy_band_min));
    out.push_str(&format!("qy_band_max={}\n", config.qy_band_max));
    out.push_str(&format!(
        "min_peak_confidence={}\n",
        config.min_peak_confidence
    ));
    out.push_str(&format!(
        "enable_peak_tracking={}\n",
        config.enable_peak_tracking
    ));
    out.push_str(&format!(
        "qx_track_half_width={}\n",
        config.qx_track_half_width
    ));
    out.push_str(&format!(
        "qy_track_half_width={}\n",
        config.qy_track_half_width
    ));
    out.push_str(&format!(
        "max_tune_step_per_window={}\n",
        config.max_tune_step_per_window
    ));
    out.push_str(&format!(
        "align_tolerance_ms={}\n",
        config.align_tolerance_ms
    ));
    out.push_str(&format!(
        "min_aligned_fraction={}\n",
        config.min_aligned_fraction
    ));

    for device in &config.devices {
        out.push_str("\n[[device]]\n");
        out.push_str(&format!("label={}\n", device.label));
        out.push_str(&format!("bpm_ip={}\n", device.bpm_ip));
        out.push_str(&format!("redis_host={}\n", device.redis.host));
        out.push_str(&format!("redis_port={}\n", device.redis.port));
        out.push_str(&format!("redis_db={}\n", device.redis.db));

        if let Some(username) = device.redis.username.as_ref() {
            out.push_str(&format!("redis_username={}\n", username));
        }
        if let Some(password) = device.redis.password.as_ref() {
            out.push_str(&format!("redis_password={}\n", password));
        }

        out.push_str(&format!("trigger_key={}\n", device.trigger_key));

        for fallback in &device.trigger_fallback_keys {
            out.push_str(&format!("trigger_fallback={}\n", fallback));
        }

        for stream in &device.stream_keys {
            out.push_str(&format!("stream_key={}\n", stream));
        }
    }

    fs::write(path, out).with_context(|| format!("unable to write {}", path.display()))
}

fn validate_band(name: &str, min: f64, max: f64) -> Result<()> {
    if !(0.0..=1.0).contains(&min) || !(0.0..=1.0).contains(&max) {
        bail!("{name}_band must be within [0, 1]");
    }
    if min >= max {
        bail!("{name}_band_min must be < {name}_band_max");
    }
    Ok(())
}

fn validate_tracking_param(name: &str, value: f64) -> Result<()> {
    if !value.is_finite() || value <= 0.0 {
        bail!("{name} must be finite and > 0");
    }
    if value > 0.5 {
        bail!("{name} must be <= 0.5");
    }
    Ok(())
}

fn validate_peak_confidence(value: f64) -> Result<()> {
    if !value.is_finite() || value <= 0.0 {
        bail!("min_peak_confidence must be finite and > 0");
    }
    Ok(())
}

fn parse_bool(value: &str) -> Result<bool> {
    match value.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" | "on" => Ok(true),
        "false" | "0" | "no" | "off" => Ok(false),
        _ => bail!("expected bool"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn write_temp_config(content: &str) -> std::path::PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let path = std::env::temp_dir().join(format!("tbt-monitor-config-{unique}.cfg"));
        fs::write(&path, content).expect("write config");
        path
    }

    fn base_config_text() -> String {
        r#"
[[device]]
label=test
bpm_ip=10.0.0.1
redis_host=127.0.0.1
trigger_key={MUON:BPM:10.0.0.1}:LAST_TRIGGER_TIME
stream_key={MUON:BPM:10.0.0.1}:HP101:TBT_POSITION_SCALED
"#
        .to_string()
    }

    #[test]
    fn tracking_defaults_are_loaded() {
        let path = write_temp_config(&base_config_text());
        let config = load_monitor_config(&path).expect("load config");
        let _ = fs::remove_file(path);

        assert!((config.min_peak_confidence - 2.0).abs() < 1e-12);
        assert!(config.enable_peak_tracking);
        assert_eq!(config.sliding_window_turns, 2048);
        assert_eq!(config.sliding_stride_turns, 256);
        assert!((config.qx_track_half_width - 0.005).abs() < 1e-12);
        assert!((config.qy_track_half_width - 0.005).abs() < 1e-12);
        assert!((config.max_tune_step_per_window - 0.005).abs() < 1e-12);
    }

    #[test]
    fn tracking_values_parse_from_config() {
        let mut text = String::new();
        text.push_str("enable_peak_tracking=false\n");
        text.push_str("qx_track_half_width=0.01\n");
        text.push_str("qy_track_half_width=0.02\n");
        text.push_str("max_tune_step_per_window=0.03\n");
        text.push_str("min_peak_confidence=1.1\n");
        text.push_str(&base_config_text());
        let path = write_temp_config(&text);
        let config = load_monitor_config(&path).expect("load config");
        let _ = fs::remove_file(path);

        assert!(!config.enable_peak_tracking);
        assert!((config.min_peak_confidence - 1.1).abs() < 1e-12);
        assert!((config.qx_track_half_width - 0.01).abs() < 1e-12);
        assert!((config.qy_track_half_width - 0.02).abs() < 1e-12);
        assert!((config.max_tune_step_per_window - 0.03).abs() < 1e-12);
    }

    #[test]
    fn tracking_validation_rejects_invalid_ranges() {
        let path = write_temp_config(&base_config_text());
        let mut config = load_monitor_config(&path).expect("load config");
        let _ = fs::remove_file(path);

        config.qx_track_half_width = 0.0;
        assert!(config.validate().is_err());

        config.qx_track_half_width = 0.005;
        config.min_peak_confidence = 0.0;
        assert!(config.validate().is_err());

        config.min_peak_confidence = 2.0;
        config.max_tune_step_per_window = 0.75;
        assert!(config.validate().is_err());
    }
}
