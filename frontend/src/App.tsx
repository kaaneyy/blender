/** App shell: prompt/AI panel left, 3D viewport center, controls right.
 * Clicking a part in the viewport swaps the right panel for a focused
 * selection editor (position nudges, dimensions, group stats). Spec state
 * is the single source of truth; mesh + code checks recompute synchronously
 * on every change — the preview never waits on the server (T4.6). */
import { useEffect, useMemo, useState } from "react";
import defaultSpecJson from "../../examples/street_light.json";
import type { AssetSpec, Primitive, SpecMaterial, SpecPrimitive, UnitSystem, Vec3 } from "./types";
import { applyAuditFixes, auditConnections, computePrimitives } from "./builders";
import type { AuditFinding } from "./builders";
import { checkSpec } from "./standards";
import CheckPanel from "./components/CheckPanel";
import ControlsPanel from "./components/ControlsPanel";
import PromptPanel from "./components/PromptPanel";
import SelectionPanel from "./components/SelectionPanel";
import Viewport, { type CommittedTransform } from "./components/Viewport";
import type { Selection } from "./components/AssetMesh";
import "./styles.css";

const defaultSpec = defaultSpecJson as unknown as AssetSpec;

type Theme = "light" | "dark";

function initialTheme(): Theme {
  const saved = localStorage.getItem("af-theme");
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** Components whose parts moved, appeared, or vanished between two builds —
 * what the fix preview highlights so the user sees exactly what changes. */
function changedComponents(before: Primitive[], after: Primitive[]): Set<string> {
  const key = (p: Primitive) => `${p.component}/${p.name}`;
  const prev = new Map(before.map((p) => [key(p), p]));
  const out = new Set<string>();
  for (const p of after) {
    const q = prev.get(key(p));
    if (!q || q.location.some((v, k) => Math.abs(v - p.location[k]) > 1e-9)) {
      out.add(p.component);
    }
    prev.delete(key(p));
  }
  for (const p of prev.values()) out.add(p.component); // removed parts
  return out;
}

export default function App() {
  const [spec, setSpec] = useState<AssetSpec>(() => structuredClone(defaultSpec));
  const [displayUnits, setDisplayUnits] = useState<UnitSystem>(defaultSpec.units);
  const [selected, setSelected] = useState<Selection | null>(null);
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [tourId, setTourId] = useState(0);
  const [homeId, setHomeId] = useState(0);

  // ── connection check: report, per-finding selection, hover preview ──
  const [checkOpen, setCheckOpen] = useState(false);
  /** finding ids the user UNchecked (default = every fixable finding on) */
  const [excludedFixes, setExcludedFixes] = useState<Set<string>>(new Set());
  const [previewFixes, setPreviewFixes] = useState(false);
  const [hoverFinding, setHoverFinding] = useState<AuditFinding | null>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("af-theme", theme);
  }, [theme]);

  const primitives = useMemo(() => computePrimitives(spec), [spec]);
  const violations = useMemo(() => checkSpec(spec), [spec]);

  // the audit re-runs live while the panel is open, so the report always
  // matches the current sliders/edits — applying is still click-only
  const report = useMemo(
    () => (checkOpen ? auditConnections(spec) : null),
    [checkOpen, spec],
  );
  const fixable = useMemo(
    () => (report ? report.findings.filter((f) => f.fix && !excludedFixes.has(f.id)) : []),
    [report, excludedFixes],
  );
  const fixedSpec = useMemo(
    () => (fixable.length ? applyAuditFixes(spec, fixable) : null),
    [spec, fixable],
  );
  const fixedPrimitives = useMemo(
    () => (fixedSpec ? computePrimitives(fixedSpec) : null),
    [fixedSpec],
  );
  const previewing = previewFixes && fixedSpec !== null && fixedPrimitives !== null;
  // highlight what the hovered fix preview changes, or the hovered finding
  const flash = useMemo(() => {
    if (previewing) return changedComponents(primitives, fixedPrimitives!);
    const out = new Set<string>();
    if (hoverFinding?.component) out.add(hoverFinding.component);
    if (hoverFinding?.joint != null) out.add(`joint:${hoverFinding.joint}`);
    return out;
  }, [previewing, primitives, fixedPrimitives, hoverFinding]);

  /** Open the connection check: make hardware visible (the audit inspects
   * it, so the user should see it too), then show the report panel. */
  const startCheck = () => {
    setSpec((s) => {
      const toggles = [...(s.toggles ?? [])];
      const i = toggles.findIndex((t) => t.id === "connection_hardware");
      if (i === -1) {
        toggles.push({ id: "connection_hardware", label: "Connection Hardware", value: true });
      } else if (!toggles[i].value) {
        toggles[i] = { ...toggles[i], value: true };
      }
      return { ...s, toggles };
    });
    setExcludedFixes(new Set());
    setPreviewFixes(false);
    setCheckOpen(true);
  };

  /** The confirmed apply — the ONLY place audit fixes reach the spec. The
   * report then recomputes on the fixed spec, so remaining findings (and
   * any deferred fixes) surface for the next round. */
  const applyFixes = () => {
    if (!fixedSpec) return;
    setSpec(fixedSpec);
    setExcludedFixes(new Set());
    setPreviewFixes(false);
  };

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

  /** Global weathering: set the same aging value on every material slot. */
  const weatherAll = (value: number) =>
    setSpec((s) => ({
      ...s,
      materials: (s.materials ?? []).map((m) => ({ ...m, weathering: value })),
    }));

  /** "Show/hide bolts & connections": ensure the connection_hardware toggle
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

  /** "Tour the connections": make sure hardware is visible, then run the
   * camera tour (each joint highlighted as the camera visits it). */
  const startTour = () => {
    setSpec((s) => {
      const toggles = [...(s.toggles ?? [])];
      const i = toggles.findIndex((t) => t.id === "connection_hardware");
      if (i === -1) {
        toggles.push({ id: "connection_hardware", label: "Connection Hardware", value: true });
      } else if (!toggles[i].value) {
        toggles[i] = { ...toggles[i], value: true };
      }
      return { ...s, toggles };
    });
    setTourId((t) => t + 1);
  };

  /** Dimension lock: locked (default) keeps the spec's slider limits and
   * strict code clamping on export; unlocked switches the spec to advisory
   * mode and widens the slider ranges so any dimension can be dialed in. */
  const locked = spec.code_mode !== "advisory";
  const toggleLock = () =>
    setSpec((s) => ({
      ...s,
      code_mode: s.code_mode === "advisory" ? "strict" : "advisory",
    }));

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

  // ── SketchUp-style direct edits (move/rotate/stretch/duplicate/delete) ──
  // All baked in computePrimitives and honored by the Blender export.

  const componentNames = (s: AssetSpec): Set<string> => {
    try {
      return new Set(computePrimitives(s).map((p) => p.component));
    } catch {
      return new Set();
    }
  };

  /** Clone the selected component group; the copy is nudged aside and selected. */
  const duplicateComponent = (component: string) => {
    const taken = componentNames(spec);
    const base = `${component} copy`;
    let name = base;
    let i = 2;
    while (taken.has(name)) name = `${base} ${i++}`;
    setSpec((s) => {
      const edits = { ...(s.edits ?? {}) };
      edits.duplicates = [...(edits.duplicates ?? []), { source: component, name }];
      const offsets = { ...(s.offsets ?? {}), [name]: [0.3, 0, 0] as Vec3 };
      return { ...s, edits, offsets };
    });
    setSelected({ component: name });
  };

  /** Delete the selection: a whole component ('pole') or one part ('pole/shaft'). */
  const deleteSelection = (sel: Selection) => {
    const key = sel.part ? `${sel.component}/${sel.part}` : sel.component;
    setSpec((s) => {
      const edits = { ...(s.edits ?? {}) };
      edits.hidden = [...new Set([...(edits.hidden ?? []), key])];
      return { ...s, edits };
    });
    setSelected(null);
  };

  /** Bake a gizmo transform: moves → offsets, rotate/scale → edits. Values at
   * the identity are cleared so the overlay stays minimal. */
  const commitTransform = (component: string, t: CommittedTransform) =>
    setSpec((s) => {
      const eps = 1e-6;
      const rotations = { ...(s.edits?.rotations ?? {}) };
      const scales = { ...(s.edits?.scales ?? {}) };
      const offsets = { ...(s.offsets ?? {}) };
      if (t.rotation.some((v) => Math.abs(v) > eps)) rotations[component] = t.rotation;
      else delete rotations[component];
      if (t.scale.some((v) => Math.abs(v - 1) > eps)) scales[component] = t.scale;
      else delete scales[component];
      if (t.offset.some((v) => Math.abs(v) > eps)) offsets[component] = t.offset;
      else delete offsets[component];
      return { ...s, edits: { ...(s.edits ?? {}), rotations, scales }, offsets };
    });

  /** Drop every manual edit (also un-deletes and un-duplicates). */
  const resetEdits = () => {
    setSpec((s) => {
      const next = { ...s };
      delete next.edits;
      delete next.offsets;
      return next;
    });
    setSelected(null);
  };

  const hasEdits = Boolean(
    (spec.offsets && Object.keys(spec.offsets).length) ||
      (spec.edits &&
        ((spec.edits.rotations && Object.keys(spec.edits.rotations).length) ||
          (spec.edits.scales && Object.keys(spec.edits.scales).length) ||
          spec.edits.hidden?.length ||
          spec.edits.duplicates?.length)),
  );

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
    setHomeId((h) => h + 1); // glide the camera to frame the new asset
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
          spec={previewing ? fixedSpec! : spec}
          primitives={previewing ? fixedPrimitives! : primitives}
          displayUnits={displayUnits}
          theme={theme}
          selected={selected}
          onSelect={setSelected}
          tourId={tourId}
          homeId={homeId}
          onDuplicate={duplicateComponent}
          onDelete={deleteSelection}
          onCommitTransform={commitTransform}
          onResetEdits={resetEdits}
          hasEdits={hasEdits}
          flash={flash}
          banner={previewing ? "🔍 Previewing the proposed fixes — nothing applied yet" : null}
        />
      </main>
      <aside className="sidebar sidebar--right">
        {checkOpen && report ? (
          <CheckPanel
            report={report}
            isChecked={(f) => !excludedFixes.has(f.id)}
            onToggleFinding={(f) =>
              setExcludedFixes((prev) => {
                const next = new Set(prev);
                if (next.has(f.id)) next.delete(f.id);
                else next.add(f.id);
                return next;
              })
            }
            onHoverFinding={setHoverFinding}
            onPreview={setPreviewFixes}
            onApply={applyFixes}
            onClose={() => {
              setCheckOpen(false);
              setPreviewFixes(false);
              setHoverFinding(null);
            }}
            fixable={fixable}
          />
        ) : selected && primitives.some((p) => p.component === selected.component) ? (
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
            onWeatherAll={weatherAll}
            onDisplayUnits={setDisplayUnits}
            onHardware={toggleHardware}
            onTour={startTour}
            onCheck={startCheck}
            locked={locked}
            onLock={toggleLock}
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
