#!/usr/bin/env python3
"""Smoke tests for the standalone poster-analysis helper."""

from bpm_dgx_poster import self_test


if __name__ == "__main__":
    self_test()
    print("bpm_dgx_poster self-test passed")
