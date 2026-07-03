/** App shell: prompt/AI panel left, 3D viewport center, controls right.
 * Clicking a part in the viewport swaps the right panel for a focused
 * selection editor (position nudges, dimensions, group stats). Spec state
 * is the single source of truth; mesh + code checks recompute synchronously
 * on every change — the preview never waits on the server (T4.6). */
import { useEffect, useMemo, useState } from "react";
import defaultSpecJson from "../../examples/street_light.json";
import type { AssetSpec, SpecMaterial, SpecPrimitive, UnitSystem } from "./types";
import { computePrimitives } from "./builders";
import { checkSpec } from "./standards";
import ControlsPanel from "./components/ControlsPanel";
import PromptPanel from "./components/PromptPanel";
import SelectionPanel from "./components/SelectionPanel";
import Viewport from "./components/Viewport";
import type { Selection } from "./components/AssetMesh";
import "./styles.css";

const defaultSpec = defaultSpecJson as unknown as AssetSpec;

type Theme = "light" | "dark";

function initialTheme(): Theme {
  const saved = localStorage.getItem("af-theme");
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export default function App() {
  const [spec, setSpec] = useState<AssetSpec>(() => structuredClone(defaultSpec));
  const [displayUnits, setDisplayUnits] = useState<UnitSystem>(defaultSpec.units);
  const [selected, setSelected] = useState<Selection | null>(null);
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("af-theme", theme);
  }, [theme]);

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

  /** "Show real bolts & connections": ensure the connection_hardware toggle
   * exists in the spec (so it exports too), then flip it. */
  const toggleHardware = () =>
    setSpec((s) => {
      const toggles = [...(s.toggles ?? [])];
      const i = toggles.findIndex((t) => t.id === "connection_hardware");
      if (i === -1) {
        toggles.push({ id: "connection_hardware", label: "Connection Hardware", value: true });
      } else {
        toggles[i] = { ...toggles[i], value: !toggles[i].value };
      }
      return { ...s, toggles };
    });

  /** Position nudge for a component or part, stored in spec.offsets (meters). */
  const updateOffset = (key: string, axis: 0 | 1 | 2, meters: number) =>
    setSpec((s) => {
      const current: [number, number, number] = [...(s.offsets?.[key] ?? [0, 0, 0])];
      current[axis] = meters;
      return { ...s, offsets: { ...(s.offsets ?? {}), [key]: current } };
    });

  const resetOffsets = (component: string) =>
    setSpec((s) => {
      const offsets = Object.fromEntries(
        Object.entries(s.offsets ?? {}).filter(
          ([k]) => k !== component && !k.startsWith(`${component}/`),
        ),
      );
      return { ...s, offsets };
    });

  /** Direct dimension edit for custom (primitives-in-spec) assets. Only
   * plain-number params are editable; expression-driven ones stay bound to
   * their sliders. */
  const updatePrimitiveDim = (
    raw: SpecPrimitive,
    key: string,
    index: number | null,
    meters: number,
  ) =>
    setSpec((s) => ({
      ...s,
      primitives: (s.primitives ?? []).map((p) => {
        if (p !== raw && p.name !== raw.name) return p;
        const params = { ...p.params };
        if (index === null) {
          (params as Record<string, number | string>)[key] = meters;
        } else {
          const size = [...(params.size ?? [1, 1, 1])];
          size[index] = meters;
          params.size = size;
        }
        return { ...p, params };
      }),
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
    setSelected(null);
    setDisplayUnits(newSpec.units ?? "imperial");
    return null;
  };

  const isCustomAsset = Boolean(spec.primitives?.length);

  return (
    <div className="app">
      <aside className="sidebar sidebar--left">
        <PromptPanel
          spec={spec}
          violations={violations}
          theme={theme}
          onTheme={setTheme}
          onName={(name) => setSpec((s) => ({ ...s, name }))}
          onSpec={adoptSpec}
        />
      </aside>
      <main className="viewport">
        <Viewport
          spec={spec}
          primitives={primitives}
          displayUnits={displayUnits}
          theme={theme}
          selected={selected}
          onSelect={setSelected}
        />
      </main>
      <aside className="sidebar sidebar--right">
        {selected && primitives.some((p) => p.component === selected.component) ? (
          <SelectionPanel
            spec={spec}
            primitives={primitives}
            selected={selected}
            displayUnits={displayUnits}
            onSelect={setSelected}
            onOffset={updateOffset}
            onResetOffsets={resetOffsets}
            onPrimitiveDim={isCustomAsset ? updatePrimitiveDim : null}
            onClose={() => setSelected(null)}
          />
        ) : (
          <ControlsPanel
            spec={spec}
            violations={violations}
            displayUnits={displayUnits}
            onParam={updateParam}
            onToggle={updateToggle}
            onMaterial={updateMaterial}
            onDisplayUnits={setDisplayUnits}
            onHardware={toggleHardware}
            onReset={() => {
              setSpec(structuredClone(defaultSpec));
              setSelected(null);
            }}
          />
        )}
      </aside>
    </div>
  );
}
