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

/** Replace each interior corner of a 3D polyline with a circular arc of
 * `radius` — a specified bend, the way a drawing calls one out, instead of
 * whatever a spline happens to do through the same points.
 *
 * At a corner P between neighbours A and C: the arc is tangent to both legs,
 * so it starts a tangent distance `r / tan(theta/2)` back along each (theta
 * being the interior angle at P). That distance is clamped to half of the
 * shorter leg, so a radius too large for its corner tightens instead of
 * overshooting into the neighbouring segment. Endpoints are never moved.
 *
 * Degenerate corners are left alone: a straight run has nothing to fillet,
 * and a doubled-back one has no tangent solution.
 *
 * MIRROR of `fillet_path` in blender/builders/shapes.py. */
export function filletPath(
  path: Array<[number, number, number]> | number[][],
  radius: number,
  segments = 8,
): Array<[number, number, number]> {
  const pts = path.map((p) => [p[0], p[1], p[2]] as [number, number, number]);
  if (radius <= 0 || pts.length < 3) return pts;

  const dist = (a: number[], b: number[]) => Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]);
  const out: Array<[number, number, number]> = [pts[0]];
  for (let i = 1; i < pts.length - 1; i++) {
    const a = pts[i - 1];
    const p = pts[i];
    const c = pts[i + 1];
    const l1 = dist(a, p);
    const l2 = dist(c, p);
    if (l1 < 1e-9 || l2 < 1e-9) {
      out.push(p);
      continue;
    }
    const u1 = [0, 1, 2].map((k) => (a[k] - p[k]) / l1);
    const u2 = [0, 1, 2].map((k) => (c[k] - p[k]) / l2);
    const cosT = Math.max(-1, Math.min(1, u1[0] * u2[0] + u1[1] * u2[1] + u1[2] * u2[2]));
    const theta = Math.acos(cosT);
    // straight through (theta ~ pi) or doubled back (theta ~ 0): no arc
    if (theta < 1e-6 || Math.abs(Math.PI - theta) < 1e-6) {
      out.push(p);
      continue;
    }
    const tanHalf = Math.tan(theta / 2);
    const t = Math.min(radius / tanHalf, l1 / 2, l2 / 2);
    const rEff = t * tanHalf; // the radius that distance actually buys
    const t1 = [0, 1, 2].map((k) => p[k] + u1[k] * t);
    const t2 = [0, 1, 2].map((k) => p[k] + u2[k] * t);
    // arc centre: along the corner bisector, r/sin(theta/2) from P
    const bis = [0, 1, 2].map((k) => u1[k] + u2[k]);
    const bisLen = Math.hypot(bis[0], bis[1], bis[2]);
    if (bisLen < 1e-9) {
      out.push(p);
      continue;
    }
    const b = bis.map((v) => v / bisLen);
    const d = rEff / Math.sin(theta / 2);
    const centre = [0, 1, 2].map((k) => p[k] + b[k] * d);
    const w1 = [0, 1, 2].map((k) => t1[k] - centre[k]);
    const w2 = [0, 1, 2].map((k) => t2[k] - centre[k]);
    const n1 = Math.hypot(w1[0], w1[1], w1[2]);
    const n2 = Math.hypot(w2[0], w2[1], w2[2]);
    if (n1 < 1e-9 || n2 < 1e-9) {
      out.push(p);
      continue;
    }
    const cosPhi = Math.max(
      -1,
      Math.min(1, (w1[0] * w2[0] + w1[1] * w2[1] + w1[2] * w2[2]) / (n1 * n2)),
    );
    const phi = Math.acos(cosPhi);
    if (phi < 1e-9) {
      out.push(p);
      continue;
    }
    const sinPhi = Math.sin(phi);
    for (let s = 0; s <= segments; s++) {
      const f = s / segments;
      const k1 = Math.sin((1 - f) * phi) / sinPhi;
      const k2 = Math.sin(f * phi) / sinPhi;
      out.push([
        centre[0] + w1[0] * k1 + w2[0] * k2,
        centre[1] + w1[1] * k1 + w2[1] * k2,
        centre[2] + w1[2] * k1 + w2[2] * k2,
      ]);
    }
  }
  out.push(pts[pts.length - 1]);
  return out;
}
