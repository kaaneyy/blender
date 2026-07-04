/** Night-mode light profiles per asset usage.
 *
 * HONEST NOTE: Three.js light intensity is NOT lux. `targetLux` is a
 * representative real-world illuminance DESIGN REFERENCE shown to the user
 * (roadway lighting ~15 lux, path ~8 lux, accent ~3 lux) — not a measured
 * or simulated value. The `intensity`/`color`/`distance` are tuned visually
 * for a believable night preview. No photometric data exists in the repo. */

export interface LightProfile {
  /** warm→cool color-temperature hex sent to the Three.js light */
  color: string;
  /** Three.js point-light intensity (visual, not physical) */
  intensity: number;
  /** falloff distance in meters */
  distance: number;
  /** representative ground illuminance target, for the on-screen label only */
  targetLux: number;
  /** short usage description for the label */
  usage: string;
}

const PROFILES: Record<string, LightProfile> = {
  // roadway LED ~4000-5000K, mounted high, wide pool
  street_light: { color: "#fff0d6", intensity: 60, distance: 22, targetLux: 15, usage: "roadway" },
  // pedestrian/park post-top ~3000K, softer, closer
  pedestrian_lamp: { color: "#ffd9a8", intensity: 26, distance: 12, targetLux: 8, usage: "path" },
  // bollard accent ~2700K, low downward wash
  bollard: { color: "#ffc888", intensity: 10, distance: 5, targetLux: 3, usage: "accent" },
};

/** Profile for an emitter primitive of the given asset type. Unknown types
 * fall back to a warm point light scaled by the material's emission (the
 * schema's 0-20 "use 2-6 for lamp lenses" is the only relative-brightness
 * signal available). */
export function lightProfile(assetType: string, emission: number): LightProfile {
  const known = PROFILES[assetType];
  if (known) return known;
  const e = Math.max(1, emission);
  return {
    color: "#ffe0b0",
    intensity: 8 * e,
    distance: 4 + e,
    targetLux: Math.round(2 * e),
    usage: "fixture",
  };
}
