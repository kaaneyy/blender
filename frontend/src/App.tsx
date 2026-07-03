/** App shell (T4.1): prompt panel left, 3D viewport center, schema-driven
 * controls right. Spec state is the single source of truth; the mesh and
 * the code checks recompute synchronously on every change — the preview
 * never waits on the server (T4.6). */
import { useMemo, useState } from "react";
import defaultSpecJson from "../../examples/street_light.json";
import type { AssetSpec, SpecMaterial, UnitSystem } from "./types";
import { computePrimitives } from "./builders";
import { checkSpec } from "./standards";
import ControlsPanel from "./components/ControlsPanel";
import PromptPanel from "./components/PromptPanel";
import Viewport from "./components/Viewport";
import "./styles.css";

const defaultSpec = defaultSpecJson as unknown as AssetSpec;

export default function App() {
  const [spec, setSpec] = useState<AssetSpec>(() => structuredClone(defaultSpec));
  const [displayUnits, setDisplayUnits] = useState<UnitSystem>(defaultSpec.units);

  const primitives = useMemo(() => computePrimitives(spec), [spec]);
  const violations = useMemo(() => checkSpec(spec), [spec]);

  const updateParam = (id: string, value: number | string) =>
    setSpec((s) => ({
      ...s,
      parameters: s.parameters.map((p) => (p.id === id ? { ...p, value } : p)),
    }));

  const updateToggle = (id: string, value: boolean) =>
    setSpec((s) => ({
      ...s,
      toggles: (s.toggles ?? []).map((t) => (t.id === id ? { ...t, value } : t)),
    }));

  const updateMaterial = (slot: string, patch: Partial<SpecMaterial>) =>
    setSpec((s) => ({
      ...s,
      materials: (s.materials ?? []).map((m) =>
        m.slot === slot ? { ...m, ...patch } : m,
      ),
    }));

  /** Swap in an AI-generated spec — but only if it actually builds, so a
   * bad spec can never blank the viewport. Returns an error string to show
   * in the prompt panel, or null on success. */
  const adoptSpec = (newSpec: AssetSpec): string | null => {
    try {
      computePrimitives(newSpec);
    } catch (e) {
      return `The generated spec has invalid geometry: ${
        e instanceof Error ? e.message : String(e)
      }`;
    }
    setSpec(newSpec);
    setDisplayUnits(newSpec.units ?? "imperial");
    return null;
  };

  return (
    <div className="app">
      <aside className="sidebar sidebar--left">
        <PromptPanel
          spec={spec}
          violations={violations}
          onName={(name) => setSpec((s) => ({ ...s, name }))}
          onSpec={adoptSpec}
        />
      </aside>
      <main className="viewport">
        <Viewport spec={spec} primitives={primitives} displayUnits={displayUnits} />
      </main>
      <aside className="sidebar sidebar--right">
        <ControlsPanel
          spec={spec}
          violations={violations}
          displayUnits={displayUnits}
          onParam={updateParam}
          onToggle={updateToggle}
          onMaterial={updateMaterial}
          onDisplayUnits={setDisplayUnits}
          onReset={() => setSpec(structuredClone(defaultSpec))}
        />
      </aside>
    </div>
  );
}
