"""Joint schedule: the authoritative list of connection hardware the
generator actually placed, collected from the joint records the orchestrator
stamps on each joint's anchor primitive (Primitive.meta["joint"]).

Feeds the install guide so its Connections section cites the real fasteners,
counts, and torque values instead of inventing them — and doubles as a BOM
source. Hardware is computed with the ``connection_hardware`` toggle forced
on: the crew installs the bolts whether or not the preview showed them."""
from __future__ import annotations

import copy
from typing import List

from .base import compute_primitives


def joint_schedule(spec: dict) -> List[dict]:
    """Every generated joint's record: id, type, members, fastener, count,
    grade, torque_nm, code_ref. Empty when the spec doesn't build."""
    s = copy.deepcopy(spec)
    toggles = s.setdefault("toggles", [])
    for t in toggles:
        if t.get("id") == "connection_hardware":
            t["value"] = True
            break
    else:
        toggles.append({"id": "connection_hardware",
                        "label": "Connection Hardware", "value": True})
    try:
        prims = compute_primitives(s)
    except Exception:
        return []
    records = [
        p.meta["joint"] for p in prims
        if p.meta and isinstance(p.meta.get("joint"), dict)
    ]
    return sorted(records, key=lambda r: r.get("id", 0))
