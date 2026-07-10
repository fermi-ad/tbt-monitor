"""Machine-readable run contracts for resumable and sharded analyses."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

from .io import atomic_write_text


CONTRACT_SCHEMA_VERSION = 1


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def object_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_inventory_sha256(manifests: Sequence[Path], root: Path | None = None) -> str:
    inventory = []
    resolved_root = root.resolve() if root is not None else None
    for manifest in sorted(path.resolve() for path in manifests):
        try:
            label = str(manifest.relative_to(resolved_root)) if resolved_root is not None else str(manifest)
        except ValueError:
            label = str(manifest)
        inventory.append({"manifest": label, "sha256": file_sha256(manifest)})
    return object_sha256(inventory)


def materialize_contract(contract: Mapping[str, object]) -> dict[str, object]:
    return {"contract_schema_version": CONTRACT_SCHEMA_VERSION, **dict(contract)}


def ensure_run_contract(
    path: Path,
    contract: Mapping[str, object],
    protected_outputs: Sequence[Path] = (),
) -> dict[str, object]:
    """Write a contract once and reject any incompatible reuse of its output."""
    expected = materialize_contract(contract)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid run contract: {path}: {exc}") from exc
        if existing != expected:
            changed = sorted(
                key
                for key in set(existing) | set(expected)
                if existing.get(key) != expected.get(key)
            )
            raise ValueError(f"run contract mismatch for {path}; changed fields: {changed}")
        return expected
    existing_outputs = [str(output) for output in protected_outputs if output.exists()]
    if existing_outputs:
        raise ValueError(
            f"analysis outputs exist without a run contract at {path}: {existing_outputs[:5]}"
        )
    atomic_write_text(path, json.dumps(expected, indent=2, sort_keys=True) + "\n")
    return expected


def load_run_contract(path: Path) -> dict[str, object]:
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid run contract: {path}: {exc}") from exc
    if int(contract.get("contract_schema_version") or 0) != CONTRACT_SCHEMA_VERSION:
        raise ValueError(f"unsupported run contract schema: {path}")
    return contract


def compatible_shard_contracts(
    shard_contracts: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], list[int]]:
    """Require exactly one compatible contract for every declared shard."""
    if not shard_contracts:
        raise ValueError("no shard contracts supplied")
    shard_count = int(shard_contracts[0].get("shard_count") or 0)
    if shard_count < 1:
        raise ValueError("shard contract has an invalid shard_count")
    indices = [int(contract.get("shard_index") or 0) for contract in shard_contracts]
    expected_indices = list(range(shard_count))
    if sorted(indices) != expected_indices:
        raise ValueError(
            f"shard contracts do not cover exactly 0..{shard_count - 1}: {sorted(indices)}"
        )
    reference = {
        key: value
        for key, value in shard_contracts[0].items()
        if key != "shard_index"
    }
    for index, contract in zip(indices, shard_contracts):
        comparable = {key: value for key, value in contract.items() if key != "shard_index"}
        if comparable != reference:
            changed = sorted(
                key
                for key in set(reference) | set(comparable)
                if reference.get(key) != comparable.get(key)
            )
            raise ValueError(f"shard {index} run contract mismatch; changed fields: {changed}")
    return reference, sorted(indices)
