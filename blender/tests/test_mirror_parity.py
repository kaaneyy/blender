"""Mechanical guard for the Python<->TypeScript mirror's shared numeric
constants (CLAUDE.md: "the mirror is a hard requirement"). pytest only ever
sees the Python side, so a TS-only edit to e.g. GROUND_EPS or GROUND_TOL
would otherwise ship undetected and silently desync the web preview from
the Blender export.

This test imports the real Python constant, regex-extracts the matching
`const NAME = <number>;` (or `export const NAME = <number>;`) declaration
out of the mirrored TS source file, and asserts numeric equality.

HOW TO ADD AN ENTRY
--------------------
Add a row to MIRROR_TABLE below: (python_module, python_name, ts_relpath,
ts_name). Only add a row once you have verified BY HAND that:
  1. the constant is a plain module-level number on both sides (not a
     dict/tuple/list — this file only does scalar regex matching), and
  2. it lives in a pair of files CLAUDE.md documents as mirrors of each
     other (see the table in CLAUDE.md), and
  3. the current values already agree — if they've diverged, that's a bug
     to report, not something to paper over by editing this test.
Do not add rows for Python-only modules (connectivity.py, schedule.py,
ops.py) — CLAUDE.md documents these as having no TS mirror by design, so
there is nothing to guard.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# (python_module, python_const_name, ts_file_relative_to_repo_root, ts_const_name)
MIRROR_TABLE = [
    # edits.py <-> edits.ts
    ("blender.builders.edits", "GROUND_EPS", "frontend/src/builders/edits.ts", "GROUND_EPS"),
    # audit.py <-> audit.ts
    ("blender.builders.audit", "CONTACT_TOL", "frontend/src/builders/audit.ts", "CONTACT_TOL"),
    ("blender.builders.audit", "GROUND_TOL", "frontend/src/builders/audit.ts", "GROUND_TOL"),
    ("blender.builders.audit", "EMBED_FIX", "frontend/src/builders/audit.ts", "EMBED_FIX"),
    ("blender.builders.audit", "SLIVER", "frontend/src/builders/audit.ts", "SLIVER"),
    ("blender.builders.audit", "ATTACH_TOL", "frontend/src/builders/audit.ts", "ATTACH_TOL"),
    ("blender.builders.audit", "MIN_SHADE_COVERAGE", "frontend/src/builders/audit.ts", "MIN_SHADE_COVERAGE"),
    ("blender.builders.audit", "SHADE_CLEARANCE_TOL", "frontend/src/builders/audit.ts", "SHADE_CLEARANCE_TOL"),
    # hardware.py <-> hardware.ts (joint dedupe/tolerance constants; the
    # dict-shaped constants like BOLT_CATALOG/LOAD_FACTOR are out of scope
    # for this regex-based scalar check and already have their own sync
    # test in test_schedule.py)
    ("blender.builders.hardware", "MAX_JOINTS", "frontend/src/builders/hardware.ts", "MAX_JOINTS"),
    ("blender.builders.hardware", "EMBED", "frontend/src/builders/hardware.ts", "EMBED"),
    ("blender.builders.hardware", "MIN_FACE", "frontend/src/builders/hardware.ts", "MIN_FACE"),
    ("blender.builders.hardware", "GRID", "frontend/src/builders/hardware.ts", "GRID"),
    ("blender.builders.hardware", "MERGE_TOL", "frontend/src/builders/hardware.ts", "MERGE_TOL"),
    # street_light.py <-> streetLight.ts
    ("blender.builders.street_light", "ARM_SEGMENTS", "frontend/src/builders/streetLight.ts", "ARM_SEGMENTS"),
    ("blender.builders.street_light", "ARM_RADIUS", "frontend/src/builders/streetLight.ts", "ARM_RADIUS"),
    ("blender.builders.street_light", "FT", "frontend/src/builders/streetLight.ts", "FT"),
    ("blender.builders.street_light", "IN", "frontend/src/builders/streetLight.ts", "IN"),
    ("blender.builders.street_light", "HEAD_LEN", "frontend/src/builders/streetLight.ts", "HEAD_LEN"),
    ("blender.builders.street_light", "HEAD_W", "frontend/src/builders/streetLight.ts", "HEAD_W"),
    ("blender.builders.street_light", "HEAD_H", "frontend/src/builders/streetLight.ts", "HEAD_H"),
    ("blender.builders.street_light", "NOSE_W", "frontend/src/builders/streetLight.ts", "NOSE_W"),
    ("blender.builders.street_light", "NOSE_H", "frontend/src/builders/streetLight.ts", "NOSE_H"),
    ("blender.builders.street_light", "LENS_STATION", "frontend/src/builders/streetLight.ts", "LENS_STATION"),
    ("blender.builders.street_light", "LENS_DEPTH", "frontend/src/builders/streetLight.ts", "LENS_DEPTH"),
    ("blender.builders.street_light", "LENS_RECESS", "frontend/src/builders/streetLight.ts", "LENS_RECESS"),
    # accessible_table.py <-> accessibleTable.ts
    ("blender.builders.accessible_table", "IN", "frontend/src/builders/accessibleTable.ts", "IN"),
    ("blender.builders.accessible_table", "LEG_RADIUS", "frontend/src/builders/accessibleTable.ts", "LEG_RADIUS"),
    ("blender.builders.accessible_table", "TOP_THICKNESS_DEFAULT", "frontend/src/builders/accessibleTable.ts", "TOP_THICKNESS_DEFAULT"),
    ("blender.builders.accessible_table", "MIN_TOP_THICKNESS", "frontend/src/builders/accessibleTable.ts", "MIN_TOP_THICKNESS"),
    ("blender.builders.accessible_table", "LEG_INSET", "frontend/src/builders/accessibleTable.ts", "LEG_INSET"),
    ("blender.builders.accessible_table", "LEG_BACK_INSET", "frontend/src/builders/accessibleTable.ts", "LEG_BACK_INSET"),
    ("blender.builders.accessible_table", "PENETRATION", "frontend/src/builders/accessibleTable.ts", "PENETRATION"),
    ("blender.builders.accessible_table", "WELD_SIZE_RATIO", "frontend/src/builders/accessibleTable.ts", "WELD_SIZE_RATIO"),
    ("blender.builders.accessible_table", "MIN_WELD_SIZE", "frontend/src/builders/accessibleTable.ts", "MIN_WELD_SIZE"),
    ("blender.builders.accessible_table", "APRON_HEIGHT", "frontend/src/builders/accessibleTable.ts", "APRON_HEIGHT"),
    ("blender.builders.accessible_table", "APRON_DEPTH", "frontend/src/builders/accessibleTable.ts", "APRON_DEPTH"),
    ("blender.builders.accessible_table", "KNEE_CLEARANCE_WIDTH", "frontend/src/builders/accessibleTable.ts", "KNEE_CLEARANCE_WIDTH"),
]


def _ts_const_pattern(name: str) -> "re.Pattern[str]":
    # Matches `const NAME = 0.005;` or `export const NAME = 0.005;`,
    # tolerating an optional type annotation (`const NAME: number = ...`).
    return re.compile(
        r"(?:export\s+)?const\s+" + re.escape(name) + r"\s*(?::[^=]+)?=\s*(-?\d+(?:\.\d+)?)\s*;"
    )


def _extract_ts_const(ts_path: Path, name: str) -> Optional[float]:
    text = ts_path.read_text()
    match = _ts_const_pattern(name).search(text)
    if match is None:
        return None
    return float(match.group(1))


@pytest.mark.parametrize(
    "py_module, py_name, ts_relpath, ts_name",
    MIRROR_TABLE,
    ids=[f"{mod.rsplit('.', 1)[-1]}.{name}" for mod, name, _, _ in MIRROR_TABLE],
)
def test_constant_matches_ts_mirror(py_module, py_name, ts_relpath, ts_name):
    module = importlib.import_module(py_module)
    assert hasattr(module, py_name), (
        f"{py_module} no longer defines {py_name} — update or remove this "
        f"row in MIRROR_TABLE (blender/tests/test_mirror_parity.py)"
    )
    py_value = getattr(module, py_name)

    ts_path = REPO_ROOT / ts_relpath
    assert ts_path.is_file(), f"mirror file not found: {ts_relpath}"

    ts_value = _extract_ts_const(ts_path, ts_name)
    assert ts_value is not None, (
        f"could not find `const {ts_name} = <number>;` in {ts_relpath} — "
        f"either it was renamed/removed (update MIRROR_TABLE) or it "
        f"desynced from {py_module.replace('.', '/')}.py::{py_name}"
    )

    assert py_value == pytest.approx(ts_value, abs=1e-9), (
        f"{py_module}.{py_name} = {py_value} but {ts_relpath}::{ts_name} = "
        f"{ts_value} — the Python/TypeScript mirror has desynced"
    )


def test_mirror_table_has_no_duplicate_rows():
    seen = set()
    for row in MIRROR_TABLE:
        assert row not in seen, f"duplicate MIRROR_TABLE row: {row}"
        seen.add(row)
