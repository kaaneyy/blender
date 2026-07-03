/** TypeScript mirror of schemas/asset_spec.schema.json and the Python
 * Primitive dataclass (blender/builders/base.py). Dimensional parity with
 * the Python builders is mandatory (T4.3); keep these in lockstep. */

export type Unit = "ft" | "in" | "m" | "cm" | "mm";
export type UnitSystem = "imperial" | "metric";

export interface SpecParameter {
  id: string;
  label: string;
  type: "slider" | "number" | "select";
  min?: number;
  max?: number;
  step?: number;
  value: number | string;
  unit?: Unit;
  options?: string[];
  code_ref?: string;
}

export interface SpecToggle {
  id: string;
  label: string;
  value: boolean;
}

export interface SpecMaterial {
  slot: string;
  preset: string;
  /** Optional per-slot overrides on top of the preset. */
  color?: string; // #rrggbb
  metalness?: number; // 0..1 (reflectivity)
  roughness?: number; // 0..1
  uv_scale?: number; // texture tiling density
  emission?: number; // glow strength
}

export type PrimitiveKind = "cylinder" | "cone" | "box" | "sphere";

/** Raw (pre-evaluation) primitive as it appears in a custom spec: numeric
 * fields may be expression strings over parameter/toggle ids. */
export interface SpecPrimitive {
  kind: PrimitiveKind;
  name?: string;
  component?: string;
  location?: Array<number | string>;
  rotation?: Array<number | string>;
  material_slot?: string;
  visible_if?: string;
  params: {
    radius?: number | string;
    depth?: number | string;
    radius_bottom?: number | string;
    radius_top?: number | string;
    size?: Array<number | string>;
    segments?: number | string;
  };
}

export interface AssetSpec {
  asset_type: string;
  name: string;
  units: UnitSystem;
  code_mode?: "strict" | "advisory";
  parameters: SpecParameter[];
  toggles?: SpecToggle[];
  materials?: SpecMaterial[];
  components?: string[];
  /** Custom parametric geometry — the "generate anything" path. */
  primitives?: SpecPrimitive[];
  /** Per-component/part position nudges in meters ('pole' or 'pole/shaft'). */
  offsets?: Record<string, [number, number, number]>;
  seed?: number;
}

export type Vec3 = [number, number, number];

/** One parametric primitive in meters, Z-up, matching the Python layer. */
export interface Primitive {
  kind: PrimitiveKind;
  name: string;
  component: string;
  location: Vec3;
  rotation: Vec3; // Euler XYZ radians, Blender convention
  materialSlot: string;
  params: {
    radius?: number;
    depth?: number;
    radius_bottom?: number;
    radius_top?: number;
    size?: Vec3;
    /** Radial segments for cylinders/cones (6 = hex bolt heads/nuts). */
    segments?: number;
  };
}
