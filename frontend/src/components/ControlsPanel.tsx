/** 100% schema-driven controls (T4.2): sliders/inputs from parameters[],
 * switches from toggles[], material dropdowns from materials[].
 * Zero per-asset UI code. Violations render red with the code citation and
 * a "snap to code" action (T4.5). */
import type { AssetSpec, SpecMaterial, SpecParameter, UnitSystem } from "../types";
import type { CodeViolation } from "../standards";
import { MATERIAL_PRESETS, resolveMaterial } from "../builders";
import { counterpart } from "../units";

interface Props {
  spec: AssetSpec;
  violations: Record<string, CodeViolation>;
  displayUnits: UnitSystem;
  onParam: (id: string, value: number | string) => void;
  onToggle: (id: string, value: boolean) => void;
  onMaterial: (slot: string, patch: Partial<SpecMaterial>) => void;
  onDisplayUnits: (u: UnitSystem) => void;
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
  onParam,
}: {
  param: SpecParameter;
  violation?: CodeViolation;
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
  return (
    <div className={`control${violation ? " control--violation" : ""}`}>
      <div className="control__row">
        <span className="control__label" title={param.code_ref}>
          {param.label}
        </span>
        <span className="control__value">
          <input
            type="number"
            value={value}
            step={param.step ?? 1}
            onChange={(e) => onParam(param.id, Number(e.target.value))}
          />
          <span className="control__unit">{param.unit ?? ""}</span>
        </span>
      </div>
      <input
        type="range"
        min={param.min ?? value / 2}
        max={param.max ?? value * 2}
        step={param.step ?? 1}
        value={value}
        onChange={(e) => onParam(param.id, Number(e.target.value))}
      />
      <div className="control__meta">
        <span>{counterpart(value, param.unit ?? "ft")}</span>
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
  onReset,
}: Props) {
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
        <ParamControl key={p.id} param={p} violation={violations[p.id]} onParam={onParam} />
      ))}

      {(spec.toggles?.length ?? 0) > 0 && <h3>Options</h3>}
      {spec.toggles?.map((t) => (
        <label key={t.id} className="toggle">
          <input
            type="checkbox"
            checked={t.value}
            onChange={(e) => onToggle(t.id, e.target.checked)}
          />
          {t.label}
        </label>
      ))}

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
