# Delivery Ring TUNE prototype

The operational data path is raw BPM Redis streams → `tbt-processor` → derived
Redis streams → the existing Delivery Ring IOC → bpm-hub-tui TUNE. The processor
is a separate headless binary; the original `tbt-monitor-tui` commands and
captured-spill format remain available.

The `delivery-ring-bpms` repository owns the source inventory, generated IOC
mapping, and `docker-compose.tune-host.yml`. The algorithm and replay executable
are built from this repository's `Dockerfile.processor`. The deployed image
must be pinned by digest; supply its source revision as the `VCS_REF` build arg.

## Shared measurement specification

`DR:BPM:TUNE:SPEC` reads an accepted `{revision, spec}` JSON envelope. To change
it, put one complete `{request_id, expected_revision, spec}` request. The processor
validates and persists the entire specification before publishing acceptance.
`SPEC_STATUS` reports the request ID, accepted/rejected state, current revision,
and reason. A successful PVA put alone does not establish acceptance. Concurrent
editors must refresh after a revision conflict; changes are never silently merged.

`DEFAULT_SPEC` and `SCHEMA` publish the initial paper profile and its JSON schema.
All analysis parameters live in the specification, including the separate
selection and trajectory stages, score weights, fitting support, tracking, and
commissioning display gates. Each job snapshots one revision. Applying settings
also requests reanalysis of the latest retained acquisition; this changes the
result generation, not its acquisition timestamp.

The initial profile enables both planes, H Best-5 and V Best-12, 4096-turn windows,
256-turn steps, eight requested fit windows, and beam search width 64. The analysis
range is `[0, min(actual_length, 50000))`. The end limit is optional and overridable.
The separate `selection_end_turn` defaults to 15,000, preserving the paper's
selection-cache and later-support interval while trajectories extend farther.
The acquired array lengths always govern available support. Short recordings can
reduce fit support or make analysis unavailable; no missing turns are invented.

## Numerical stages

Best-N searches joint subsets, rather than sorting independent BPM scores. It
uses per-window mean subtraction, symmetric Hann power, twofold FFT padding,
and the publication's float16 cache round trip. Its H/V discovery bands are
`[0.60,0.70]` and `[0.67,0.75]`. Candidates use the upper fractional tune branch
`q = 1 - f`; no expected-tune prior or external consensus is supplied.

Membership is frozen for each spill. Trajectory calculation divides each cropped
trace by its RMS before window mean subtraction, uses an unpadded FFT, and searches
H `[0.620,0.680]` / V `[0.690,0.740]`. Tracking defaults to a half-width and maximum
step of `0.005`. The later-support comparison uses the paper's purged fit/test split
and tune half-width `0.0025`; insufficient later support is missing, never zero.

Static mode uses the requested identities exactly and does not substitute a
different BPM when a member is absent. The selection and trajectory settings,
including normalization, padding, precision and bands, can be overridden
independently. Published results carry the complete effective specification.

## Quality and time semantics

Operational display gates are separate from the paper's subset score. Defaults
require finite positive spectral power, peak/median power confidence at least 2,
and median selected-member beam-supported fraction at least 95%. Beam support
uses the source's producer intensity threshold unless
`quality.beam_threshold_override` supplies an analysis-only override. Missing
intensity and synthetic/test data suppress the default primary reading. These
are commissioning defaults, not calibrated confidence probabilities or error
bars. Suppressed points remain gaps; raw candidates and rejection reasons stay
available for expert inspection.

`RESULT` is one complete JSON envelope containing both planes, exact source stream
IDs, source and processing times, configuration revision, selected memberships,
per-window power spectra, window bounds and centers, candidates, displayed
values, and quality. Nonfinite or unavailable output values use JSON `null`.
TUNE consumes this envelope rather than assembling independent scalar monitors.

`candidate_q` is the fit-selected candidate used by later-support diagnostics;
`q` is the median accepted trajectory value. Windows retain their own raw/tracked
candidates and displayed `q`. Spectra are **power**, not Mu2e's FFT magnitude.
The current Mu2e FFT processor's normalized bin frequency is not automatically
interchangeable with this instrument's fractional betatron tune.

Redis output stream IDs describe publication time. `source_time_ms` and source
stream IDs preserve acquisition provenance. Replays, reanalysis, processor
restarts, and unchanged numerical values cannot make an old acquisition fresh.
The prototype's default stale threshold is 30 seconds. Window positions are
payload-relative turns; the acquisition grouping timestamp is not a calibrated
beam-gate clock.

## Runtime boundaries

The processor only reads the acquisition Redis services. Its writes go to its
dedicated result/configuration Redis and state directory. It never changes
digitizer acquisition length, thresholds, triggers, or test mode.

One analysis runs at a time while collection, specification handling, and
heartbeats remain responsive. The newest pending acquisition supersedes older
pending work with an explicit counter; the queue cannot grow without bound.
Accepted configuration and the latest complete input are persisted atomically.
Source absence, read errors, missing members, and failed analyses must remain
visible instead of leaving an old successful result labeled current.

Qualification covers the original Python estimator, changing recording lengths,
exact static membership, missing and nonfinite data, publication identity,
restart persistence, settings conflicts, and the Redis/IOC/TUI boundary. Live
commissioning uses retained data before beam validation. Build, deployment,
verification evidence, and rollback commands belong in the operational report;
a successful local replay alone is not proof of a working live diagnostic.
