/** 100% schema-driven controls (T4.2): sliders/inputs from parameters[],
 * switches from toggles[], material dropdowns from materials[].
 * Zero per-asset UI code. Violations render red with the code citation and
 * a "snap to code" action (T4.5). */
import type { AssetSpec, SpecMaterial, SpecParameter, Unit, UnitSystem } from "../types";
import type { CodeViolation } from "../standards";
import { MATERIAL_PRESETS, resolveMaterial } from "../builders";
import { convert, counterpart } from "../units";

/** Which unit a parameter is DISPLAYED in for the chosen system: ft↔m,
 * in↔cm. The spec always keeps the parameter's native unit — this is pure
 * display conversion, so the validator and builders are untouched. */
function displayUnitFor(unit: Unit, system: UnitSystem): Unit {
  if (system === "metric") {
    if (unit === "ft") return "m";
    if (unit === "in") return "cm";
    return unit;
  }
  if (unit === "m") return "ft";
  if (unit === "cm" || unit === "mm") return "in";
  return unit;
}

const round3 = (v: number) => Number(v.toFixed(3));

interface Props {
  spec: AssetSpec;
  violations: Record<string, CodeViolation>;
  displayUnits: UnitSystem;
  onParam: (id: string, value: number | string) => void;
  onToggle: (id: string, value: boolean) => void;
  onMaterial: (slot: string, patch: Partial<SpecMaterial>) => void;
  onDisplayUnits: (u: UnitSystem) => void;
  onHardware: () => void;
  onReset: () => void;
}

/** Per-slot material editor: preset dropdown + color / reflection
 * (metalness) / roughness / UV-tiling / glow sliders. All values live in
 * the spec, so the AI can set them from prompts too. */
function MaterialControl({
  spec,
  material,
  onMaterial,
}: {
  spec: AssetSpec;
  material: SpecMaterial;
  onMaterial: Props["onMaterial"];
}) {
  const resolved = resolveMaterial(spec, material.slot);
  const slider = (
    label: string,
    key: "metalness" | "roughness" | "uv_scale" | "emission",
    min: number,
    max: number,
    step: number,
    value: number,
  ) => (
    <div className="matrow">
      <span className="matrow__label">{label}</span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onMaterial(material.slot, { [key]: Number(e.target.value) })}
      />
      <span className="matrow__value">{value}</span>
    </div>
  );

  return (
    <div className="control material">
      <div className="control__row">
        <span className="control__label">{material.slot}</span>
        <input
          type="color"
          value={resolved.color}
          onChange={(e) => onMaterial(material.slot, { color: e.target.value })}
          title="Base color"
        />
      </div>
      <select
        value={material.preset}
        onChange={(e) =>
          // switching preset clears overrides so the preset shows true
          onMaterial(material.slot, {
            preset: e.target.value,
            color: undefined,
            metalness: undefined,
            roughness: undefined,
          })
        }
      >
        {Object.keys(MATERIAL_PRESETS).map((k) => (
          <option key={k} value={k}>
            {k.replaceAll("_", " ")}
          </option>
        ))}
      </select>
      {slider("reflection", "metalness", 0, 1, 0.05, resolved.metalness)}
      {slider("roughness", "roughness", 0, 1, 0.05, resolved.roughness)}
      {slider("uv scale", "uv_scale", 0.25, 8, 0.25, resolved.uvScale)}
      {slider("glow", "emission", 0, 6, 0.25, resolved.emission)}
    </div>
  );
}

function ParamControl({
  param,
  violation,
  displayUnits,
  onParam,
}: {
  param: SpecParameter;
  violation?: CodeViolation;
  displayUnits: UnitSystem;
  onParam: Props["onParam"];
}) {
  if (param.type === "select" || typeof param.value === "string") {
    return (
      <label className="control">
        <span className="control__label">{param.label}</span>
        <select
          value={String(param.value)}
          onChange={(e) => onParam(param.id, e.target.value)}
        >
          {(param.options ?? [String(param.value)]).map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      </label>
    );
  }

  const value = param.value;
  const unit: Unit = param.unit ?? "ft";
  const dispUnit = displayUnitFor(unit, displayUnits);
  const toDisplay = (v: number) => convert(v, unit, dispUnit);
  const fromDisplay = (v: number) => Number(convert(v, dispUnit, unit).toFixed(6));
  const shownValue = round3(toDisplay(value));
  const shownMin = round3(toDisplay(param.min ?? value / 2));
  const shownMax = round3(toDisplay(param.max ?? value * 2));
  const shownStep = dispUnit === unit ? (param.step ?? 1) : toDisplay(param.step ?? 1);

  return (
    <div className={`control${violation ? " control--violation" : ""}`}>
      <div className="control__row">
        <span className="control__label" title={param.code_ref}>
          {param.label}
        </span>
        <span className="control__value">
          <input
            type="number"
            value={shownValue}
            step={shownStep}
            onChange={(e) => onParam(param.id, fromDisplay(Number(e.target.value)))}
          />
          <span className="control__unit">{dispUnit}</span>
        </span>
      </div>
      <input
        type="range"
        min={shownMin}
        max={shownMax}
        step={shownStep}
        value={shownValue}
        onChange={(e) => onParam(param.id, fromDisplay(Number(e.target.value)))}
      />
      <div className="control__meta">
        <span>
          {dispUnit === unit
            ? counterpart(value, unit)
            : `${round3(value)} ${unit}`}
        </span>
        {param.code_ref && <span className="control__coderef">{param.code_ref}</span>}
      </div>
      {violation && (
        <div className="violation" role="alert">
          <p>{violation.message}</p>
          <button onClick={() => onParam(param.id, violation.correctedValue)}>
            Snap to code ({violation.correctedValue} {param.unit})
          </button>
        </div>
      )}
    </div>
  );
}

export default function ControlsPanel({
  spec,
  violations,
  displayUnits,
  onParam,
  onToggle,
  onMaterial,
  onDisplayUnits,
  onHardware,
  onReset,
}: Props) {
  const hardwareOn =
    spec.toggles?.find((t) => t.id === "connection_hardware")?.value ?? false;
  const visibleToggles = (spec.toggles ?? []).filter(
    (t) => t.id !== "connection_hardware",
  );
  return (
    <div className="panel">
      <div className="panel__header">
        <h3>Parameters</h3>
        <div className="unit-toggle">
          {(["imperial", "metric"] as const).map((u) => (
            <button
              key={u}
              className={displayUnits === u ? "active" : ""}
              onClick={() => onDisplayUnits(u)}
            >
              {u === "imperial" ? "ft" : "m"}
            </button>
          ))}
        </div>
      </div>

      {spec.parameters.map((p) => (
        <ParamControl
          key={p.id}
          param={p}
          violation={violations[p.id]}
          displayUnits={displayUnits}
          onParam={onParam}
        />
      ))}

      <h3>Options</h3>
      {visibleToggles.map((t) => (
        <label key={t.id} className="toggle">
          <input
            type="checkbox"
            checked={t.value}
            onChange={(e) => onToggle(t.id, e.target.checked)}
          />
          {t.label}
        </label>
      ))}
      <button
        className={`hardware-btn${hardwareOn ? " hardware-btn--on" : ""}`}
        onClick={onHardware}
        title="Adds representative bolt/nut assemblies wherever components meet — included in exports too"
      >
        🔩 {hardwareOn ? "Hide" : "Show"} real bolts &amp; connections
      </button>
      <p className="hint">
        Tip: click any part in the 3D view to edit just that part — position,
        size, and group settings.
      </p>

      {(spec.materials?.length ?? 0) > 0 && <h3>Materials</h3>}
      {spec.materials?.map((m) => (
        <MaterialControl key={m.slot} spec={spec} material={m} onMaterial={onMaterial} />
      ))}

      <button className="reset" onClick={onReset}>
        Reset to defaults
      </button>
    </div>
  );
}
