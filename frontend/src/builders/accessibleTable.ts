/** 1:1 port of blender/builders/accessible_table.py (pure primitive layer).
 * Any geometry change there MUST be mirrored here — dimensional parity
 * between preview and final export is a hard requirement.
 *
 * A flat top on legs whose knee/toe zone under the accessible (front) side
 * is open BY CONSTRUCTION, per ADA Standards for Accessible Design 2010
 * Section 902 (dining/work surfaces) and Section 306 (knee/toe clearance).
 * Two round legs are set back from the accessible (front, -Y) edge far
 * enough that the leg's own footprint never reaches into the ADA
 * toe-clearance strip along that edge, REGARDLESS of table_width — the
 * guarantee comes from the legs' Y position, not from keeping them apart in
 * X, so it holds at any width. A rear apron beam ties the legs together at
 * that same set-back Y, so it never intrudes on the clearance zone either.
 * Legs interpenetrate the tabletop by PENETRATION and each gets its own
 * welded joint (via connections.weldFillet) at the top's underside — the
 * same "curated builder emits its own joint hardware" pattern
 * streetLight.ts uses for its ground connection. */
import type { AssetSpec, Primitive } from "../types";
import { register, specParams } from "./base";
import { weldFillet } from "./connections";

const IN = 0.0254;

// round leg radius (m) — a substantial post, not a pencil leg
const LEG_RADIUS = 0.025;
// default/minimum tabletop thickness (m) — ~1.5 in / ~0.75 in
const TOP_THICKNESS_DEFAULT = 0.038;
const MIN_TOP_THICKNESS = 0.019;
// how far a leg is inset from the table's side (X) edges
const LEG_INSET = 0.05;
// how far a leg center sits in from the REAR (non-accessible) edge — the
// legs live back here so the whole front stays open regardless of width
const LEG_BACK_INSET = 0.06;
// legs run up INTO the tabletop by this much (a real welded joint, not a
// zero-thickness touch) — within the 10-20 mm fabrication convention
const PENETRATION = 0.015;
// weld bead size as a fraction of the leg radius (min-clamped, mirrors the
// proportions connections.groundConnection uses for its own weld_bead)
const WELD_SIZE_RATIO = 0.3;
const MIN_WELD_SIZE = 0.006;
// rear apron beam (m): vertical height and depth (Y thickness)
const APRON_HEIGHT = 0.05;
const APRON_DEPTH = 0.03;

// ADA-306.3.5 knee-clearance width (30 in) — the standards DB only carries
// the height/depth entries for accessible_table, so this is a builder
// constant (also the number the tests build the clear-zone box from).
export const KNEE_CLEARANCE_WIDTH = 0.762;

const DEFAULTS_M = {
  surface_height: 30 * IN,
  knee_clearance_height: 27 * IN,
  toe_clearance_depth: 17 * IN,
  table_width: 60 * IN,
  table_depth: 30 * IN,
};

function computeAccessibleTable(spec: AssetSpec): Primitive[] {
  const p = specParams(spec);

  const surfaceHeight = p.surface_height ?? DEFAULTS_M.surface_height;
  const kneeClearanceHeight = p.knee_clearance_height ?? DEFAULTS_M.knee_clearance_height;
  const tableWidth = p.table_width ?? DEFAULTS_M.table_width;
  const tableDepth = p.table_depth ?? DEFAULTS_M.table_depth;

  // Resolve the tension between a thin top and a low surface_height: never
  // let the underside drop below knee_clearance_height. If the requested
  // surface_height doesn't leave room for even the minimum top thickness
  // above the knee floor, raise the EFFECTIVE surface height instead of
  // violating clearance (a thin top is preferred first; raising the top
  // only kicks in once thinning bottoms out at MIN_TOP_THICKNESS).
  const effSurfaceHeight = Math.max(surfaceHeight, kneeClearanceHeight + MIN_TOP_THICKNESS);
  let topThickness = Math.min(TOP_THICKNESS_DEFAULT, effSurfaceHeight - kneeClearanceHeight);
  topThickness = Math.max(topThickness, MIN_TOP_THICKNESS);
  const topUnderside = effSurfaceHeight - topThickness;

  const halfW = tableWidth / 2;
  const halfD = tableDepth / 2;
  const legX = halfW - LEG_INSET;
  const legY = halfD - LEG_BACK_INSET; // set back near the rear edge, not the accessible front
  const legDepth = topUnderside + PENETRATION;
  const legZ = legDepth / 2;

  const prims: Primitive[] = [];

  // tabletop
  prims.push({
    kind: "box",
    name: "top",
    component: "top",
    location: [0, 0, effSurfaceHeight - topThickness / 2],
    rotation: [0, 0, 0],
    materialSlot: "top",
    params: { size: [tableWidth, tableDepth, topThickness] },
  });

  // legs + their own welded joint at the top underside
  const legPositions: Array<[number, number]> = [
    [-legX, legY],
    [legX, legY],
  ];
  const weldSize = Math.max(MIN_WELD_SIZE, LEG_RADIUS * WELD_SIZE_RATIO);
  legPositions.forEach(([lx, ly], i) => {
    const name = `leg${i + 1}`;
    prims.push({
      kind: "cylinder",
      name,
      component: "legs",
      location: [lx, ly, legZ],
      rotation: [0, 0, 0],
      materialSlot: "frame",
      params: { radius: LEG_RADIUS, depth: legDepth },
    });
    prims.push(
      weldFillet(LEG_RADIUS, weldSize, topUnderside, "legs", "frame", `${name}_weld_bead`, [lx, ly]),
    );
  });

  // rear apron beam tying the legs together — same Y as the legs, so it
  // never reaches into the accessible front's knee/toe zone
  const apronSpan = 2 * legX;
  prims.push({
    kind: "box",
    name: "apron_back",
    component: "apron",
    location: [0, legY, topUnderside - APRON_HEIGHT / 2],
    rotation: [0, 0, 0],
    materialSlot: "frame",
    params: { size: [apronSpan, APRON_DEPTH, APRON_HEIGHT] },
  });

  return prims;
}

register("accessible_table", computeAccessibleTable);
