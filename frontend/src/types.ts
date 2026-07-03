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
  seed?: number;
}

export type Vec3 = [number, number, number];

/** One parametric primitive in meters, Z-up, matching the Python layer. */
export interface Primitive {
  kind: "cylinder" | "cone" | "box" | "sphere";
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
  };
}
