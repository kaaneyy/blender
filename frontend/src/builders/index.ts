/** Importing this module registers every preview builder (mirrors
 * blender/builders/__init__.py) and wires the cross-cutting passes. */
import "./streetLight";
import "./accessibleTable";
import { buildCustom } from "./generic";
import { computeHardware } from "./hardware";
import { setCustomBuilder, setHardwareBuilder } from "./base";

setCustomBuilder(buildCustom);
setHardwareBuilder(computeHardware);

export {
  computePrimitives,
  preEditPrimitives,
  MATERIAL_PRESETS,
  resolveMaterial,
  weatheredShading,
  specParams,
} from "./base";
export type { ResolvedMaterial, ShadedMaterial } from "./base";
export { aabb, halfExtents } from "./hardware";
export type { Aabb } from "./hardware";
export { componentPivot } from "./edits";
export { auditConnections, applyAuditFixes } from "./audit";
export type { AuditFinding, AuditFix, AuditReport, FixOp } from "./audit";
export { computeTakeoff, solidVolume, stockCallout, materialFamily, KG_PER_LB } from "./takeoff";
export type { PartWeight, Takeoff } from "./takeoff";
