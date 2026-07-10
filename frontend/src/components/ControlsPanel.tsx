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
  onWeatherAll: (value: number) => void;
  onDisplayUnits: (u: UnitSystem) => void;
  onHardware: () => void;
  onTour: () => void;
  onCheck: () => void;
  /** true = spec/code slider limits enforced; false = free dimensions */
  locked: boolean;
  onLock: () => void;
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
    key: "metalness" | "roughness" | "uv_scale" | "emission" | "weathering",
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
      {slider("weathering", "weathering", 0, 1, 0.05, resolved.weathering)}
    </div>
  );
}

function ParamControl({
  param,
  violation,
  displayUnits,
  locked,
  onParam,
}: {
  param: SpecParameter;
  violation?: CodeViolation;
  displayUnits: UnitSystem;
  locked: boolean;
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
  const specMin = round3(toDisplay(param.min ?? value / 2));
  const specMax = round3(toDisplay(param.max ?? value * 2));
  // unlocked: widen the slider well past the model's suggested limits
  const shownMin = locked ? specMin : round3(Math.min(specMin / 4, shownValue / 2));
  const shownMax = locked ? specMax : round3(Math.max(specMax * 4, shownValue * 2));
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
  onWeatherAll,
  onDisplayUnits,
  onHardware,
  onTour,
  onCheck,
  locked,
  onLock,
  onReset,
}: Props) {
  const hardwareOn =
    spec.toggles?.find((t) => t.id === "connection_hardware")?.value ?? false;
  const visibleToggles = (spec.toggles ?? []).filter(
    (t) => t.id !== "connection_hardware",
  );
  // global weathering = max of the per-slot values (a single "age it" lever)
  const globalWeather = Math.max(
    0,
    ...(spec.materials ?? []).map((m) => m.weathering ?? 0),
  );
  return (
    <div className="panel">
      <div className="panel__header">
        <h3>Parameters</h3>
        <div className="header-tools">
          <button
            className={`lock-toggle${locked ? "" : " lock-toggle--open"}`}
            onClick={onLock}
            title={
              locked
                ? "Dimensions are limited to the suggested/code ranges. Click to unlock and set any size (export will no longer auto-clamp)."
                : "Dimensions are unlocked — any size allowed, code checks are advisory only. Click to re-lock."
            }
          >
            {locked ? "🔒" : "🔓"}
          </button>
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
      </div>
      {!locked && (
        <p className="hint hint--unlock">
          Limits unlocked: sliders reach far beyond the suggested ranges and
          exports keep your exact dimensions (code violations are warnings
          only).
        </p>
      )}

      {spec.parameters.map((p) => (
        <ParamControl
          key={p.id}
          param={p}
          violation={violations[p.id]}
          displayUnits={displayUnits}
          locked={locked}
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
        title="Adds engineered bolt/nut assemblies wherever components meet — included in exports too"
      >
        🔩 {hardwareOn ? "Hide" : "Show"} bolts &amp; connections
      </button>
      <button
        className="hardware-btn"
        onClick={onTour}
        title="Fly the camera to every connection point in order, highlighting each one (turns the hardware on if needed)"
      >
        🎥 Tour the connections
      </button>
      <button
        className="hardware-btn"
        onClick={onCheck}
        title="Audit every joint like a fabricator: hardware sticking into thin air, parts that don't really touch, floating members, below-grade geometry. Fixes are proposed, previewed on hover, and applied only when you confirm."
      >
        🔍 Check connections
      </button>
      <p className="hint">
        Tip: click any part in the 3D view to edit just that part — position,
        size, and group settings.
      </p>

      {(spec.materials?.length ?? 0) > 0 && <h3>Materials</h3>}
      {(spec.materials?.length ?? 0) > 0 && (
        <div className="matrow matrow--global" title="Age the whole asset: factory-new → weathered">
          <span className="matrow__label">🌦 weather all</span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={globalWeather}
            onChange={(e) => onWeatherAll(Number(e.target.value))}
          />
          <span className="matrow__value">{globalWeather.toFixed(2)}</span>
        </div>
      )}
      {spec.materials?.map((m) => (
        <MaterialControl key={m.slot} spec={spec} material={m} onMaterial={onMaterial} />
      ))}

      <button className="reset" onClick={onReset}>
        Reset to defaults
      </button>
    </div>
  );
}
