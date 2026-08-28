# Contributing

Bug reports, analysis questions, and focused improvements are welcome through
GitHub issues and pull requests.

Before opening a pull request:

1. Keep changes scoped and describe any effect on capture, timing, quality, or
   output semantics.
2. Preserve incomplete and low-quality states explicitly; do not replace them
   with silent fallbacks.
3. Add or update tests for behavior changes.
4. Update the relevant user, architecture, or physics documentation.
5. Run the standard checks:

   ```bash
   cargo fmt --all -- --check
   cargo test --locked -- --nocapture
   python3 scripts/test_autosweep.py
   python3 scripts/test_best_bpm_mining.py
   ```

Publication artifacts require their focused materialization and verification
tests as described in `publication/ibic2026/README.md`.

Please avoid committing raw accelerator captures, credentials, site-local
working paths, generated review packages, or unrelated build output.
