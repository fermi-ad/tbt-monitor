//! Versioned shared measurement specification. Defaults are the paper profile;
//! commissioning display gates are separate from the numerical estimator.
use anyhow::{Result, bail, ensure};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct MeasurementSpec {
    pub schema_version: u32,
    pub profile: String,
    pub analysis_start_turn: usize,
    pub analysis_end_turn: Option<usize>,
    /// Selection and later-support cache limit; the paper used the first 15,000 turns.
    pub selection_end_turn: Option<usize>,
    pub turn_period_us: f64,
    pub planes: PlaneSpecs,
    pub selection: SpectralSettings,
    pub trajectory: SpectralSettings,
    pub search: SearchSettings,
    pub tracking: TrackingSettings,
    pub quality: QualitySettings,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PlaneSpecs {
    #[serde(rename = "H")]
    pub h: PlaneSpec,
    #[serde(rename = "V")]
    pub v: PlaneSpec,
}
impl PlaneSpecs {
    pub fn get(&self, plane: &str) -> Option<&PlaneSpec> {
        match plane {
            "H" => Some(&self.h),
            "V" => Some(&self.v),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PlaneSpec {
    pub enabled: bool,
    pub mode: EnsembleMode,
    pub n: usize,
    pub members: Vec<String>,
    pub selection_band: [f64; 2],
    pub trajectory_band: [f64; 2],
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum EnsembleMode {
    BestN,
    Static,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum WindowFunction {
    Hann,
    Rectangular,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Normalization {
    None,
    Rms,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CachePrecision {
    Float16,
    Float32,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SpectralSettings {
    pub window_turns: usize,
    pub step_turns: usize,
    pub padding_factor: usize,
    pub mean_subtract: bool,
    pub window: WindowFunction,
    pub normalization: Normalization,
    pub normalization_floor: f64,
    pub cache_precision: CachePrecision,
}
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SearchSettings {
    pub fit_windows: usize,
    pub beam_width: usize,
    pub tune_tolerance_bins: f64,
    pub tune_tolerance_min: f64,
    pub later_support_half_width: f64,
    pub score: ScoreSettings,
}
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ScoreSettings {
    pub prominence_scale: f64,
    pub visibility_threshold: f64,
    pub stability_scale: f64,
    pub consensus_neutral: f64,
    pub digitizer_weight: f64,
    pub ring_span_weight: f64,
    pub ring_order_scale: f64,
    pub ambiguity_ratio_start: f64,
    pub ambiguity_weight: f64,
    pub ambiguity_cap: f64,
    pub local_peak_weights: [f64; 2],
    pub peak_quality_weights: [f64; 2],
    /// Holdout, peak, consensus, visibility, diversity, finite quality.
    pub window_weights: [f64; 6],
    /// Median window score, tenth percentile, stability.
    pub aggregate_weights: [f64; 3],
    pub log_power_floor: f64,
    pub mad_scale: f64,
    pub mad_floor: f64,
}
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct TrackingSettings {
    pub enabled: bool,
    pub injection_window_turns: usize,
    pub half_width: f64,
    pub max_step: f64,
}
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct QualitySettings {
    pub min_peak_confidence: f64,
    pub require_intensity: bool,
    /// Analysis-only threshold override; null uses each source's producer threshold.
    pub beam_threshold_override: Option<f64>,
    pub min_beam_fraction: f64,
    pub suppress_synthetic: bool,
    pub stale_after_ms: u64,
}

impl Default for MeasurementSpec {
    fn default() -> Self {
        let selection = SpectralSettings {
            window_turns: 4096,
            step_turns: 256,
            padding_factor: 2,
            mean_subtract: true,
            window: WindowFunction::Hann,
            normalization: Normalization::None,
            normalization_floor: 1e-12,
            cache_precision: CachePrecision::Float16,
        };
        let mut trajectory = selection.clone();
        trajectory.padding_factor = 1;
        trajectory.normalization = Normalization::Rms;
        trajectory.cache_precision = CachePrecision::Float32;
        Self {
            schema_version: 1,
            profile: "paper-best-n-v1".into(),
            analysis_start_turn: 0,
            analysis_end_turn: Some(50000),
            selection_end_turn: Some(15000),
            turn_period_us: 1.69,
            planes: PlaneSpecs {
                h: PlaneSpec {
                    enabled: true,
                    mode: EnsembleMode::BestN,
                    n: 5,
                    members: vec![],
                    selection_band: [0.60, 0.70],
                    trajectory_band: [0.620, 0.680],
                },
                v: PlaneSpec {
                    enabled: true,
                    mode: EnsembleMode::BestN,
                    n: 12,
                    members: vec![],
                    selection_band: [0.67, 0.75],
                    trajectory_band: [0.690, 0.740],
                },
            },
            selection,
            trajectory,
            search: SearchSettings {
                fit_windows: 8,
                beam_width: 64,
                tune_tolerance_bins: 4.0,
                tune_tolerance_min: 0.002,
                later_support_half_width: 0.0025,
                score: ScoreSettings {
                    prominence_scale: 12.0,
                    visibility_threshold: 4.0,
                    stability_scale: 0.006,
                    consensus_neutral: 0.5,
                    digitizer_weight: 0.65,
                    ring_span_weight: 0.35,
                    ring_order_scale: 1000.0,
                    ambiguity_ratio_start: 0.6,
                    ambiguity_weight: 0.5,
                    ambiguity_cap: 0.25,
                    local_peak_weights: [0.75, 0.25],
                    peak_quality_weights: [0.7, 0.3],
                    window_weights: [0.35, 0.20, 0.15, 0.10, 0.10, 0.10],
                    aggregate_weights: [0.60, 0.25, 0.15],
                    log_power_floor: 1e-24,
                    mad_scale: 1.4826,
                    mad_floor: 1e-9,
                },
            },
            tracking: TrackingSettings {
                enabled: true,
                injection_window_turns: 4096,
                half_width: 0.005,
                max_step: 0.005,
            },
            quality: QualitySettings {
                min_peak_confidence: 2.0,
                require_intensity: true,
                beam_threshold_override: None,
                min_beam_fraction: 0.95,
                suppress_synthetic: true,
                stale_after_ms: 30000,
            },
        }
    }
}

impl MeasurementSpec {
    /// Bound products, not just individual knobs. Check again for every acquisition:
    /// an accepted specification may encounter a longer frame on the next trigger.
    pub fn validate_execution(
        &self,
        plane: &str,
        available_turns: usize,
        channels: usize,
    ) -> Result<()> {
        let p = self
            .planes
            .get(plane)
            .ok_or_else(|| anyhow::anyhow!("unknown plane"))?;
        if !p.enabled {
            return Ok(());
        }
        let end = available_turns.min(self.analysis_end_turn.unwrap_or(available_turns));
        let selection_end = end.min(self.selection_end_turn.unwrap_or(end));
        let count = |end: usize, s: &SpectralSettings| -> usize {
            let turns = end.saturating_sub(self.analysis_start_turn);
            if turns < s.window_turns {
                0
            } else {
                1 + (turns - s.window_turns) / s.step_turns.max(1)
            }
        };
        let bins = |s: &SpectralSettings, band: [f64; 2]| -> u128 {
            ((s.window_turns as f64 * s.padding_factor as f64 * (band[1] - band[0])).ceil() as u128)
                .saturating_add(2)
        };
        let selection_windows = count(selection_end, &self.selection) as u128;
        let selection_bins = bins(&self.selection, p.selection_band);
        let selection_cells = channels as u128 * selection_windows * selection_bins;
        ensure!(
            selection_cells <= 16_000_000,
            "{plane}: selection cache exceeds 16 million cells; shorten selection range or increase step"
        );
        let result_cells =
            count(end, &self.trajectory) as u128 * bins(&self.trajectory, p.trajectory_band);
        ensure!(
            result_cells <= 250_000,
            "{plane}: result spectra exceed 250000 cells; shorten analysis range or increase step"
        );
        let trajectory_windows = count(end, &self.trajectory) as u128;
        let trajectory_fft =
            self.trajectory.window_turns as u128 * self.trajectory.padding_factor as u128;
        ensure!(
            trajectory_windows * trajectory_fft <= 16_000_000,
            "{plane}: full trajectory FFT storage exceeds 16 million cells; reduce padding or increase step"
        );
        let selected = if p.mode == EnsembleMode::Static {
            p.members.len()
        } else {
            p.n
        };
        let selection_fft =
            self.selection.window_turns as u128 * self.selection.padding_factor as u128;
        let fft_work = |n: u128| n.saturating_mul((n as f64).log2().ceil() as u128);
        let injection_fft =
            self.tracking.injection_window_turns as u128 * self.trajectory.padding_factor as u128;
        let total_fft_work = channels as u128 * selection_windows * fft_work(selection_fft)
            + selected as u128 * trajectory_windows * fft_work(trajectory_fft)
            + selected as u128 * fft_work(injection_fft);
        ensure!(
            total_fft_work <= 2_000_000_000,
            "{plane}: FFT work exceeds execution budget; shorten ranges, reduce padding, or increase steps"
        );
        if p.mode == EnsembleMode::BestN {
            let expansions = channels as u128
                * (1 + self.search.beam_width as u128 * p.n.saturating_sub(1) as u128);
            let scoring_cells = expansions
                * selection_windows.min(self.search.fit_windows as u128)
                * selection_bins;
            ensure!(
                scoring_cells <= 2_000_000_000,
                "{plane}: subset search exceeds work budget; reduce N, fit windows, beam width, or spectral bins"
            );
        }
        Ok(())
    }

    pub fn validate(&self) -> Result<()> {
        ensure!(
            self.schema_version == 1,
            "unsupported specification schema_version"
        );
        ensure!(
            !self.profile.is_empty() && self.profile.len() <= 128,
            "profile must be 1..128 characters"
        );
        ensure!(
            self.analysis_end_turn
                .is_none_or(|v| v > self.analysis_start_turn),
            "analysis_end_turn must exceed analysis_start_turn"
        );
        ensure!(
            self.selection_end_turn
                .is_none_or(|v| v > self.analysis_start_turn),
            "selection_end_turn must exceed analysis_start_turn"
        );
        ensure!(
            self.turn_period_us.is_finite() && self.turn_period_us > 0.0,
            "turn_period_us must be positive"
        );
        for (name, s) in [
            ("selection", &self.selection),
            ("trajectory", &self.trajectory),
        ] {
            ensure!(
                (16..=262144).contains(&s.window_turns),
                "{name}.window_turns must be 16..262144"
            );
            ensure!(
                s.step_turns > 0 && s.step_turns <= 1000000,
                "{name}.step_turns must be 1..1000000"
            );
            ensure!(
                (1..=8).contains(&s.padding_factor),
                "{name}.padding_factor must be 1..8"
            );
            ensure!(
                s.normalization_floor.is_finite() && s.normalization_floor > 0.0,
                "{name}.normalization_floor must be positive"
            );
        }
        for (name, p) in [("H", &self.planes.h), ("V", &self.planes.v)] {
            ensure!((1..=60).contains(&p.n), "{name}.n must be 1..60");
            ensure!(
                p.members.len() <= 60,
                "{name}.members exceeds the ring plane inventory"
            );
            let unique: std::collections::BTreeSet<_> = p.members.iter().collect();
            ensure!(
                unique.len() == p.members.len(),
                "{name}.members contains duplicates"
            );
            for member in &p.members {
                let prefix = if name == "H" { "HP" } else { "VP" };
                ensure!(
                    member.starts_with(prefix) && member[2..].parse::<u16>().is_ok(),
                    "invalid {name} BPM identity: {member}"
                );
            }
            ensure!(
                !p.enabled || p.mode != EnsembleMode::Static || !p.members.is_empty(),
                "{name} static ensemble must contain BPMs"
            );
            for (band_name, band, fft) in [
                ("selection_band", p.selection_band, &self.selection),
                ("trajectory_band", p.trajectory_band, &self.trajectory),
            ] {
                ensure!(
                    band.iter().all(|v| v.is_finite())
                        && band[0] >= 0.5
                        && band[0] < band[1]
                        && band[1] <= 1.0,
                    "{name}.{band_name} must be increasing within upper tune branch [0.5,1]"
                );
                let n = (fft.window_turns * fft.padding_factor) as f64;
                ensure!(
                    ((1.0 - band[0]) * n).floor() - ((1.0 - band[1]) * n).ceil() >= 1.0,
                    "{name}.{band_name} must contain at least two FFT bins"
                );
            }
        }
        ensure!(
            (1..=1024).contains(&self.search.fit_windows),
            "fit_windows must be 1..1024"
        );
        ensure!(
            (1..=512).contains(&self.search.beam_width),
            "beam_width must be 1..512"
        );
        for (name, value) in [
            ("tune_tolerance_bins", self.search.tune_tolerance_bins),
            ("tune_tolerance_min", self.search.tune_tolerance_min),
            (
                "later_support_half_width",
                self.search.later_support_half_width,
            ),
            ("tracking.half_width", self.tracking.half_width),
            ("tracking.max_step", self.tracking.max_step),
        ] {
            ensure!(
                value.is_finite() && value > 0.0,
                "{name} must be finite and positive"
            );
        }
        ensure!(
            (16..=262144).contains(&self.tracking.injection_window_turns),
            "injection_window_turns must be 16..262144"
        );
        ensure!(
            self.quality.min_peak_confidence.is_finite() && self.quality.min_peak_confidence > 0.0,
            "min_peak_confidence must be positive"
        );
        ensure!(
            (0.0..=1.0).contains(&self.quality.min_beam_fraction),
            "min_beam_fraction must be in [0,1]"
        );
        ensure!(
            self.quality
                .beam_threshold_override
                .is_none_or(|v| v.is_finite() && v >= 0.0),
            "beam_threshold_override must be null or finite and nonnegative"
        );
        ensure!(
            self.quality.stale_after_ms > 0,
            "stale_after_ms must be positive"
        );
        let score = serde_json::to_value(&self.search.score)?;
        fn finite_nonnegative(v: &serde_json::Value) -> bool {
            match v {
                serde_json::Value::Number(n) => {
                    n.as_f64().is_some_and(|x| x.is_finite() && x >= 0.0)
                }
                serde_json::Value::Array(a) => a.iter().all(finite_nonnegative),
                serde_json::Value::Object(o) => o.values().all(finite_nonnegative),
                _ => false,
            }
        }
        ensure!(
            finite_nonnegative(&score),
            "score parameters must be finite and nonnegative"
        );
        let s = &self.search.score;
        if [
            s.prominence_scale,
            s.stability_scale,
            s.ring_order_scale,
            s.log_power_floor,
            s.mad_scale,
            s.mad_floor,
        ]
        .iter()
        .any(|v| *v <= 0.0)
        {
            bail!("score scale and floor parameters must be positive");
        }
        Ok(())
    }

    pub fn schema() -> serde_json::Value {
        let mut schema = serde_json::to_value(schemars::schema_for!(MeasurementSpec))
            .expect("schema is serializable");
        schema["default"] = serde_json::to_value(Self::default()).expect("default is serializable");
        schema
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct SpecRequest {
    pub request_id: String,
    pub expected_revision: u64,
    pub spec: MeasurementSpec,
}
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct AcceptedSpec {
    pub revision: u64,
    pub spec: MeasurementSpec,
}
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct SpecReceipt {
    pub request_id: String,
    pub accepted: bool,
    pub revision: u64,
    pub message: String,
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn defaults_validate_and_roundtrip() {
        let s = MeasurementSpec::default();
        s.validate().unwrap();
        let json = serde_json::to_string(&s).unwrap();
        assert_eq!(s, serde_json::from_str::<MeasurementSpec>(&json).unwrap());
    }
    #[test]
    fn invalid_band_and_members_rejected() {
        let mut s = MeasurementSpec::default();
        s.planes.h.selection_band = [0.7, 0.6];
        assert!(s.validate().is_err());
        s = MeasurementSpec::default();
        s.planes.h.mode = EnsembleMode::Static;
        s.planes.h.members = vec!["VP102".into()];
        assert!(s.validate().is_err());
    }
    #[test]
    fn unknown_parameter_rejected() {
        let mut v = serde_json::to_value(MeasurementSpec::default()).unwrap();
        v["unknown"] = true.into();
        assert!(serde_json::from_value::<MeasurementSpec>(v).is_err());
    }

    #[test]
    fn execution_products_are_bounded_for_changing_frame_lengths() {
        let mut s = MeasurementSpec::default();
        s.validate_execution("H", 300000, 60).unwrap();
        s.validate_execution("V", 300000, 60).unwrap();
        s.analysis_end_turn = None;
        s.selection_end_turn = None;
        s.selection.step_turns = 1;
        s.trajectory.step_turns = 1;
        s.validate().unwrap();
        assert!(s.validate_execution("H", 1000000, 60).is_err());
    }
}
