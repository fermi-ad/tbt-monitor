# Config Reference

This reference describes `config/monitor.cfg` keys used by runtime and analysis commands.

## Top-Level Runtime Keys

- `xread_block_ms`
  - Redis `XREAD BLOCK` timeout in ms (`0` means block indefinitely).
  - Rationale: stream-native waiting with configurable responsiveness.
- `reconnect_initial_ms`
  - Initial reconnect backoff delay.
- `reconnect_max_ms`
  - Maximum reconnect backoff delay.
- `min_stream_values`
  - Minimum values required before a stream is considered valid in monitoring views.

## Top-Level Analysis Keys

- `injection_start_turn`
  - Start index for injection-window tune extraction.
- `injection_window_turns`
  - Length of injection analysis window used only for the single representative
    injection tune estimate.
  - Recommended default practice: keep equal to `sliding_window_turns`; diverge
    only for deliberate study workflows.
- `sliding_window_turns`
  - Window length for tune-vs-time sliding analysis.
- `sliding_stride_turns`
  - Step size between sliding windows.
- `turn_period_us`
  - Conversion from turn index to physical time (`1 turn = turn_period_us` microseconds).
  - Used for time-axis rendering when time-domain plotting is enabled.
  - Default: `1.6`.
- `plot_time_axes_in_us`
  - `false` (default): use turn index on time-like axes.
  - `true`: render time-like axes in microseconds using `turn_period_us`.
  - Affects per-spill `tune_vs_time`, per-spill spectrograms, tune-validation
    panels, and composite waterfall Z-axis labels/ticks.
- `tune_plot_y_min`, `tune_plot_y_max`
  - Fixed Y-axis range for tune-valued plots (`tune_vs_time`, batch tune trend,
    tune-by-BPM, study tune panels, and composite waterfall tune axis).
  - Defaults are `0.58` to `0.74`.
- `tune_plot_y_tick_step`
  - Horizontal Y-grid spacing for `tune_vs_time`.
  - Default: `0.01`.

## Tune-Band and Confidence Keys

- `qx_band_min`, `qx_band_max`
- `qy_band_min`, `qy_band_max`
  - Search bands for horizontal/vertical tune peak detection.
- `min_peak_confidence`
  - Minimum peak-to-median spectral ratio to accept a peak.

## Peak-Tracking Keys

- `enable_peak_tracking`
  - Enables local-band continuity tracking across sliding windows.
- `qx_track_half_width`, `qy_track_half_width`
  - Half-width of local search band around previous trusted tune.
- `max_tune_step_per_window`
  - Threshold for suspicious step diagnostics.

## Synchronization Keys

- `align_tolerance_ms`
  - Legacy/live-analysis per-stream alignment tolerance for strict observation
    alignment checks.
- `same_spill_tolerance_ms`
  - Capture and DAQ diagnostics tolerance for deciding whether a stream belongs
    to the selected same-spill `target_ms`.
  - Default: `25`.
  - Exact stream timestamp deltas are still reported so millisecond-level spread
    can be trended.
- `min_aligned_fraction`
  - Minimum aligned fraction threshold for warnings/quality semantics.

## Device Section

Each `[[device]]` defines:
- identity: `label`, `bpm_ip`
- redis endpoint: `redis_host`, `redis_port`, `redis_db`, optional auth
- trigger keys: `trigger_key`, repeated `trigger_fallback`
- measured streams: repeated `stream_key` (usually `*:TBT_POSITION_SCALED`)

## Operational Guidance

- Keep `same_spill_tolerance_ms` far below the nominal event spacing. The
  default `25 ms` is intended to measure millisecond jitter while staying well
  below the 15-second spill cadence.
- Keep `align_tolerance_ms` conservative for legacy live-analysis checks.
- Tune `min_peak_confidence` by signal regime:
  - higher for stricter quality gating
  - lower when signal amplitude is weak but expected
- Keep `tune_plot_y_min/max` fixed across runs when comparing tune trends
  between datasets; this avoids visual auto-scaling bias.
- Keep `turn_period_us` aligned with machine timing assumptions so spectrogram
  time labels remain physically meaningful when `plot_time_axes_in_us=true`.
- Keep `plot_time_axes_in_us=false` unless physical time readout is needed;
  turns are the default review domain.
- Prefer explicit trigger fallback keys to preserve trigger timestamp robustness.
