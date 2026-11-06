use super::MeasurementSpec;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SpillInput {
    pub schema_version: u32,
    pub acquisition_id: String,
    pub source_time_ms: u64,
    pub captured_at_ms: u64,
    pub synthetic: bool,
    pub traces: Vec<TraceInput>,
    #[serde(default)]
    pub warnings: Vec<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TraceInput {
    pub id: String,
    pub plane: String,
    pub digitizer: String,
    pub ring_order: f64,
    pub source_key: String,
    pub source_stream_id: String,
    pub intensity_stream_id: Option<String>,
    pub beam_threshold: f64,
    #[serde(with = "finite_samples")]
    pub samples: Vec<f32>,
    #[serde(default, with = "optional_samples")]
    pub intensity: Option<Vec<f32>>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TuneResult {
    pub schema_version: u32,
    pub algorithm: String,
    pub software_version: String,
    pub generation: u64,
    pub acquisition_id: String,
    pub source_time_ms: u64,
    pub processed_at_ms: u64,
    pub configuration_revision: u64,
    pub result_kind: String,
    pub synthetic: bool,
    pub processing_ms: u64,
    pub superseded_acquisitions: u64,
    pub effective_spec: MeasurementSpec,
    pub sources: Vec<SourceProvenance>,
    pub planes: BTreeMap<String, PlaneResult>,
    pub warnings: Vec<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SourceProvenance {
    pub id: String,
    pub source_key: String,
    pub source_stream_id: String,
    pub intensity_stream_id: Option<String>,
    pub sample_count: usize,
    pub intensity_sample_count: Option<usize>,
}
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PlaneResult {
    pub state: String,
    pub reasons: Vec<String>,
    pub mode: String,
    pub requested_n: usize,
    pub available_n: usize,
    pub selected_members: Vec<String>,
    pub available_turns: usize,
    pub used_turns: usize,
    pub fit_windows: usize,
    pub fit_end_turn: Option<usize>,
    pub test_start_turn: Option<usize>,
    pub candidate_q: Option<f64>,
    pub q: Option<f64>,
    pub score: Option<ScoreComponents>,
    pub later_support: Option<LaterSupport>,
    pub spectrum_tune: Vec<f64>,
    pub windows: Vec<WindowResult>,
}
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ScoreComponents {
    pub subset_score: f64,
    pub holdout_support: f64,
    pub peak_quality: f64,
    pub consensus_agreement: f64,
    pub window_stability: f64,
    pub diversity_score: f64,
    pub ambiguity_penalty: f64,
    pub visible_fraction: f64,
    pub visibility_duration_turns: f64,
}
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct LaterSupport {
    pub window_count: usize,
    pub q_hat: Option<f64>,
    pub blind_q_hat: Option<f64>,
    pub peak_prominence: Option<f64>,
    pub p10_peak_prominence: Option<f64>,
    pub power_support: Option<f64>,
    pub visible_fraction: Option<f64>,
    pub spectral_entropy: Option<f64>,
}
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct WindowResult {
    pub start_turn: usize,
    pub end_turn_exclusive: usize,
    pub center_turn: usize,
    pub state: String,
    pub reasons: Vec<String>,
    pub q: Option<f64>,
    pub candidate_q: Option<f64>,
    pub raw_q: Option<f64>,
    pub tracked_q: Option<f64>,
    pub confidence: Option<f64>,
    pub beam_fraction: Option<f64>,
    pub suspicious_step: bool,
    pub used_raw_fallback: bool,
    pub spectrum_power: Vec<Option<f64>>,
}

mod finite_samples {
    use serde::{Deserialize, Deserializer, Serialize, Serializer};
    pub fn serialize<S: Serializer>(v: &[f32], s: S) -> Result<S::Ok, S::Error> {
        v.iter()
            .map(|x| x.is_finite().then_some(*x))
            .collect::<Vec<_>>()
            .serialize(s)
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Vec<f32>, D::Error> {
        Ok(Vec::<Option<f32>>::deserialize(d)?
            .into_iter()
            .map(|x| x.unwrap_or(f32::NAN))
            .collect())
    }
}
mod optional_samples {
    use serde::{Deserialize, Deserializer, Serialize, Serializer};
    pub fn serialize<S: Serializer>(v: &Option<Vec<f32>>, s: S) -> Result<S::Ok, S::Error> {
        v.as_ref()
            .map(|a| {
                a.iter()
                    .map(|x| x.is_finite().then_some(*x))
                    .collect::<Vec<_>>()
            })
            .serialize(s)
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Option<Vec<f32>>, D::Error> {
        Ok(Option::<Vec<Option<f32>>>::deserialize(d)?
            .map(|a| a.into_iter().map(|x| x.unwrap_or(f32::NAN)).collect()))
    }
}
