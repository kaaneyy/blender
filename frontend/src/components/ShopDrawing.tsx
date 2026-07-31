/** A shop drawing of the current asset: three orthographic views with
 * witness lines and dimensions, a per-component weight block, and the stock
 * callouts for hollow members — the same information a fabrication drawing
 * carries.
 *
 * Views are projected from each primitive's world AABB (the same `aabb()`
 * the joint auditor uses), so a part appears exactly where it is built. That
 * makes this a real orthographic projection of the model rather than a
 * decorative diagram, at the cost of drawing every part as its bounding
 * rectangle — a round pole reads as its silhouette, which is what an
 * elevation shows anyway.
 *
 * Rendered as inline SVG so it prints and exports without a canvas. */
import { useMemo } from "react";
import type { AssetSpec, Primitive, UnitSystem } from "../types";
import { aabb, computeTakeoff, stockCallout } from "../builders";
import Modal from "./Modal";

/** Which world axes a view maps onto the sheet's (x, y). */
const VIEWS = [
  { key: "front", label: "FRONT", h: 0, v: 2 },
  { key: "side", label: "SIDE", h: 1, v: 2 },
  { key: "top", label: "TOP", h: 0, v: 1 },
] as const;

const M_PER_FT = 0.3048;
const M_PER_IN = 0.0254;

/** Dimension text in the drawing's own convention: parenthesised, in inches
 * for anything under ~4 ft (like the reference drawing's 35.0 / 23.8 / 6.0),
 * feet beyond that, or metres/mm when the asset is metric. */
function dim(meters: number, units: UnitSystem): string {
  if (units === "metric") {
    return meters < 1 ? `(${Math.round(meters * 1000)})` : `(${meters.toFixed(2)} m)`;
  }
  const inches = meters / M_PER_IN;
  if (inches <= 48) return `(${inches.toFixed(1)})`;
  return `(${(meters / M_PER_FT).toFixed(1)} ft)`;
}

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
  component: string;
  name: string;
  /** For a swept member: its path in view coordinates, stroked at the tube
   * diameter. A bent tube's SHAPE is its path — drawn as a bounding
   * rectangle an inverted-U rack reads as a solid panel, which is simply
   * wrong on an elevation. Everything else stays a rectangle (a round
   * pole's silhouette IS its rectangle). */
  polyline?: Array<[number, number]>;
  stroke?: number;
}

/** Project the built parts onto one view's plane. `h`/`v` pick the world
 * axes; the vertical axis is flipped because SVG y grows downward. */
function project(prims: Primitive[], h: number, v: number): Rect[] {
  const out: Rect[] = [];
  for (const p of prims) {
    if (p.cut) continue; // negative space isn't drawn
    const { center, half } = aabb(p);
    const rect: Rect = {
      x: center[h] - half[h],
      y: -(center[v] + half[v]), // flip: SVG y grows down
      w: half[h] * 2,
      h: half[v] * 2,
      component: p.component,
      name: p.name,
    };
    if (p.kind === "sweep" && p.params.path?.length) {
      rect.polyline = p.params.path.map(
        (pt) => [p.location[h] + pt[h], -(p.location[v] + pt[v])] as [number, number],
      );
      rect.stroke = 2 * (p.params.radius ?? 0);
    }
    out.push(rect);
  }
  return out;
}

function bounds(rects: Rect[]) {
  const x0 = Math.min(...rects.map((r) => r.x));
  const x1 = Math.max(...rects.map((r) => r.x + r.w));
  const y0 = Math.min(...rects.map((r) => r.y));
  const y1 = Math.max(...rects.map((r) => r.y + r.h));
  return { x0, x1, y0, y1, w: x1 - x0, h: y1 - y0 };
}

/** One orthographic view: the parts, an overall width dimension under it and
 * an overall height dimension beside it, drawn to a shared scale. */
function View({
  title,
  rects,
  units,
  scale,
  width,
  height,
}: {
  title: string;
  rects: Rect[];
  units: UnitSystem;
  scale: number;
  width: number;
  height: number;
}) {
  if (!rects.length) return null;
  const b = bounds(rects);
  const pad = 34;
  // centre the view in its cell
  const ox = pad + (width - 2 * pad - b.w * scale) / 2 - b.x0 * scale;
  const oy = pad + (height - 2 * pad - b.h * scale) / 2 - b.y0 * scale;
  const X = (v: number) => ox + v * scale;
  const Y = (v: number) => oy + v * scale;

  const left = X(b.x0);
  const right = X(b.x1);
  const top = Y(b.y0);
  const bottom = Y(b.y1);
  const dimY = bottom + 20; // width dimension sits under the view
  const dimX = left - 18; // height dimension sits to its left

  return (
    <g>
      {rects.map((r, i) =>
        r.polyline ? (
          <polyline
            key={i}
            points={r.polyline.map(([px, py]) => `${X(px)},${Y(py)}`).join(" ")}
            className="sd__swept"
            strokeWidth={Math.max(1, (r.stroke ?? 0) * scale)}
          >
            <title>{`${r.component} / ${r.name}`}</title>
          </polyline>
        ) : (
          <rect
            key={i}
            x={X(r.x)}
            y={Y(r.y)}
            width={Math.max(0.6, r.w * scale)}
            height={Math.max(0.6, r.h * scale)}
            className="sd__part"
          >
            <title>{`${r.component} / ${r.name}`}</title>
          </rect>
        ),
      )}

      {/* overall width: witness lines down from each edge, arrowed dim line */}
      <line x1={left} y1={bottom} x2={left} y2={dimY + 5} className="sd__witness" />
      <line x1={right} y1={bottom} x2={right} y2={dimY + 5} className="sd__witness" />
      <line x1={left} y1={dimY} x2={right} y2={dimY} className="sd__dim" />
      <text x={(left + right) / 2} y={dimY - 4} className="sd__dimtext">
        {dim(b.w, units)}
      </text>

      {/* overall height, to the left */}
      <line x1={left} y1={top} x2={dimX - 5} y2={top} className="sd__witness" />
      <line x1={left} y1={bottom} x2={dimX - 5} y2={bottom} className="sd__witness" />
      <line x1={dimX} y1={top} x2={dimX} y2={bottom} className="sd__dim" />
      <text
        x={dimX - 4}
        y={(top + bottom) / 2}
        className="sd__dimtext"
        transform={`rotate(-90 ${dimX - 4} ${(top + bottom) / 2})`}
      >
        {dim(b.h, units)}
      </text>

      <text x={width / 2} y={height - 8} className="sd__viewlabel">
        {title}
      </text>
    </g>
  );
}

/** The region a DETAIL view enlarges: the feet/base — the busiest, smallest
 * feature on a street asset and the one a drawing always calls out (the
 * reference sheet's "FOOT DETAIL A"). Chosen as the parts sitting in the
 * bottom slice of the build, widened to whole parts so nothing is clipped
 * mid-body. Returns null when there is nothing distinct down there. */
function detailRegion(prims: Primitive[]): { rects: Rect[]; label: string } | null {
  const rects = project(prims, 0, 2); // same plane as the FRONT view
  if (!rects.length) return null;
  const b = bounds(rects);
  // bottom 18% of the overall height, or 300 mm, whichever is taller
  const band = Math.max(b.h * 0.18, 0.3);
  const cutoff = b.y1 - band; // remember: y grows downward
  // a part qualifies only if it FITS in the band — testing "touches the
  // band" would sweep in every full-height member, since their AABBs reach
  // the ground too, and the detail would be the whole asset again
  const inBand = rects.filter((r) => r.y >= cutoff);
  // worth a callout only if it is a genuinely small part of the whole
  if (!inBand.length || inBand.length === rects.length) return null;
  const nested = bounds(inBand);
  if (nested.h > b.h * 0.5) return null;
  const names = new Set(inBand.map((r) => r.component));
  const label = names.size === 1 ? [...names][0].toUpperCase() : "BASE";
  return { rects: inBand, label };
}

/** The circled callout on a parent view: a ring around the detailed feature
 * plus a leader out to the label, matching how a sheet points from an
 * elevation to its enlarged detail. */
function DetailBalloon({
  rects,
  detail,
  scale,
  width,
  height,
}: {
  rects: Rect[];
  detail: { rects: Rect[]; label: string };
  scale: number;
  width: number;
  height: number;
}) {
  if (!rects.length) return null;
  const b = bounds(rects);
  const d = bounds(detail.rects);
  const pad = 34;
  const ox = pad + (width - 2 * pad - b.w * scale) / 2 - b.x0 * scale;
  const oy = pad + (height - 2 * pad - b.h * scale) / 2 - b.y0 * scale;
  const cx = ox + (d.x0 + d.w / 2) * scale;
  const cy = oy + (d.y0 + d.h / 2) * scale;
  const r = Math.max(12, (Math.max(d.w, d.h) * scale) / 2 + 6);
  const lx = cx + r * 0.71 + 14;
  const ly = cy - r * 0.71 - 10;
  return (
    <g>
      <circle cx={cx} cy={cy} r={r} className="sd__balloon" />
      <line x1={cx + r * 0.71} y1={cy - r * 0.71} x2={lx} y2={ly} className="sd__dim" />
      <text x={lx + 3} y={ly + 3} className="sd__dimtext" style={{ textAnchor: "start" }}>
        A
      </text>
    </g>
  );
}

export default function ShopDrawing({
  spec,
  primitives,
  displayUnits,
  onClose,
}: {
  spec: AssetSpec;
  primitives: Primitive[];
  displayUnits: UnitSystem;
  onClose: () => void;
}) {
  const takeoff = useMemo(() => computeTakeoff(primitives, spec), [primitives, spec]);

  /** Distinct stock callouts (2.0 SQ x 0.188 wall tube, ...) — the drawing's
   * "HEAVY DUTY 0.188 WALL TUBE" note, derived rather than hand-written. */
  const stock = useMemo(() => {
    const seen = new Map<string, string>();
    for (const p of primitives) {
      const callout = stockCallout(p, displayUnits === "imperial");
      if (callout && !seen.has(callout)) seen.set(callout, p.component);
    }
    return [...seen.entries()];
  }, [primitives, displayUnits]);

  const views = useMemo(
    () => VIEWS.map((v) => ({ ...v, rects: project(primitives, v.h, v.v) })),
    [primitives],
  );

  const detail = useMemo(() => detailRegion(primitives), [primitives]);

  const CELL_W = 250;
  const CELL_H = 330;
  // one shared scale across all three views, so they read as one object
  const scale = useMemo(() => {
    // px per metre: the largest scale at which EVERY view still fits its
    // cell. Starts at Infinity so the first view sets the bar — starting at
    // 1 would silently cap the whole sheet at 1 px/m.
    let best = Infinity;
    for (const v of views) {
      if (!v.rects.length) continue;
      const b = bounds(v.rects);
      best = Math.min(best, (CELL_W - 80) / (b.w || 1), (CELL_H - 80) / (b.h || 1));
    }
    return Number.isFinite(best) && best > 0 ? best : 100;
  }, [views]);

  /** The detail cell has its own, larger scale — that's the whole point of a
   * detail: it fills the cell so small features are legible. */
  const detailScale = useMemo(() => {
    if (!detail) return scale;
    const b = bounds(detail.rects);
    return Math.min((CELL_W - 80) / (b.w || 1), (CELL_H - 80) / (b.h || 1));
  }, [detail, scale]);

  const total = displayUnits === "imperial"
    ? `${takeoff.total_lb.toFixed(1)} LB`
    : `${takeoff.total_kg.toFixed(1)} KG`;

  const download = () => {
    const svg = document.getElementById("shop-drawing-svg");
    if (!svg) return;
    const blob = new Blob([svg.outerHTML], { type: "image/svg+xml" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${spec.name || spec.asset_type}-drawing.svg`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <Modal title={`Shop drawing — ${spec.name}`} onClose={onClose}>
      <div className="shop-drawing">
        <svg
          id="shop-drawing-svg"
          className="sd"
          viewBox={`0 0 ${CELL_W * (detail ? 4 : 3)} ${CELL_H}`}
          xmlns="http://www.w3.org/2000/svg"
        >
          {views.map((v, i) => (
            <g key={v.key} transform={`translate(${i * CELL_W} 0)`}>
              <View
                title={v.label}
                rects={v.rects}
                units={displayUnits}
                scale={scale}
                width={CELL_W}
                height={CELL_H}
              />
              {/* the callout balloon on the FRONT view, ringing the feature
                  the DETAIL cell enlarges — the reference sheet's circled A */}
              {detail && v.key === "front" && <DetailBalloon rects={v.rects} detail={detail} scale={scale} width={CELL_W} height={CELL_H} />}
            </g>
          ))}
          {detail && (
            <g transform={`translate(${3 * CELL_W} 0)`}>
              <View
                title={`DETAIL A — ${detail.label}`}
                rects={detail.rects}
                units={displayUnits}
                scale={detailScale}
                width={CELL_W}
                height={CELL_H}
              />
              <text x={CELL_W / 2} y={16} className="sd__viewlabel">
                {`SCALE ${(detailScale / scale).toFixed(1)}:1`}
              </text>
            </g>
          )}
        </svg>

        <div className="sd__block">
          <div className="sd__weight">APPROXIMATE WEIGHT: {total}</div>
          <table className="sd__table">
            <tbody>
              {takeoff.by_component.map((c) => (
                <tr key={c.component}>
                  <td>{c.component}</td>
                  <td className="sd__num">
                    {displayUnits === "imperial"
                      ? `${c.lb.toFixed(1)} lb`
                      : `${c.kg.toFixed(1)} kg`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {stock.length > 0 && (
            <ul className="sd__stock">
              {stock.map(([callout, component]) => (
                <li key={callout}>
                  <strong>{component}</strong>: {callout}
                </li>
              ))}
            </ul>
          )}
          <p className="hint">
            Views are orthographic projections of the built parts; dimensions
            are overall extents. Weights are approximate — finish (bevels,
            welds) and drilled holes are not counted.
          </p>
        </div>

        <div className="modal__actions">
          <button onClick={download}>Download drawing (.svg)</button>
          <button className="secondary-btn" onClick={() => window.print()}>
            Print
          </button>
        </div>
      </div>
    </Modal>
  );
}
