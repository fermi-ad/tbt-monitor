//! ACNET XML -> `MonitorConfig` importer.
//!
//! Import policy:
//! - Extract DR BPM TbT stream keys and trigger keys from process-scoped XML nodes.
//! - Prefer deterministic output ordering for diffability and repeatable generated configs.
//! - Fill safe defaults when XML omits optional fields (for example trigger fallbacks).
//!
//! This module intentionally does not perform analysis-time physics decisions; it only
//! builds a faithful, runnable monitoring/analysis configuration from infrastructure data.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;

use anyhow::{Context, Result};
use roxmltree::{Document, Node};

use crate::config::{DeviceConfig, MonitorConfig, RedisConfig};

const MUON_BPM_PREFIX: &str = "{MUON:BPM:";

#[derive(Debug, Clone)]
pub struct ImportReport {
    pub device_count: usize,
    pub stream_count: usize,
}

#[derive(Debug, Clone)]
struct DeviceDraft {
    label: String,
    bpm_ip: String,
    redis_host: String,
    redis_port: u16,
    trigger_key: Option<String>,
    trigger_fallback_keys: BTreeSet<String>,
    stream_keys: BTreeSet<String>,
}

pub fn import_xml_config(path: &Path) -> Result<(MonitorConfig, ImportReport)> {
    let xml = fs::read_to_string(path)
        .with_context(|| format!("unable to read XML file {}", path.display()))?;
    let doc = Document::parse(&xml).context("invalid XML")?;

    let mut drafts: BTreeMap<String, DeviceDraft> = BTreeMap::new();

    for process_node in doc
        .descendants()
        .filter(|node| node.is_element() && node.has_tag_name("process"))
    {
        let Some(redis_raw) = direct_child_text(process_node, "redisServerSocketAddress") else {
            continue;
        };

        let Some((redis_host, redis_port)) = parse_host_port(redis_raw) else {
            continue;
        };

        let device_name = process_node
            .ancestors()
            .find(|node| node.is_element() && node.has_tag_name("device"))
            .and_then(|node| direct_child_text(node, "deviceName"))
            .unwrap_or_else(|| "unnamed_device".to_string());

        for key in collect_redis_key_reads_within(process_node, "process") {
            if let Some(bpm_ip) = parse_tbt_position_key_ip(&key) {
                let draft_id = format!("{}@{}:{}", bpm_ip, redis_host, redis_port);
                let entry = drafts.entry(draft_id).or_insert_with(|| DeviceDraft {
                    label: format!("{} {}", device_name, bpm_ip),
                    bpm_ip: bpm_ip.to_string(),
                    redis_host: redis_host.clone(),
                    redis_port,
                    trigger_key: None,
                    trigger_fallback_keys: BTreeSet::new(),
                    stream_keys: BTreeSet::new(),
                });
                entry.stream_keys.insert(key.clone());
                continue;
            }

            if let Some((bpm_ip, suffix)) = parse_bpm_ip_and_suffix(&key) {
                let draft_id = format!("{}@{}:{}", bpm_ip, redis_host, redis_port);
                let entry = drafts.entry(draft_id).or_insert_with(|| DeviceDraft {
                    label: format!("{} {}", device_name, bpm_ip),
                    bpm_ip: bpm_ip.to_string(),
                    redis_host: redis_host.clone(),
                    redis_port,
                    trigger_key: None,
                    trigger_fallback_keys: BTreeSet::new(),
                    stream_keys: BTreeSet::new(),
                });

                match suffix {
                    "LAST_TRIGGER_TIME" => {
                        entry.trigger_key = Some(key.clone());
                    }
                    "TRIGGER" | "LAST_TRIGGER" => {
                        entry.trigger_fallback_keys.insert(key.clone());
                    }
                    _ => {}
                }
            }
        }
    }

    let mut devices = drafts
        .into_values()
        .filter(|draft| !draft.stream_keys.is_empty())
        .map(|draft| {
            let trigger_key = draft
                .trigger_key
                .unwrap_or_else(|| format!("{{MUON:BPM:{}}}:LAST_TRIGGER_TIME", draft.bpm_ip));

            let mut fallback_keys = draft
                .trigger_fallback_keys
                .into_iter()
                .collect::<Vec<String>>();

            let fallback_trigger = format!("{{MUON:BPM:{}}}:TRIGGER", draft.bpm_ip);
            if !fallback_keys.iter().any(|key| key == &fallback_trigger) {
                fallback_keys.push(fallback_trigger);
            }

            let fallback_last_trigger = format!("{{MUON:BPM:{}}}:LAST_TRIGGER", draft.bpm_ip);
            if !fallback_keys
                .iter()
                .any(|key| key == &fallback_last_trigger)
            {
                fallback_keys.push(fallback_last_trigger);
            }

            DeviceConfig {
                label: draft.label,
                bpm_ip: draft.bpm_ip,
                redis: RedisConfig {
                    host: draft.redis_host,
                    port: draft.redis_port,
                    db: 0,
                    username: None,
                    password: None,
                },
                trigger_key,
                trigger_fallback_keys: fallback_keys,
                stream_keys: draft.stream_keys.into_iter().collect(),
            }
        })
        .collect::<Vec<_>>();

    devices.sort_by(|a, b| {
        let host_cmp = a.redis.host.cmp(&b.redis.host);
        if host_cmp != std::cmp::Ordering::Equal {
            return host_cmp;
        }
        a.bpm_ip.cmp(&b.bpm_ip)
    });

    let report = ImportReport {
        device_count: devices.len(),
        stream_count: devices.iter().map(|d| d.stream_keys.len()).sum(),
    };

    let config = MonitorConfig {
        xread_block_ms: 1_000,
        reconnect_initial_ms: 2_000,
        reconnect_max_ms: 30_000,
        min_stream_values: 1,
        injection_start_turn: 0,
        injection_window_turns: 2_048,
        sliding_window_turns: 2_048,
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
        devices,
    };

    Ok((config, report))
}

fn direct_child_text(node: Node<'_, '_>, tag_name: &str) -> Option<String> {
    node.children()
        .find(|child| child.is_element() && child.has_tag_name(tag_name))
        .and_then(|child| child.text())
        .map(str::trim)
        .filter(|text| !text.is_empty())
        .map(ToOwned::to_owned)
}

fn collect_redis_key_reads_within(scope_node: Node<'_, '_>, scope_tag: &str) -> Vec<String> {
    scope_node
        .descendants()
        .filter(|node| {
            if !node.is_element() || !node.has_tag_name("redisKeyRead") {
                return false;
            }

            node.ancestors()
                .find(|ancestor| ancestor.is_element() && ancestor.has_tag_name(scope_tag))
                .map(|ancestor| ancestor.id() == scope_node.id())
                .unwrap_or(false)
        })
        .filter_map(|node| node.text())
        .map(str::trim)
        .filter(|text| !text.is_empty())
        .map(ToOwned::to_owned)
        .collect()
}

fn parse_host_port(input: String) -> Option<(String, u16)> {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return None;
    }

    if let Some((host, port_raw)) = trimmed.rsplit_once(':') {
        if let Ok(port) = port_raw.parse::<u16>() {
            return Some((host.trim().to_string(), port));
        }
    }

    Some((trimmed.to_string(), 6379))
}

fn parse_tbt_position_key_ip(key: &str) -> Option<&str> {
    if !key.ends_with(":TBT_POSITION_SCALED") {
        return None;
    }

    let (bpm_ip, suffix) = parse_bpm_ip_and_suffix(key)?;
    if suffix.starts_with("HP") || suffix.starts_with("VP") {
        return Some(bpm_ip);
    }

    None
}

fn parse_bpm_ip_and_suffix(key: &str) -> Option<(&str, &str)> {
    if !key.starts_with(MUON_BPM_PREFIX) {
        return None;
    }

    let after_prefix = &key[MUON_BPM_PREFIX.len()..];
    let brace_idx = after_prefix.find('}')?;
    let bpm_ip = &after_prefix[..brace_idx];
    let after_brace = &after_prefix[brace_idx + 1..];

    let suffix = after_brace.strip_prefix(':')?;
    Some((bpm_ip, suffix))
}
