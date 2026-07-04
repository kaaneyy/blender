/** Mirror of blender/builders/shapes.py — named lathe profiles and loft
 * cross-section rings. Keep in exact lockstep. */

export const PROFILES_2D: Record<string, Array<[number, number]>> = {
  acorn: [
    [0.02, 0.0], [0.42, 0.02], [0.62, 0.12], [0.78, 0.3], [0.85, 0.5],
    [0.8, 0.68], [0.62, 0.85], [0.35, 0.96], [0.02, 1.0],
  ],
  teardrop: [
    [0.02, 0.0], [0.55, 0.05], [0.85, 0.25], [0.9, 0.45], [0.75, 0.68],
    [0.45, 0.88], [0.18, 0.97], [0.02, 1.0],
  ],
  dome: [
    [1.0, 0.0], [0.96, 0.3], [0.85, 0.55], [0.6, 0.8], [0.3, 0.95],
    [0.02, 1.0],
  ],
  finial: [
    [0.25, 0.0], [0.42, 0.12], [0.3, 0.3], [0.5, 0.5], [0.28, 0.72],
    [0.12, 0.85], [0.02, 1.0],
  ],
  flared_base: [
    [1.0, 0.0], [0.85, 0.15], [0.55, 0.45], [0.42, 0.75], [0.4, 1.0],
  ],
  vase: [
    [0.55, 0.0], [0.75, 0.12], [0.92, 0.35], [0.95, 0.55], [0.8, 0.75],
    [0.6, 0.9], [0.62, 1.0],
  ],
};

export function resolveProfile(
  profile: string | Array<[number, number]>,
  radius?: number,
  depth?: number,
): Array<[number, number]> {
  if (typeof profile === "string") {
    const pts = PROFILES_2D[profile];
    if (!pts) throw new Error(`Unknown profile "${profile}"`);
    if (radius === undefined || depth === undefined) {
      throw new Error(`Named profile "${profile}" needs radius and depth`);
    }
    return pts.map(([r, z]) => [r * radius, z * depth]);
  }
  return profile.map(([r, z]) => [r, z]);
}

export function ringPoints(
  shape: string,
  w: number,
  h: number,
  n = 32,
): Array<[number, number]> {
  const k = shape === "ellipse" ? 1.0 : 0.35;
  const pts: Array<[number, number]> = [];
  for (let i = 0; i < n; i++) {
    const t = (2 * Math.PI * i) / n;
    const c = Math.cos(t);
    const s = Math.sin(t);
    pts.push([
      (w / 2) * Math.sign(c) * Math.abs(c) ** k,
      (h / 2) * Math.sign(s) * Math.abs(s) ** k,
    ]);
  }
  return pts;
}

export function profileBounds(
  points: Array<[number, number]>,
): [number, number, number] {
  const maxR = Math.max(...points.map(([r]) => Math.abs(r)));
  const zs = points.map(([, z]) => z);
  return [maxR, Math.min(...zs), Math.max(...zs)];
}
