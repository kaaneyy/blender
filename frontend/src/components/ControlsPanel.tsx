/** 100% schema-driven controls (T4.2): sliders/inputs from parameters[],
 * switches from toggles[], material dropdowns from materials[].
 * Zero per-asset UI code. Violations render red with the code citation and
 * a "snap to code" action (T4.5). */
import { useRef, useState, type ChangeEvent } from "react";
import type { AssetSpec, LengthUnit, SpecMaterial, SpecParameter, UnitSystem } from "../types";
import type { CodeViolation } from "../standards";
import { MATERIAL_PRESETS, resolveMaterial } from "../builders";
import { convert, counterpart, isLengthUnit, unitSymbol } from "../units";

/** Which length unit a parameter is DISPLAYED in for the chosen system: ft↔m,
 * in↔cm. The spec always keeps the parameter's native unit — this is pure
 * display conversion, so the validator and builders are untouched. */
function displayUnitFor(unit: LengthUnit, system: UnitSystem): LengthUnit {
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
  onCheckAI: () => void;
  onImprove: () => void;
  /** true while any AI call (the AI check or improve itself) is in flight */
  improveDisabled: boolean;
  /** true = spec/code slider limits enforced; false = free dimensions */
  locked: boolean;
  onLock: () => void;
  onReset: () => void;
  /** Spec-editor undo/redo (Ctrl+Z / Ctrl+Shift+Z, Cmd on Mac) — mirrors
   * the same history the keyboard shortcuts drive. */
  canUndo: boolean;
  canRedo: boolean;
  onUndo: () => void;
  onRedo: () => void;
  /** Downloads the current spec as JSON (Save spec). */
  onSave: () => void;
  /** Reads + adopts a picked spec JSON file (Open spec…); resolves to an
   * error message on a bad/invalid file, or null on success. */
  onOpenFile: (file: File) => Promise<string | null>;
  /** True right after app init when the current spec was restored from the
   * autosave rather than starting from the bundled default. */
  restoredNotice: boolean;
  onDismissRestoredNotice: () => void;
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
  // Length params (ft/in/m/cm/mm) convert with the ft↔m toggle and show a
  // metric-equivalent line. Dimensionless params (deg/W/x, or no unit at all
  // — counts, ratios, light power) show their symbol verbatim, never convert,
  // and have no counterpart line.
  const lengthUnit = isLengthUnit(param.unit) ? param.unit : null;
  const dispUnit = lengthUnit ? displayUnitFor(lengthUnit, displayUnits) : null;
  const toDisplay = (v: number) =>
    lengthUnit && dispUnit ? convert(v, lengthUnit, dispUnit) : v;
  const fromDisplay = (v: number) =>
    lengthUnit && dispUnit ? Number(convert(v, dispUnit, lengthUnit).toFixed(6)) : v;
  const converting = !!lengthUnit && dispUnit !== lengthUnit;
  const shownValue = round3(toDisplay(value));
  const specMin = round3(toDisplay(param.min ?? value / 2));
  const specMax = round3(toDisplay(param.max ?? value * 2));
  // unlocked: widen the slider well past the model's suggested limits
  const shownMin = locked ? specMin : round3(Math.min(specMin / 4, shownValue / 2));
  const shownMax = locked ? specMax : round3(Math.max(specMax * 4, shownValue * 2));
  const shownStep = converting ? toDisplay(param.step ?? 1) : (param.step ?? 1);

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
          <span className="control__unit">{unitSymbol(dispUnit ?? param.unit)}</span>
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
          {lengthUnit && !converting ? counterpart(value, lengthUnit) : ""}
          {converting && lengthUnit ? `${round3(value)} ${lengthUnit}` : ""}
        </span>
        {param.code_ref && <span className="control__coderef">{param.code_ref}</span>}
      </div>
      {violation && (
        <div className="violation" role="alert">
          <p>{violation.message}</p>
          <button onClick={() => onParam(param.id, violation.correctedValue)}>
            Snap to code ({violation.correctedValue} {unitSymbol(param.unit)})
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
  onCheckAI,
  onImprove,
  improveDisabled,
  locked,
  onLock,
  onReset,
  canUndo,
  canRedo,
  onUndo,
  onRedo,
  onSave,
  onOpenFile,
  restoredNotice,
  onDismissRestoredNotice,
}: Props) {
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform ?? navigator.userAgent ?? "");
  const undoShortcut = isMac ? "⌘Z" : "Ctrl+Z";
  const redoShortcut = isMac ? "⇧⌘Z" : "Ctrl+Shift+Z";
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [openError, setOpenError] = useState<string | null>(null);
  const handleFileChange = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-selecting the same file next time
    if (!file) return;
    setOpenError(null);
    const err = await onOpenFile(file);
    if (err) setOpenError(err);
  };
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
      {restoredNotice && (
        <div className="notice notice--restored" role="status">
          <p>Restored your last session — Reset to defaults discards it.</p>
          <button
            className="notice__dismiss"
            onClick={onDismissRestoredNotice}
            title="Dismiss"
          >
            ✕
          </button>
        </div>
      )}
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
      <button
        className="hardware-btn"
        onClick={onCheckAI}
        title="Ask the AI to review every joint like a fabricator — joint types vs. materials, missing declarations, assembly access. Same rules as the deterministic check: proposals preview on hover and apply only when you confirm."
      >
        🤖 Check connections with AI
      </button>
      <button
        className="hardware-btn"
        onClick={onImprove}
        disabled={improveDisabled}
        title="Runs the app's checks and an AI pass on the current asset, then adopts the improved result."
      >
        ✨ Improve
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

      {/* ── save / open (Brief: Save / Open / autosave) ── */}
      <div className="file-row">
        <button
          className="reset"
          onClick={onSave}
          title="Download this design as spec.json — the exact file the Blender export pipeline (build_cli.py / blender -b -P blender/build_cli.py) reads."
        >
          💾 Save spec
        </button>
        <button
          className="reset"
          onClick={() => fileInputRef.current?.click()}
          title="Load a spec JSON file (e.g. one from Save spec, or from examples/) back into the app."
        >
          📂 Open spec…
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".json,application/json"
          className="visually-hidden"
          onChange={handleFileChange}
        />
      </div>
      {openError && (
        <div className="violation" role="alert">
          <p>{openError}</p>
        </div>
      )}
      {/* ── end save / open ── */}

      <div className="history-row">
        <button
          className="reset"
          onClick={onUndo}
          disabled={!canUndo}
          title={`Undo (${undoShortcut})`}
        >
          ↩︎ Undo
        </button>
        <button
          className="reset"
          onClick={onRedo}
          disabled={!canRedo}
          title={`Redo (${redoShortcut} or Ctrl+Y)`}
        >
          ↪︎ Redo
        </button>
      </div>
      <button className="reset" onClick={onReset}>
        Reset to defaults
      </button>
    </div>
  );
}
