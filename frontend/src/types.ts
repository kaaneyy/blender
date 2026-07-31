/** TypeScript mirror of schemas/asset_spec.schema.json and the Python
 * Primitive dataclass (blender/builders/base.py). Dimensional parity with
 * the Python builders is mandatory (T4.3); keep these in lockstep. */

/** Length units convert to meters in the builders and toggle ft↔m / in↔cm
 * for display. */
export type LengthUnit = "ft" | "in" | "m" | "cm" | "mm";
/** Dimensionless display units: shown verbatim as a symbol (° / W / ×),
 * never ft/m-converted, and passed through the builders unchanged (angles in
 * degrees, light power/wattage, counts & ratios). */
export type DimensionlessUnit = "deg" | "W" | "x";
export type Unit = LengthUnit | DimensionlessUnit;
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

export type ConnectionType =
  | "anchor_base"
  | "through_bolt"
  | "flange_splice"
  | "band_clamp"
  | "slip_fit"
  | "weld"
  | "carriage_bolt"
  | "lag_screw"
  | "none";

/** Declared joint intent between two components (or 'component/part' paths;
 * b may be 'ground'). Honored by the hardware generator at matching
 * contacts; geometric inference remains the fallback. */
export interface SpecConnection {
  a: string;
  b: string;
  type: ConnectionType;
  load?: "light" | "standard" | "heavy";
  count?: number;
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
  weathering?: number; // 0..1 (factory-new → aged)
  finish?: "cast" | "machined" | "sheet" | "rough";
}

export type PrimitiveKind =
  | "cylinder"
  | "cone"
  | "box"
  | "sphere"
  | "lathe"
  | "sweep"
  | "loft"
  | "tube";

export interface LoftProfile {
  shape: "rect" | "ellipse";
  w: number;
  h: number;
}

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
  /** negative space: boolean-subtracted from the component in Blender */
  cut?: boolean;
  /** linear repetition: count copies offset by step each */
  array?: { count: number | string; step: Array<number | string> };
  params: {
    radius?: number | string;
    depth?: number | string;
    radius_bottom?: number | string;
    radius_top?: number | string;
    size?: Array<number | string>;
    segments?: number | string;
    shell?: number | string;
    profile?: string | Array<Array<number | string>>;
    path?: Array<Array<number | string>>;
    radius_end?: number | string;
    wall?: number | string;
    /** kind=sweep: fillet radius applied to every interior path corner */
    bend_radius?: number | string;
    /** kind=tube: "round" (default) or "square" hollow stock (HSS) */
    section?: "round" | "square";
    profile_start?: { shape: string; w: number | string; h: number | string };
    profile_end?: { shape: string; w: number | string; h: number | string };
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
  /** Declared fabrication connections between components (see SpecConnection). */
  connections?: SpecConnection[];
  /** Per-component/part position nudges in meters ('pole' or 'pole/shaft'). */
  offsets?: Record<string, [number, number, number]>;
  /** Direct-manipulation edits from the viewport toolbar (rotate / stretch /
   * delete / duplicate). Moves live in `offsets`; these are baked in
   * computePrimitives and honored identically by the Blender export. */
  edits?: SpecEdits;
  seed?: number;
}

/** SketchUp-style edit overlay. Keys are a whole component ('pole') or a
 * single part ('pole/shaft'); part edits act about the part's own center
 * and compose with any group edit. */
export interface SpecEdits {
  /** Added Euler XYZ rotation (radians), about the target's center. */
  rotations?: Record<string, [number, number, number]>;
  /** Multiplicative per-axis scale, about the target's center. */
  scales?: Record<string, [number, number, number]>;
  /** Deleted keys: a whole component ('pole') or one part ('pole/shaft'). */
  hidden?: string[];
  /** Duplicated component groups: clone `source` as a new component `name`. */
  duplicates?: Array<{ source: string; name: string }>;
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
  /** negative space — not rendered in the preview, subtracted in Blender */
  cut?: boolean;
  /** generator metadata (e.g. the joint record on a hardware anchor prim) */
  meta?: Record<string, unknown>;
  params: {
    radius?: number;
    depth?: number;
    radius_bottom?: number;
    radius_top?: number;
    size?: Vec3;
    /** Radial segments for cylinders/cones (6 = hex bolt heads/nuts). */
    segments?: number;
    /** Wall thickness for shell parts (Blender solidify; shape-only in preview). */
    shell?: number;
    profile?: string | Array<[number, number]>;
    path?: Vec3[];
    radius_end?: number;
    wall?: number;
    /** kind=sweep: fillet radius applied to every interior path corner */
    bend_radius?: number;
    /** kind=tube: "round" (default) or "square" hollow stock (HSS) */
    section?: "round" | "square";
    profile_start?: LoftProfile;
    profile_end?: LoftProfile;
  };
}
