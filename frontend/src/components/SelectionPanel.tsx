/** Focused settings for the clicked part/group: part count, measured
 * width/depth/height, per-axis position nudges (stored in spec.offsets, so
 * they survive into the Blender export), and — for custom assets — directly
 * editable primitive dimensions. */
import type { AssetSpec, Primitive, SpecPrimitive, UnitSystem } from "../types";
import type { Selection } from "./AssetMesh";
import { aabb } from "../builders";
import { convert, formatLength } from "../units";

type Axis = 0 | 1 | 2;
const AXES: Array<{ axis: Axis; label: string }> = [
  { axis: 0, label: "X" },
  { axis: 1, label: "Y" },
  { axis: 2, label: "Z (up)" },
];

function groupBounds(prims: Primitive[]) {
  const lo = [Infinity, Infinity, Infinity];
  const hi = [-Infinity, -Infinity, -Infinity];
  for (const p of prims) {
    const box = aabb(p);
    for (let k = 0; k < 3; k++) {
      lo[k] = Math.min(lo[k], box.center[k] - box.half[k]);
      hi[k] = Math.max(hi[k], box.center[k] + box.half[k]);
    }
  }
  return [hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]];
}

function OffsetEditor({
  label,
  offsetKey,
  spec,
  displayUnits,
  onOffset,
}: {
  label: string;
  offsetKey: string;
  spec: AssetSpec;
  displayUnits: UnitSystem;
  onOffset: (key: string, axis: Axis, meters: number) => void;
}) {
  const current = spec.offsets?.[offsetKey] ?? [0, 0, 0];
  const unit = displayUnits === "imperial" ? "ft" : "m";
  const step = displayUnits === "imperial" ? 0.05 : 0.01;
  return (
    <div className="offset">
      <span className="control__label">{label}</span>
      <div className="offset__row">
        {AXES.map(({ axis, label: axisLabel }) => (
          <label key={axis} className="offset__axis">
            <span>{axisLabel}</span>
            <input
              type="number"
              step={step}
              value={Number(convert(current[axis], "m", unit).toFixed(3))}
              onChange={(e) =>
                onOffset(offsetKey, axis, convert(Number(e.target.value) || 0, unit, "m"))
              }
            />
          </label>
        ))}
        <span className="control__unit">{unit}</span>
      </div>
    </div>
  );
}

function DimRow({
  label,
  meters,
  displayUnits,
  editable,
  onChange,
}: {
  label: string;
  meters: number;
  displayUnits: UnitSystem;
  editable: boolean;
  onChange?: (meters: number) => void;
}) {
  if (!editable) {
    return (
      <div className="dimrow">
        <span>{label}</span>
        <span className="dimrow__value">{formatLength(meters, displayUnits)}</span>
      </div>
    );
  }
  const unit = displayUnits === "imperial" ? "ft" : "m";
  return (
    <div className="dimrow">
      <span>{label}</span>
      <span className="dimrow__edit">
        <input
          type="number"
          step={0.01}
          min={0.001}
          value={Number(convert(meters, "m", unit).toFixed(3))}
          onChange={(e) => {
            const v = convert(Number(e.target.value), unit, "m");
            if (v > 0) onChange!(v);
          }}
        />
        <span className="control__unit">{unit}</span>
      </span>
    </div>
  );
}

export default function SelectionPanel({
  spec,
  primitives,
  selected,
  displayUnits,
  onSelect,
  onOffset,
  onResetOffsets,
  onPrimitiveDim,
  onClose,
}: {
  spec: AssetSpec;
  primitives: Primitive[];
  selected: Selection;
  displayUnits: UnitSystem;
  onSelect: (sel: Selection) => void;
  onOffset: (key: string, axis: Axis, meters: number) => void;
  onResetOffsets: (component: string) => void;
  /** null when the asset is a curated builder (dims are slider-driven). */
  onPrimitiveDim: ((raw: SpecPrimitive, key: string, index: number | null, meters: number) => void) | null;
  onClose: () => void;
}) {
  const groupPrims = primitives.filter((p) => p.component === selected.component);
  const part = selected.part
    ? groupPrims.find((p) => p.name === selected.part) ?? null
    : null;
  const [w, d, h] = groupBounds(part ? [part] : groupPrims);
  const rawPart: SpecPrimitive | undefined = part
    ? spec.primitives?.find((rp, i) => (rp.name || `part_${i + 1}`) === part.name)
    : undefined;
  const hasOffsets = Object.keys(spec.offsets ?? {}).some(
    (k) => k === selected.component || k.startsWith(`${selected.component}/`),
  );

  const dimRows = () => {
    if (!part) return null;
    const editable = (key: string, index: number | null): boolean => {
      if (!onPrimitiveDim || !rawPart) return false;
      const v = index === null
        ? rawPart.params[key as "radius"]
        : rawPart.params.size?.[index];
      return typeof v === "number";
    };
    const change = (key: string, index: number | null) => (meters: number) =>
      onPrimitiveDim!(rawPart!, key, index, meters);

    switch (part.kind) {
      case "box":
        return (
          <>
            {(["Width", "Depth", "Height"] as const).map((label, i) => (
              <DimRow
                key={label}
                label={label}
                meters={part.params.size![i]}
                displayUnits={displayUnits}
                editable={editable("size", i)}
                onChange={change("size", i)}
              />
            ))}
          </>
        );
      case "cylinder":
        return (
          <>
            <DimRow label="Radius" meters={part.params.radius!} displayUnits={displayUnits}
              editable={editable("radius", null)} onChange={change("radius", null)} />
            <DimRow label="Length" meters={part.params.depth!} displayUnits={displayUnits}
              editable={editable("depth", null)} onChange={change("depth", null)} />
          </>
        );
      case "cone":
        return (
          <>
            <DimRow label="Base radius" meters={part.params.radius_bottom!} displayUnits={displayUnits}
              editable={editable("radius_bottom", null)} onChange={change("radius_bottom", null)} />
            <DimRow label="Top radius" meters={part.params.radius_top!} displayUnits={displayUnits}
              editable={editable("radius_top", null)} onChange={change("radius_top", null)} />
            <DimRow label="Length" meters={part.params.depth!} displayUnits={displayUnits}
              editable={editable("depth", null)} onChange={change("depth", null)} />
          </>
        );
      case "sphere":
        return (
          <DimRow label="Radius" meters={part.params.radius!} displayUnits={displayUnits}
            editable={editable("radius", null)} onChange={change("radius", null)} />
        );
      case "tube":
        return (
          <>
            <DimRow label="Radius" meters={part.params.radius!} displayUnits={displayUnits}
              editable={editable("radius", null)} onChange={change("radius", null)} />
            <DimRow label="Wall" meters={part.params.wall!} displayUnits={displayUnits}
              editable={editable("wall", null)} onChange={change("wall", null)} />
            <DimRow label="Length" meters={part.params.depth!} displayUnits={displayUnits}
              editable={editable("depth", null)} onChange={change("depth", null)} />
          </>
        );
      case "sweep":
        return (
          <>
            <DimRow label="Radius" meters={part.params.radius!} displayUnits={displayUnits}
              editable={editable("radius", null)} onChange={change("radius", null)} />
            <DimRow label="Tip radius" meters={part.params.radius_end ?? part.params.radius!}
              displayUnits={displayUnits} editable={editable("radius_end", null)}
              onChange={change("radius_end", null)} />
          </>
        );
      default: // lathe / loft: overall size is in the stats box above
        return null;
    }
  };

  return (
    <div className="panel">
      <div className="panel__header">
        <h3>
          {selected.component}
          {part ? ` / ${part.name}` : ""}
        </h3>
        <button className="close" onClick={onClose} title="Back to all controls">
          ✕
        </button>
      </div>

      <div className="selection-stats">
        {!part && <div className="dimrow"><span>Parts</span><span className="dimrow__value">{groupPrims.length}</span></div>}
        {part && <div className="dimrow"><span>Shape</span><span className="dimrow__value">{part.kind}</span></div>}
        <div className="dimrow"><span>Width (X)</span><span className="dimrow__value">{formatLength(w, displayUnits)}</span></div>
        <div className="dimrow"><span>Depth (Y)</span><span className="dimrow__value">{formatLength(d, displayUnits)}</span></div>
        <div className="dimrow"><span>Height (Z)</span><span className="dimrow__value">{formatLength(h, displayUnits)}</span></div>
      </div>

      {part && (
        <>
          <h3>Dimensions</h3>
          {dimRows()}
          {!onPrimitiveDim && (
            <p className="hint">
              This part's dimensions are driven by the sliders — close this
              panel to adjust them parametrically.
            </p>
          )}
          {onPrimitiveDim && rawPart && (
            <p className="hint">
              Values shown in gray are driven by expressions/sliders; the rest
              edit the part directly.
            </p>
          )}
        </>
      )}

      <h3>Position</h3>
      <OffsetEditor
        label={`Move whole "${selected.component}" group`}
        offsetKey={selected.component}
        spec={spec}
        displayUnits={displayUnits}
        onOffset={onOffset}
      />
      {part && (
        <OffsetEditor
          label={`Move only "${part.name}"`}
          offsetKey={`${selected.component}/${part.name}`}
          spec={spec}
          displayUnits={displayUnits}
          onOffset={onOffset}
        />
      )}
      {hasOffsets && (
        <button className="reset" onClick={() => onResetOffsets(selected.component)}>
          Reset this group's position
        </button>
      )}

      <h3>Parts in group</h3>
      <div className="parts-list">
        {groupPrims.map((p) => (
          <button
            key={p.name}
            className={p.name === selected.part ? "active" : ""}
            onClick={() => onSelect({ component: selected.component, part: p.name })}
          >
            {p.name}
          </button>
        ))}
      </div>
    </div>
  );
}
