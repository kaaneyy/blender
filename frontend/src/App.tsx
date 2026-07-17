/** App shell: prompt/AI panel left, 3D viewport center, controls right.
 * Clicking a part in the viewport swaps the right panel for a focused
 * selection editor (position nudges, dimensions, group stats). Spec state
 * is the single source of truth; mesh + code checks recompute synchronously
 * on every change — the preview never waits on the server (T4.6). */
import { useEffect, useMemo, useState } from "react";
import defaultSpecJson from "../../examples/street_light.json";
import type { AssetSpec, Primitive, SpecMaterial, SpecPrimitive, UnitSystem, Vec3 } from "./types";
import { applyAuditFixes, auditConnections, computePrimitives } from "./builders";
import type { AuditFinding, AuditReport } from "./builders";
import {
  improveSpecStream,
  reviewConnectionsStream,
  summarizeChanges,
  variationsSpec,
  type Consensus,
  type DeepseekModel,
  type Finding,
  type Perspective,
  type SpecChanges,
  type Variant,
} from "./api";
import { checkSpec } from "./standards";
import { useSpecHistory } from "./hooks/useSpecHistory";
import CheckPanel from "./components/CheckPanel";
import ControlsPanel from "./components/ControlsPanel";
import LibraryPanel from "./components/LibraryPanel";
import PromptPanel from "./components/PromptPanel";
import SelectionPanel from "./components/SelectionPanel";
import VariationsPanel from "./components/VariationsPanel";
import Viewport, { type CommittedTransform } from "./components/Viewport";
import type { Selection } from "./components/AssetMesh";
import "./styles.css";

const defaultSpec = defaultSpecJson as unknown as AssetSpec;

/** Severity → icon for the improve findings list (same vocabulary as
 * CheckPanel's SEV_ICON; "info"/other severities fall back to ℹ️). */
const IMPROVE_SEV_ICON: Record<string, string> = { error: "⛔", warning: "⚠️" };

/** Peer-note stance → glyph + modifier class, mirroring the app's existing
 * severity color vocabulary (ok/danger for concur/dispute; neutral for a
 * refine — no new colors). */
const PEER_STANCE: Record<string, { glyph: string; cls: string }> = {
  concur: { glyph: "✓", cls: "peer-note__stance--concur" },
  dispute: { glyph: "✗", cls: "peer-note__stance--dispute" },
  refine: { glyph: "✎", cls: "peer-note__stance--refine" },
};

type Theme = "light" | "dark";

function initialTheme(): Theme {
  const saved = localStorage.getItem("af-theme");
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

// ── autosave / restore-on-init (Brief: Save / Open / autosave) ──
/** localStorage key the current spec autosaves to. */
const AUTOSAVE_KEY = "af-spec-autosave";
/** How long after the last spec change to wait before writing autosave —
 * long enough that a slider drag doesn't hammer localStorage on every tick. */
const AUTOSAVE_DEBOUNCE_MS = 800;

/** Pure "does this spec actually build?" check — the same validation
 * `adoptSpec` runs on every AI/Open swap, factored out so the app-init path
 * (reading a possibly-stale/corrupt autosave, before the component and its
 * state even exist) can share it without depending on any component state.
 * Returns an error string on invalid specs, or null when it's safe to adopt. */
function validateSpecForAdoption(candidate: AssetSpec): string | null {
  try {
    computePrimitives(candidate);
    return null;
  } catch (e) {
    return `The generated spec has invalid geometry: ${
      e instanceof Error ? e.message : String(e)
    }`;
  }
}

/** Read + validate the autosaved spec, if any. A corrupt or invalid stored
 * value is discarded silently (the key is cleared) so it can never come back
 * to bite a later load. Any localStorage failure (quota, private mode,
 * disabled storage) degrades to "nothing to restore" rather than crashing. */
function loadAutosavedSpec(): AssetSpec | null {
  try {
    const raw = localStorage.getItem(AUTOSAVE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as AssetSpec;
    if (validateSpecForAdoption(parsed) !== null) {
      localStorage.removeItem(AUTOSAVE_KEY);
      return null;
    }
    return parsed;
  } catch {
    try {
      localStorage.removeItem(AUTOSAVE_KEY);
    } catch {
      /* storage unavailable — nothing to clear, nothing to crash */
    }
    return null;
  }
}

/** Filename stem for "Save spec": lowercased, spaces→underscores, anything
 * outside [a-z0-9_-] stripped. Falls back to "asset" if that leaves nothing
 * (e.g. a name that's all punctuation/emoji). */
function sanitizeFileStem(raw: string): string {
  const stem = raw
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "_")
    .replace(/[^a-z0-9_-]/g, "");
  return stem || "asset";
}
// ── end autosave / restore-on-init ──

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
  // Computed once, before history exists: an autosave (if present and
  // valid) becomes the history's INITIAL present, not an edit applied on
  // top of the default — so an undo right after restore has nothing to
  // undo. See loadAutosavedSpec above.
  const [{ initialSpec, wasRestored }] = useState(() => {
    const restored = loadAutosavedSpec();
    return restored
      ? { initialSpec: restored, wasRestored: true }
      : { initialSpec: defaultSpec, wasRestored: false };
  });
  const {
    spec,
    setSpec,
    commitBoundary,
    undo,
    redo,
    canUndo,
    canRedo,
  } = useSpecHistory<AssetSpec>(() => structuredClone(initialSpec));
  const [displayUnits, setDisplayUnits] = useState<UnitSystem>(initialSpec.units);
  const [selected, setSelected] = useState<Selection | null>(null);
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [tourId, setTourId] = useState(0);
  const [homeId, setHomeId] = useState(0);
  /** "Restored your last session" notice, dismissible; also cleared by
   * Reset to defaults (which also drops the stored autosave itself). */
  const [restoredNotice, setRestoredNotice] = useState(wasRestored);

  // ── connection check: report, per-finding selection, hover preview.
  // "local" = the deterministic auditor (recomputed live); "ai" = the AI
  // fabrication review (a one-shot snapshot fetched from the backend).
  // Both feed the same panel, preview, and confirm-to-apply machinery. ──
  const [checkMode, setCheckMode] = useState<null | "local" | "ai">(null);
  const [aiReport, setAiReport] = useState<AuditReport | null>(null);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiStream, setAiStream] = useState("");
  const [aiError, setAiError] = useState<string | null>(null);
  /** finding ids the user UNchecked (default = every fixable finding on) */
  const [excludedFixes, setExcludedFixes] = useState<Set<string>>(new Set());
  const [previewFixes, setPreviewFixes] = useState(false);
  const [hoverFinding, setHoverFinding] = useState<AuditFinding | null>(null);

  // ── improve: one click runs the app's checks + an AI pass on the current
  // asset and adopts the result. Independent of the check panel above — its
  // findings are informational (the fix is already folded into the spec the
  // backend returns), so there is no apply/preview step. ──
  const [improveBusy, setImproveBusy] = useState(false);
  const [improveStream, setImproveStream] = useState("");
  const [improveError, setImproveError] = useState<string | null>(null);
  const [improveFindings, setImproveFindings] = useState<Finding[] | null>(null);
  /** Four professional-evaluator cards (architecture/mechanical/civil/design)
   * over the pre-improvement asset — absent on an older backend that hasn't
   * added the `perspectives` envelope key yet. */
  const [improvePerspectives, setImprovePerspectives] = useState<Perspective[] | undefined>(
    undefined,
  );
  /** What the AI pass actually touched, from the backend's optional `changes`
   * diff — absent on an older backend or when the diff failed. */
  const [improveChanges, setImproveChanges] = useState<SpecChanges | undefined>(undefined);
  /** What the panel jointly agreed matters most — absent on an older backend
   * or a malformed payload, in which case the consensus banner is omitted. */
  const [improveConsensus, setImproveConsensus] = useState<Consensus | undefined>(undefined);

  // ── variations: "✨ Give me 4 variants" requests a batch of alternate
  // specs, previews each as a live 3D thumbnail in a grid, and adopts the
  // picked one through the same validated adoptSpec path as every other AI
  // spec swap. `variantsOpen` gates whether the modal renders at all; busy/
  // error/variants describe the one in-flight (or last completed) request. ──
  const [variantsOpen, setVariantsOpen] = useState(false);
  const [variantsBusy, setVariantsBusy] = useState(false);
  // ── asset library ("📚 Library"): a separate, multi-slot, tagged
  // localStorage collection (frontend/src/library/store.ts) distinct from
  // the single-slot autosave and the one-file Save/Open flow above.
  // `libraryOpen` just gates whether the modal renders. ──
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [variants, setVariants] = useState<Variant[] | null>(null);
  const [variantsError, setVariantsError] = useState<string | null>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("af-theme", theme);
  }, [theme]);

  // ── autosave: debounced write of the current spec to localStorage, so a
  // refresh never destroys work-in-progress. Any storage failure (quota,
  // private browsing) degrades silently to "no autosave" rather than
  // crashing the app. ──
  useEffect(() => {
    const t = setTimeout(() => {
      try {
        localStorage.setItem(AUTOSAVE_KEY, JSON.stringify(spec));
      } catch {
        /* quota exceeded / storage disabled — autosave is best-effort */
      }
    }, AUTOSAVE_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [spec]);

  // ── global undo/redo shortcuts: Ctrl/Cmd+Z undoes, Ctrl/Cmd+Shift+Z and
  // Ctrl+Y redo. Ignored while the user is typing in a text field so the
  // prompt box's own text-undo isn't hijacked into spec-undo. ──
  useEffect(() => {
    const isTextEntry = (el: EventTarget | null) => {
      const node = el as HTMLElement | null;
      if (!node) return false;
      const tag = node.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || node.isContentEditable;
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (isTextEntry(e.target)) return;
      const mod = e.ctrlKey || e.metaKey;
      if (!mod) return;
      const key = e.key.toLowerCase();
      if (key === "z" && e.shiftKey) {
        e.preventDefault();
        redo();
      } else if (key === "z") {
        e.preventDefault();
        undo();
      } else if (key === "y") {
        e.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [undo, redo]);

  const primitives = useMemo(() => computePrimitives(spec), [spec]);
  const violations = useMemo(() => checkSpec(spec), [spec]);
  /** What the last "✨ Improve" pass touched, rendered as a hint line in the
   * improve panel — null when there's no changes envelope (older backend, or
   * an empty diff) so the panel looks exactly as it did before. */
  const improveChangesLine = summarizeChanges(improveChanges);

  // the deterministic audit re-runs live while its panel is open, so the
  // report always matches the current sliders/edits — applying is still
  // click-only. The AI report is a snapshot from when the button was hit.
  const localReport = useMemo(
    () => (checkMode === "local" ? auditConnections(spec) : null),
    [checkMode, spec],
  );
  const report = checkMode === "ai" ? aiReport : localReport;
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

  /** Make hardware visible (both checks inspect it, so the user should see
   * it too). */
  const showHardware = () =>
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

  const resetCheckSelection = () => {
    setExcludedFixes(new Set());
    setPreviewFixes(false);
    setHoverFinding(null);
  };

  /** Open the deterministic connection check. */
  const startCheck = () => {
    showHardware();
    resetCheckSelection();
    closeImprove(); // a finished improve panel shouldn't keep this one hidden
    setCheckMode("local");
  };

  /** Ask the AI to review the connections. Same principles as the local
   * check: findings arrive in the same format, hovering Apply previews the
   * result in 3D, and nothing is applied without confirmation. */
  const startAiCheck = () => {
    if (aiBusy) return;
    showHardware();
    resetCheckSelection();
    closeImprove(); // a finished improve panel shouldn't keep this one hidden
    setAiReport(null);
    setAiError(null);
    setAiStream("");
    setCheckMode("ai");
    setAiBusy(true);
    const model = (localStorage.getItem("af-model") ?? "") as DeepseekModel | "";
    reviewConnectionsStream(spec, setAiStream, model)
      .then(setAiReport)
      .catch((e) => setAiError(e instanceof Error ? e.message : String(e)))
      .finally(() => setAiBusy(false));
  };

  const closeCheck = () => {
    setCheckMode(null);
    setAiReport(null);
    setAiError(null);
    resetCheckSelection();
  };

  /** "✨ Improve": run the app's deterministic checks + an AI pass on the
   * current asset in one shot, then adopt the result through the same
   * validated path as every other AI spec swap. The findings the checks
   * flagged beforehand are shown for context — the fix is already folded
   * into the adopted spec, so there's nothing further to apply. */
  const startImprove = () => {
    if (aiBusy || improveBusy) return;
    showHardware();
    setImproveError(null);
    setImproveFindings(null);
    setImprovePerspectives(undefined);
    setImproveChanges(undefined);
    setImproveConsensus(undefined);
    setImproveStream("");
    setImproveBusy(true);
    const model = (localStorage.getItem("af-model") ?? "") as DeepseekModel | "";
    improveSpecStream(spec, setImproveStream, model)
      .then((result) => {
        const err = adoptSpec(result.spec);
        if (err) {
          setImproveError(err);
          return;
        }
        setImproveFindings(result.findings);
        setImprovePerspectives(result.perspectives);
        setImproveChanges(result.changes);
        setImproveConsensus(result.consensus);
      })
      .catch((e) => setImproveError(e instanceof Error ? e.message : String(e)))
      .finally(() => setImproveBusy(false));
  };

  const closeImprove = () => {
    setImproveError(null);
    setImproveFindings(null);
    setImprovePerspectives(undefined);
    setImproveChanges(undefined);
    setImproveConsensus(undefined);
  };

  /** "✨ Give me 4 variants": request a batch of alternate takes on the
   * current asset and open the grid to preview them — picking one adopts it
   * through `adoptSpec` (see VariationsPanel's `onPick`), never a direct
   * setSpec. Read the model choice from localStorage exactly like
   * startImprove does. */
  const startVariations = () => {
    if (variantsBusy) return;
    setVariantsOpen(true);
    setVariantsBusy(true);
    setVariantsError(null);
    setVariants(null);
    const model = (localStorage.getItem("af-model") ?? "") as DeepseekModel | "";
    variationsSpec(spec, 4, model)
      .then(setVariants)
      .catch((e) => setVariantsError(e instanceof Error ? e.message : String(e)))
      .finally(() => setVariantsBusy(false));
  };

  const closeVariations = () => {
    setVariantsOpen(false);
    setVariantsBusy(false);
    setVariants(null);
    setVariantsError(null);
  };

  /** The confirmed apply — the ONLY place check fixes reach the spec. The
   * local report then recomputes on the fixed spec; the AI report drops the
   * findings that were just applied and keeps the rest for review. */
  const applyFixes = () => {
    if (!fixedSpec) return;
    setSpec(fixedSpec);
    if (checkMode === "ai" && aiReport) {
      const applied = new Set(fixable.map((f) => f.id));
      setAiReport({
        ...aiReport,
        findings: aiReport.findings.filter((f) => !applied.has(f.id)),
      });
    }
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

  /** Bake a gizmo transform: moves → offsets, rotate/scale → edits. The key
   * is the edit target — a whole component ('pole') or a single part
   * ('pole/shaft') when the user drilled down before grabbing the tool.
   * Values at the identity are cleared so the overlay stays minimal. */
  const commitTransform = (key: string, t: CommittedTransform) =>
    setSpec((s) => {
      const eps = 1e-6;
      const rotations = { ...(s.edits?.rotations ?? {}) };
      const scales = { ...(s.edits?.scales ?? {}) };
      const offsets = { ...(s.offsets ?? {}) };
      if (t.rotation.some((v) => Math.abs(v) > eps)) rotations[key] = t.rotation;
      else delete rotations[key];
      if (t.scale.some((v) => Math.abs(v - 1) > eps)) scales[key] = t.scale;
      else delete scales[key];
      if (t.offset.some((v) => Math.abs(v) > eps)) offsets[key] = t.offset;
      else delete offsets[key];
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

  /** Swap in an AI-generated (or file-opened) spec — but only if it actually
   * builds, so a bad spec can never blank the viewport. Returns an error
   * string to show in the prompt panel, or null on success. A full spec
   * swap is always its own undo step: commitBoundary() guarantees it can
   * never coalesce into whatever edit burst happened to precede it. */
  const adoptSpec = (newSpec: AssetSpec): string | null => {
    const err = validateSpecForAdoption(newSpec);
    if (err) return err;
    commitBoundary();
    setSpec(newSpec);
    setSelected(null);
    setDisplayUnits(newSpec.units ?? "imperial");
    setHomeId((h) => h + 1); // glide the camera to frame the new asset
    return null;
  };

  /** "📂 Open spec": read a picked file, parse it, and adopt it through the
   * exact same validated path as an AI-generated spec — Open reads back
   * exactly what Save writes out. Never partially applies: parse/validate
   * failures leave the current spec untouched. */
  const openSpecFile = async (file: File): Promise<string | null> => {
    let parsed: AssetSpec;
    try {
      const text = await file.text();
      parsed = JSON.parse(text) as AssetSpec;
    } catch (e) {
      return `Couldn't read "${file.name}": ${e instanceof Error ? e.message : String(e)}`;
    }
    return adoptSpec(parsed);
  };

  /** "💾 Save spec": download the current design as the JSON file the
   * documented Blender export pipeline (build_cli.py) consumes directly. */
  const saveSpecFile = () => {
    const filename = `${sanitizeFileStem(spec.name || spec.asset_type)}.json`;
    const blob = new Blob([JSON.stringify(spec, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    try {
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    } finally {
      URL.revokeObjectURL(url);
    }
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
          onVariations={startVariations}
          variationsBusy={variantsBusy}
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
        {checkMode === "ai" && (aiBusy || aiError) ? (
          <div className="panel check-panel">
            <div className="panel__header">
              <h3>🤖 AI connection review</h3>
              <button className="close" onClick={closeCheck} title="Close the AI review">
                ✕
              </button>
            </div>
            {aiBusy ? (
              <>
                <div className="stream-card" aria-live="off">
                  <div className="stream-card__title">
                    <span className="stream-card__dot" /> Reviewing every joint…
                  </div>
                  <div className="stream-card__text">{aiStream.slice(-700) || "…"}</div>
                </div>
                <p className="hint">
                  The AI reads the spec, the generated joint schedule, and the
                  deterministic findings, then proposes fixes in the same
                  format as the local check — previewed on hover, applied only
                  when you confirm.
                </p>
              </>
            ) : (
              <>
                <div className="violation" role="alert">
                  <p>{aiError}</p>
                </div>
                <button onClick={startAiCheck}>Try again</button>
              </>
            )}
          </div>
        ) : improveBusy || improveError || improveFindings ? (
          <div className="panel check-panel">
            <div className="panel__header">
              <h3>✨ Improve</h3>
              <button className="close" onClick={closeImprove} title="Close the improve panel">
                ✕
              </button>
            </div>
            {improveBusy ? (
              <>
                <div className="stream-card" aria-live="off">
                  <div className="stream-card__title">
                    <span className="stream-card__dot" /> Checking and improving the asset…
                  </div>
                  <div className="stream-card__text">{improveStream.slice(-700) || "…"}</div>
                </div>
                <p className="hint">
                  Runs the app's deterministic checks against the current
                  asset, then asks the AI to improve it in one pass — the
                  result replaces the current asset once it builds cleanly.
                </p>
              </>
            ) : improveError ? (
              <>
                <div className="violation" role="alert">
                  <p>{improveError}</p>
                </div>
                <button onClick={startImprove}>Try again</button>
              </>
            ) : (
              <>
                <div
                  className={`code-status ${
                    improveFindings!.length === 0 ? "code-status--ok" : "code-status--bad"
                  }`}
                >
                  {improveFindings!.length === 0
                    ? "0 findings — asset already passes all checks"
                    : `${improveFindings!.length} finding${
                        improveFindings!.length === 1 ? "" : "s"
                      } from the pre-improvement checks`}
                </div>
                <p className="hint">
                  The asset shown now is the AI-improved version — the checks
                  below describe what it found before improving.
                </p>
                {improveChangesLine && <p className="hint">✏️ {improveChangesLine}</p>}
                {improveConsensus && (
                  <div className="consensus-banner">
                    <div className="consensus-banner__title">🤝 Panel consensus</div>
                    <p className="consensus-banner__summary">{improveConsensus.summary}</p>
                    {improveConsensus.priorities.length > 0 && (
                      <ol className="consensus-banner__priorities">
                        {improveConsensus.priorities.map((pr, i) => (
                          <li key={i}>{pr}</li>
                        ))}
                      </ol>
                    )}
                  </div>
                )}
                {improvePerspectives && (
                  <div className="perspectives">
                    {improvePerspectives.map((p) => (
                      <div key={p.id} className="perspective-card">
                        <div className="perspective-card__header">
                          <span>{p.icon}</span>
                          <span>{p.label}</span>
                        </div>
                        {p.summary && <p className="perspective-card__summary">{p.summary}</p>}
                        {p.error && (
                          <div className="violation" role="alert">
                            <p>{p.error}</p>
                          </div>
                        )}
                        {p.findings.length === 0 ? (
                          !p.error && <p className="hint">No issues from this perspective.</p>
                        ) : (
                          p.findings.map((f, i) => (
                            <div key={i} className={`finding finding--${f.severity}`}>
                              <span className="finding__title">
                                {IMPROVE_SEV_ICON[f.severity] ?? "ℹ️"} {f.kind.replace(/_/g, " ")}
                                <span
                                  className={`finding__source finding__source--${f.source}`}
                                  title={
                                    f.source === "ai"
                                      ? "Flagged by this persona's AI review"
                                      : "Flagged by the deterministic checks"
                                  }
                                >
                                  {f.source === "ai" ? "AI" : "check"}
                                </span>
                              </span>
                              <p className="finding__detail">{f.message}</p>
                            </div>
                          ))
                        )}
                        {p.peer_notes && p.peer_notes.length > 0 && (
                          <div className="peer-notes">
                            {p.peer_notes.map((n, i) => {
                              const reviewer = improvePerspectives!.find((q) => q.id === n.from);
                              const stance = PEER_STANCE[n.stance];
                              return (
                                <p key={i} className="peer-note">
                                  <span className="peer-note__icon">{reviewer?.icon ?? "🧑"}</span>
                                  <span className={`peer-note__stance ${stance.cls}`}>
                                    {stance.glyph}
                                  </span>
                                  <span className="peer-note__text">&ldquo;{n.note}&rdquo;</span>
                                </p>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
                {improveFindings!.map((f, i) => (
                  <div key={i} className={`finding finding--${f.severity}`}>
                    <span className="finding__title">
                      {IMPROVE_SEV_ICON[f.severity] ?? "ℹ️"} {f.kind.replace(/_/g, " ")}
                    </span>
                    <p className="finding__detail">{f.message}</p>
                  </div>
                ))}
              </>
            )}
          </div>
        ) : checkMode && report ? (
          <CheckPanel
            report={report}
            mode={checkMode}
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
            onClose={closeCheck}
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
            onCheckAI={startAiCheck}
            onImprove={startImprove}
            improveDisabled={aiBusy || improveBusy}
            locked={locked}
            onLock={toggleLock}
            onReset={() => {
              setSpec(structuredClone(defaultSpec));
              setSelected(null);
              setRestoredNotice(false);
              try {
                localStorage.removeItem(AUTOSAVE_KEY);
              } catch {
                /* storage unavailable — nothing to clear */
              }
            }}
            canUndo={canUndo}
            canRedo={canRedo}
            onUndo={undo}
            onRedo={redo}
            onSave={saveSpecFile}
            onOpenFile={openSpecFile}
            onLibrary={() => setLibraryOpen(true)}
            restoredNotice={restoredNotice}
            onDismissRestoredNotice={() => setRestoredNotice(false)}
          />
        )}
      </aside>
      {variantsOpen && (
        <VariationsPanel
          busy={variantsBusy}
          error={variantsError}
          variants={variants}
          onPick={adoptSpec}
          onClose={closeVariations}
          onRetry={startVariations}
        />
      )}
      {libraryOpen && (
        <LibraryPanel spec={spec} onAdopt={adoptSpec} onClose={() => setLibraryOpen(false)} />
      )}
    </div>
  );
}
