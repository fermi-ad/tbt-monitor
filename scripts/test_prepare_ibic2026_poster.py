#!/usr/bin/env python3
"""Focused stdlib tests for the one-way IBIC 2026 poster materializer."""

from __future__ import annotations

import binascii
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import prepare_ibic2026_poster as poster_module
from prepare_ibic2026_poster import (
    ACKNOWLEDGMENT,
    GATE_SCHEMA,
    MAP_ATTRIBUTION,
    MAP_CREDIT,
    MANIFEST_SCHEMA,
    PUBLICATION_REQUIREMENTS,
    REPORT_NUMBER,
    PosterPreparationError,
    prepare_poster,
    sha256,
)


def write_png(path: Path, width: int = 600, height: int = 400) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", binascii.crc32(body))

    row = b"\x00" + b"\x33\x66\x99" * width
    pixels = row * height
    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels, 9))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class PreparePosterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.publication = self.repo / "publication" / "ibic2026"
        self.poster = self.publication / "poster"
        self.paper = self.publication / "paper"
        self.paper.mkdir(parents=True)
        (self.paper / "build").mkdir()
        (self.paper / "ABSTRACT54.tex").write_text("frozen paper\n", encoding="ascii")
        (self.paper / "build" / "ABSTRACT54.pdf").write_bytes(b"%PDF-1.7\nfrozen paper\n")

        self.sources = self.repo / "evidence"
        for name in ("best-h.png", "best-v.png", "ridge.png", "beamline.png"):
            write_png(self.sources / name)

        self.payload_path = self.publication / "results_payload.json"
        self.payload_path.write_text(
            json.dumps(self.payload(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.poster.mkdir(parents=True)
        self.gate_path = self.poster / "evidence_gate.json"
        self.write_gate()

    @staticmethod
    def payload() -> dict[str, object]:
        membership = {
            "H": {
                "plane": "H",
                "available_source_count": 60,
                "winning_source_count": 60,
                "plane_spill_count": 2000,
            },
            "V": {
                "plane": "V",
                "available_source_count": 60,
                "winning_source_count": 60,
                "plane_spill_count": 2000,
            },
        }
        null = {
            "H": {
                "plane": "H",
                "subset_size": 5,
                "status": "ok",
                "observed_agreement_rate": 0.0908,
                "null_ci_high": 0.08881,
            },
            "V": {
                "plane": "V",
                "subset_size": 12,
                "status": "ok",
                "observed_agreement_rate": 0.2628,
                "null_ci_high": 0.1832,
            },
        }
        coverage = {
            "H": {"plane": "H", "subset_size": 5, "ridge_points": 359018},
            "V": {"plane": "V", "subset_size": 12, "ridge_points": 289210},
        }
        return {
            "schema": "tbt-monitor.ibic2026-results/v2",
            "selected_sizes": {"H": 5, "V": 12},
            "cross_spill_null": {"selected": null},
            "best1_membership": {"by_plane": membership},
            "adaptive_ridge_rows": {
                "H": {"median_iqr_delta_ensemble_minus_baseline": "-0.00243472"},
                "V": {"median_iqr_delta_ensemble_minus_baseline": "0.00102365"},
            },
            "sensitivity": {
                "ranges": {
                    "H": {"minimum": 2, "maximum": 13},
                    "V": {"minimum": 10, "maximum": 28},
                }
            },
            "all_training_control": {
                "by_plane": {
                    "H": {"selected_favored": 2, "baseline_favored": 3},
                    "V": {"selected_favored": 3, "baseline_favored": 3},
                }
            },
            "primary_capture": {
                "spill_count": 2000,
                "nominal_h_channels": 60,
                "nominal_v_channels": 60,
            },
            "ridge_coverage": coverage,
        }

    def spec(self, relative: str) -> dict[str, str]:
        path = self.repo / relative
        return {"path": relative, "sha256": sha256(path)}

    def gate(self) -> dict[str, object]:
        return {
            "schema": GATE_SCHEMA,
            "paper": {
                "source": self.spec("publication/ibic2026/paper/ABSTRACT54.tex"),
                "pdf": self.spec("publication/ibic2026/paper/build/ABSTRACT54.pdf"),
            },
            "inputs": {
                "resultsPayload": self.spec("publication/ibic2026/results_payload.json"),
                "bestNH": self.spec("evidence/best-h.png"),
                "bestNV": self.spec("evidence/best-v.png"),
                "ridgeHV": self.spec("evidence/ridge.png"),
                "beamlineMap": self.spec("evidence/beamline.png"),
            },
            "mapAttribution": dict(MAP_ATTRIBUTION),
            "publicationRequirements": dict(PUBLICATION_REQUIREMENTS),
            "context": {
                "sourceDeck": {
                    "name": "Delivery Ring BPM Status",
                    "requiredAtBuild": False,
                }
            },
        }

    def write_gate(self, gate: dict[str, object] | None = None) -> None:
        self.gate_path.write_text(
            json.dumps(gate or self.gate(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_materializes_only_poster_outputs_and_preserves_frozen_paper(self) -> None:
        paper_before = {
            path.relative_to(self.repo).as_posix(): sha256(path)
            for path in (self.paper / "ABSTRACT54.tex", self.paper / "build" / "ABSTRACT54.pdf")
        }
        files_before = {path.relative_to(self.repo).as_posix() for path in self.repo.rglob("*") if path.is_file()}

        manifest = prepare_poster(self.repo)

        paper_after = {
            path.relative_to(self.repo).as_posix(): sha256(path)
            for path in (self.paper / "ABSTRACT54.tex", self.paper / "build" / "ABSTRACT54.pdf")
        }
        self.assertEqual(paper_before, paper_after)
        self.assertEqual(manifest["schema"], MANIFEST_SCHEMA)
        self.assertEqual(manifest["mapAttribution"], MAP_ATTRIBUTION)
        self.assertEqual(manifest["publicationRequirements"], PUBLICATION_REQUIREMENTS)
        self.assertTrue(all(row["unchanged"] for row in manifest["paperImmutability"].values()))
        self.assertTrue(
            all(
                row["sha256Before"] == row["sha256After"]
                for row in manifest["paperImmutability"].values()
            )
        )

        expected_new = {
            "publication/ibic2026/poster/content.json",
            "publication/ibic2026/poster/input_manifest.json",
            "publication/ibic2026/poster/assets/best_n_validation_h.png",
            "publication/ibic2026/poster/assets/best_n_validation_v.png",
            "publication/ibic2026/poster/assets/ridge_density_comparison.png",
            "publication/ibic2026/poster/assets/muon-campus-beamlines.png",
        }
        files_after = {path.relative_to(self.repo).as_posix() for path in self.repo.rglob("*") if path.is_file()}
        self.assertEqual(files_after - files_before, expected_new)
        self.assertTrue(all(path.startswith("publication/ibic2026/poster/") for path in expected_new))

        content = json.loads((self.poster / "content.json").read_text(encoding="utf-8"))
        self.assertEqual(content["title"], "Which BPMs can we trust, spill by spill?")
        self.assertEqual(
            content["subtitle"],
            "Adaptive turn-by-turn tune analysis in the Mu2e Delivery Ring",
        )
        self.assertEqual(content["methodHeading"], "THERE IS NO SINGLE BEST BPM")
        self.assertEqual(
            content["methodBody"],
            "All 60 H and 60 V sources win at least once. Choose early; test later with held-out digitizers.",
        )
        self.assertEqual(
            content["bestNHCaption"],
            "H: Best-5 reached 9.1%. The null band ends at 8.9% - promising, not decisive.",
        )
        self.assertEqual(
            content["bestNVCaption"],
            "V: Best-12 reached 26.3%. The null band ends at 18.3% - a clear separation.",
        )
        self.assertEqual(
            content["ridgeHeading"], "DOES THE CANDIDATE PERSIST FOR 50,000 TURNS?"
        )
        self.assertEqual(content["reportNumber"], REPORT_NUMBER)
        self.assertEqual(content["acknowledgment"], ACKNOWLEDGMENT)
        self.assertEqual(content["mapCredit"], MAP_CREDIT)
        self.assertEqual(
            content["mapCaption"],
            "The loop at right is the Delivery Ring. Its position readouts do not behave equally.",
        )
        self.assertEqual(
            content["conclusionBody"],
            "H Best-5 narrows the ridge. V Best-12 agrees more strongly. "
            "All-channel aggregation remains competitive.\n\n"
            "Useful operating points - not universal optima.\n\n"
            "Is this the machine tune? Not yet. Next: change the tune on purpose "
            "(a controlled quadrupole scan) and ask whether the candidate follows.",
        )
        self.assertNotRegex(json.dumps(content), r"[\u2010-\u2015\u2212\ufe58\ufe63\uff0d]")

        payload = self.payload()
        self.assertEqual(
            content["evidence"],
            {
                "best1Membership": payload["best1_membership"]["by_plane"],
                "crossSpillNull": payload["cross_spill_null"]["selected"],
                "primaryCapture": payload["primary_capture"],
                "ridgeCoverage": payload["ridge_coverage"],
            },
        )
        for source, output in (
            ("best-h.png", "best_n_validation_h.png"),
            ("best-v.png", "best_n_validation_v.png"),
            ("ridge.png", "ridge_density_comparison.png"),
            ("beamline.png", "muon-campus-beamlines.png"),
        ):
            self.assertEqual(
                (self.sources / source).read_bytes(),
                (self.poster / "assets" / output).read_bytes(),
            )

    def test_rejects_tampered_pinned_inputs_without_materializing(self) -> None:
        cases = (
            self.paper / "ABSTRACT54.tex",
            self.payload_path,
            self.sources / "best-h.png",
            self.sources / "beamline.png",
        )
        for path in cases:
            with self.subTest(path=path):
                original = path.read_bytes()
                try:
                    with path.open("ab") as handle:
                        handle.write(b"tamper")
                    with self.assertRaisesRegex(PosterPreparationError, "hash mismatch"):
                        prepare_poster(self.repo)
                    self.assertFalse((self.poster / "content.json").exists())
                    self.assertFalse((self.poster / "input_manifest.json").exists())
                finally:
                    path.write_bytes(original)

    def test_rejects_wrong_claim_relationships(self) -> None:
        payload = self.payload()
        payload["cross_spill_null"]["selected"]["H"]["observed_agreement_rate"] = 0.08
        self.payload_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.write_gate()
        with self.assertRaisesRegex(PosterPreparationError, "H observed agreement"):
            prepare_poster(self.repo)

    def test_rejects_missing_or_unconfirmed_map_permission(self) -> None:
        cases = (
            (None, "mapAttribution"),
            ({**MAP_ATTRIBUTION, "permissionStatus": "unknown"}, "attribution and permission"),
        )
        for attribution, expected_error in cases:
            with self.subTest(attribution=attribution):
                gate = self.gate()
                if attribution is None:
                    gate.pop("mapAttribution")
                else:
                    gate["mapAttribution"] = attribution
                self.write_gate(gate)
                with self.assertRaisesRegex(PosterPreparationError, expected_error):
                    prepare_poster(self.repo)

    def test_rejects_missing_or_altered_publication_requirements(self) -> None:
        cases = (
            (None, "publicationRequirements"),
            (
                {
                    **PUBLICATION_REQUIREMENTS,
                    "reportNumber": "FERMILAB-POSTER-26-0000-AD",
                },
                "poster number",
            ),
        )
        for requirements, expected_error in cases:
            with self.subTest(requirements=requirements):
                gate = self.gate()
                if requirements is None:
                    gate.pop("publicationRequirements")
                else:
                    gate["publicationRequirements"] = requirements
                self.write_gate(gate)
                with self.assertRaisesRegex(PosterPreparationError, expected_error):
                    prepare_poster(self.repo)

    def test_rejects_unsupported_all_training_claim(self) -> None:
        payload = self.payload()
        payload["all_training_control"]["by_plane"]["V"]["baseline_favored"] = 0
        self.payload_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.write_gate()
        with self.assertRaisesRegex(PosterPreparationError, "competitive-baseline claim"):
            prepare_poster(self.repo)

    def test_rejects_paper_change_during_materialization(self) -> None:
        paper_source = self.paper / "ABSTRACT54.tex"
        real_copyfile = poster_module.shutil.copyfile
        mutated = False

        def copy_then_mutate(source: Path, destination: Path) -> str:
            nonlocal mutated
            result = real_copyfile(source, destination)
            if not mutated:
                paper_source.write_text("changed during materialization\n", encoding="ascii")
                mutated = True
            return result

        with mock.patch.object(poster_module.shutil, "copyfile", side_effect=copy_then_mutate):
            with self.assertRaisesRegex(PosterPreparationError, "changed during"):
                prepare_poster(self.repo)
        self.assertFalse((self.poster / "content.json").exists())
        self.assertFalse((self.poster / "input_manifest.json").exists())

    def test_rejects_non_repo_relative_or_wrong_frozen_paths(self) -> None:
        gate = self.gate()
        gate["paper"]["source"]["path"] = "../ABSTRACT54.tex"
        self.write_gate(gate)
        with self.assertRaisesRegex(PosterPreparationError, "repo-relative"):
            prepare_poster(self.repo)


if __name__ == "__main__":
    unittest.main()
