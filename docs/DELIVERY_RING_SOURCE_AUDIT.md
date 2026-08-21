# Delivery Ring Producer And Payload Audit

Last updated: 2026-07-16.

This note records the upstream facts relevant to the BPM tune publication. It
does not replace the captured-payload verifier and does not authorize changes to
the Delivery Ring deployment.

## Live Topology

Read-only inspection through the configured `outland` jump found:

- `drbpm2`: 30 `daqddcpnode` containers, 30 board Redis containers, 30 Python
  dataflow containers, one system Redis, and one gRPC container;
- mounted `bpm_config.json`: 30 digitizers, four single-plane devices per
  digitizer, and threshold 200 for every digitizer;
- `drbpm1`: `drbpmfe:networkstormfix` plus clock/front-end services, confirming
  that it is downstream of the raw producer rather than the capture source.

Representative `drbpm-dataflow10` provenance:

| Object | Evidence |
| --- | --- |
| Container image ID | `sha256:a06d9e3439b9360d0238aeabcc2fb7e924914a1f866f7760678bffdb06585576` |
| Container start | `2026-06-01T18:29:48.663897718Z` |
| `DRBPM_DMA_DATA.py` | `bd8a1557e387672529a64b3cce168554a2c62e05990dda05f6a90fa5fd8a4836` |
| `DeliveryRingDigitizer.py` | `01ea4a57fca63feda927b23e5bf2f0cf653671137c09f8ea7351a745d25968a6` |
| `DeliveryRingBPM.py` | `5c1fe8d62467ed63e23920fd343eb898761c64666798e6d5b6c4133f471d0d4a` |
| `bpm_config.json` | `fcd468ee4a8f9cd996ed34c8da6a5428e7ac402e6e5a9c80adc9953da0b08871` |

The two adapter files were modified on June 3, after the representative
container started. The current bind-mounted text is therefore not sufficient
to prove what its long-running Python process imported. Runtime data and logs
are used below to close the publication-relevant questions.

## Raw Versus Scaled Streams

The latest retained HP101 event on board Redis 10 had stream ID
`1781250684772-5502580000000000` for all four raw/scaled position/intensity
keys. Across its 250000 samples:

| Check | Raw | Scaled |
| --- | ---: | ---: |
| HP101 position fallback `1.01` | 0 | 221215 |
| HP101 intensity fallback `1101` | 0 | 221215 |

Of 213050 samples whose raw intensity was at or below 200, all 213050 had the
expected fallback pair in the corresponding scaled arrays. This directly
demonstrates that the resident producer writes pre-threshold values to
`TBT_POSITION_RAW` and `TBT_INTENSITY_RAW`; device-coded substitution occurs in
the scaled products. The full 250000-sample raw tail also contains nonfinite and
extreme values, consistent with the earlier finding that only the first 50000
turns are eligible for this analysis.

Recent `drbpm-dataflow30` logs repeatedly contain both:

```text
Rolling BPM: HP303 Data by -1
Rolling BPM: VP304 Data by -1
```

The current mounted implementation rolls both position and intensity arrays
before writing either raw stream. Because the process predates that file, the
logs prove the correction is active but do not by themselves reproduce every
loaded source line. Exact raw position/intensity stream IDs remain required by
the intensity verifier; a one-sample residual alignment uncertainty on two
channels cannot support an extraction-timing claim.

## Publication Gate

Run `scripts/audit_delivery_ring_payloads.py` over the two 1000-spill
position-only collections. A passing publication report
must contain exactly:

- 2000 manifests;
- 239984 captured raw position rows;
- no intensity-pair source role;
- two complete union topologies of 120 channels, 60 H plus 60 V, on 30
  digitizers with two channels per plane;
- 16 manifest-level absent position streams across 12 explicitly partial
  captures, enumerated and hash-bound in `missing_position_streams.csv`;
- zero first-50000-turn nonfinite samples, sample-count mismatches, exact
  plateaus of at least 128 turns, or repeated device-coded raw fallback pairs.

The exact partial-capture distribution is five manifests in the first
position-only collection and seven in the second. The 16 absent streams are not missing payload files: they were
omitted from manifests that already record `Partial` capture state. None of the
16 position-only absences intersects the accepted per-spill H Best-5 or V
Best-12 membership. That join is preserved by
`scripts/compare_payload_absences_to_best_n.py` with source-table and output
hashes. Publication prose therefore uses the 12 partial captures and 16
absences directly. Source payloads are read-only. A failed position audit blocks
the full-buffer ridge gallery and final poster/paper materialization.

## Retained intensity sidecar audit

The completed intensity study keeps the earlier immutable three-collection
audit: 2200 manifests, 263983 captured raw position rows, 23999 exact raw
position/intensity pairs, and 17 absences across 13 partial captures. Its exact
pair verifier, payload-horizon findings, and hashes remain valid standalone
evidence. They are not publication source roles and cannot satisfy or waive the
fresh two-collection IBIC audit.
