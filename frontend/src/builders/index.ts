/** Importing this module registers every preview builder (mirrors
 * blender/builders/__init__.py) and wires the generate-anything fallback. */
import "./streetLight";
import { buildCustom } from "./generic";
import { setCustomBuilder } from "./base";

setCustomBuilder(buildCustom);

export {
  computePrimitives,
  MATERIAL_PRESETS,
  resolveMaterial,
  specParams,
} from "./base";
export type { ResolvedMaterial } from "./base";
