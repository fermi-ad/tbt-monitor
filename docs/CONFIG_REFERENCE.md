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
  - Length of injection analysis window.
- `sliding_window_turns`
  - Window length for tune-vs-time sliding analysis.
- `sliding_stride_turns`
  - Step size between sliding windows.

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
  - Per-stream alignment tolerance for considering an observation aligned with `target_ms`.
- `min_aligned_fraction`
  - Minimum aligned fraction threshold for warnings/quality semantics.

## Device Section

Each `[[device]]` defines:
- identity: `label`, `bpm_ip`
- redis endpoint: `redis_host`, `redis_port`, `redis_db`, optional auth
- trigger keys: `trigger_key`, repeated `trigger_fallback`
- measured streams: repeated `stream_key` (usually `*:TBT_POSITION_SCALED`)

## Operational Guidance

- Keep `align_tolerance_ms` conservative to avoid over-merging independent events.
- Tune `min_peak_confidence` by signal regime:
  - higher for stricter quality gating
  - lower when signal amplitude is weak but expected
- Prefer explicit trigger fallback keys to preserve trigger timestamp robustness.
