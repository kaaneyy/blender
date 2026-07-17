/** Left panel: describe any asset → AI generates a spec (streamed live);
 * keep refining conversationally. Also hosts the installation-guide and
 * standards-refresh tools, both streamed. The guide is cached per spec so
 * reopening it costs nothing when the asset hasn't changed. */
import { useEffect, useRef, useState } from "react";
import type { AssetSpec } from "../types";
import type { CodeViolation } from "../standards";
import {
  buildabilityFindings,
  clarifyRequest,
  focusSpecStream,
  generateSpecStream,
  installGuideStream,
  refineSpecStream,
  summarizeChanges,
  updateStandardsStream,
  wizardStepStream,
  MODEL_OPTIONS,
  type Clarification,
  type ClarifyQuestion,
  type DeepseekModel,
  type PanelEntry,
  type SpecChanges,
  type StandardsUpdateResult,
  type WizardStep,
} from "../api";
import { auditConnections } from "../builders";
import Modal from "./Modal";
import { EXAMPLE_ASSETS } from "../examples";

/** Live, free, client-side connection/buildability findings for a spec (no
 * network — same deterministic auditor behind "Check connections"). Used to
 * ground AI edit requests with the exact measured problem instead of vague
 * free text, and to report the real outcome after a response instead of an
 * unconditional "Updated the form." */
function connectionIssues(spec: AssetSpec): string[] {
  return auditConnections(spec)
    .findings.slice(0, 5)
    .map((f) => `${f.title}: ${f.detail}`);
}

/** "Machine findings to fix first" block (same phrasing the "Check & fix
 * connections" quick fix already uses), or "" when the spec is clean. */
function groundingBlock(spec: AssetSpec): string {
  const issues = connectionIssues(spec);
  return issues.length ? `\nMachine findings to fix first:\n- ${issues.join("\n- ")}` : "";
}

/** Chat-message suffix reporting whether the spec a response just produced
 * still has connection issues — "" when clean, so the common case stays
 * unchanged. */
function statusSuffix(spec: AssetSpec): string {
  const issues = connectionIssues(spec);
  if (!issues.length) return "";
  const n = issues.length;
  return (
    ` ⚠️ ${n} connection issue${n > 1 ? "s" : ""} remain — e.g. ${issues[0]} ` +
    `Refine again, or open 🔍 Check connections for details and one-click fixes.`
  );
}

interface ChatEntry {
  role: "you" | "assetforge";
  text: string;
}

/** Compact "design panel" chat lines: one per persona's take on what the
 * user deep-down asked for, each truncated to a chat-line-friendly length.
 * Returns [] when there's no panel to show (older backend / failed call). */
function panelEntries(panel: PanelEntry[] | undefined): ChatEntry[] {
  if (!panel || !panel.length) return [];
  const entries: ChatEntry[] = [
    { role: "assetforge", text: "🎭 Design panel — how each discipline read your ask:" },
  ];
  for (const p of panel) {
    const take = p.take.length > 140 ? `${p.take.slice(0, 140)}…` : p.take;
    entries.push({ role: "assetforge", text: `${p.icon} ${p.label}: ${take}` });
  }
  return entries;
}

/** Optional "✏️ what changed" chat line appended right after an AI edit,
 * built from the backend's `changes` diff envelope — [] when there's
 * nothing to show (no envelope from an older backend, or a failed/empty
 * diff), so the common case renders exactly today's output. */
function changeEntries(changes: SpecChanges | undefined): ChatEntry[] {
  const line = summarizeChanges(changes);
  return line ? [{ role: "assetforge", text: `✏️ ${line}` }] : [];
}

type Busy = false | "generate" | "refine" | "focus" | "guide" | "standards" | "wizard";

const BUSY_TITLES: Record<Exclude<Busy, false>, string> = {
  generate: "Generating your asset…",
  refine: "Applying your change…",
  focus: "Detailing that area…",
  guide: "Writing the installation guide…",
  standards: "Researching standards…",
  wizard: "Working on this step…",
};

const MODEL_KEY = "af-model";

/** Dropdown sentinel for "type your own answer" on a clarifying question. */
const CUSTOM_ANSWER = "__custom__";

/** One round of clarifying questions, pinned to the prompt it was asked
 * about and to the flow (one-shot generate vs guided build) that continues
 * once the user answers or skips. */
interface ClarifyState {
  forPrompt: string;
  mode: "generate" | "wizard";
  questions: ClarifyQuestion[];
  choices: string[]; // per question: "", an offered option, or CUSTOM_ANSWER
  custom: string[]; // per question: the typed answer when choice is CUSTOM_ANSWER
}

/** The guided 4-step build. Step 1 (Form) is the initial generate; steps
 * 2-4 are scoped AI passes (backend keys connections | materials | details).
 * Nothing auto-advances — each step waits for the user to refine or accept. */
const WIZARD_STEPS = [
  {
    key: "form",
    n: 1,
    icon: "📐",
    title: "Form",
    short: "Form",
    blurb: "The raw shape, size, and proportions of what you asked for.",
    placeholder: 'change the form — "make it 14 ft", "add a second arm", "art-deco style"',
    accept: "Form's right — build the connections →",
  },
  {
    key: "connections",
    n: 2,
    icon: "🔩",
    title: "Connections",
    short: "Joints",
    blurb:
      "How every part joins and carries load to the ground — bolts, welds, clamps, anchors, or nothing where a connection isn't needed.",
    placeholder: 'adjust joints — "weld the arm instead of clamping", "no anchor bolts"',
    accept: "Connections good — choose materials →",
  },
  {
    key: "materials",
    n: 3,
    icon: "🎨",
    title: "Materials",
    short: "Materials",
    blurb: "The material, finish, and weathering of each part.",
    placeholder: 'change finish — "weathered bronze pole", "brand-new powder coat"',
    accept: "Materials good — detail the working parts →",
  },
  {
    key: "details",
    n: 4,
    icon: "💡",
    title: "Working parts",
    short: "Details",
    blurb:
      "Lights, lenses, and adjustable features (banner brackets, extra arms) worked out in detail.",
    placeholder: 'tune the parts — "warmer, brighter lens", "add a dimming toggle"',
    accept: "Done — finish ✓",
  },
] as const;

/** One-click quick-fix presets. Each `message` is a canned refine
 * instruction (kept well under the 2000-char API cap). */
const QUICK_FIXES: Array<{ label: string; title: string; message: string }> = [
  {
    label: "🔗 Check & fix connections",
    title: "Audit every joint: real load path, parts actually touch, fasteners appropriate to the material",
    message:
      "Audit and fix EVERY connection in this asset, then return the FULL updated AssetSpec JSON (keep ids/values stable where unchanged). " +
      "1) Load path: every part must be supported down to the ground (z=0); add rails, aprons, stretchers, brackets or collars where a part has nothing to attach to. " +
      "2) Contact: joined parts must interpenetrate 10-20 mm — fix any parts that float or merely touch at a zero-thickness face so the app can place hardware where they truly overlap. " +
      "3) Intent: declare every real joint in the top-level connections array with the fabrication type a crew would use — anchor_base (structural vertical at grade, b:'ground'), band_clamp (arm on round pole), slip_fit (post-top telescoping fit), carriage_bolt (wood on metal frame), through_bolt, flange_splice, weld (welded steel, no bolts), lag_screw, or none (concealed joinery/cast-integral). " +
      "4) Appropriateness: light-duty or non-structural items (a basic table, wooden furniture, decorative props) must NOT show industrial anchors — declare their legs {a, b:'ground', type:'none'} and use joinery. Reserve anchor bases for structural metal verticals (poles, signs, heavy frames). Nothing below z=0.",
  },
  {
    label: "⚖️ Fix proportions",
    title: "Give members real taper/slenderness ratios instead of uniform sticks",
    message:
      "Improve the structural proportions of this asset and return the FULL updated AssetSpec JSON. Replace uniform, chunky, equal-thickness members with properly proportioned ones expressed as ratios in the parameter expressions: a vertical pole's base diameter about 1.8x its top (taper), a cantilevered arm tapering to about 60% radius at the tip, a post-top globe about 1.2-1.6x the post diameter, slats/rails thin relative to their span. Keep the asset_type, overall dimensions, component names, and all ids identical; only adjust proportions/expressions.",
  },
  {
    label: "🎨 Improve materials & finish",
    title: "Sensible preset + finish per part, matched to the style",
    message:
      "Improve ONLY the materials of this asset (do not change any geometry, parameters, or ids) and return the FULL updated AssetSpec JSON. Give every material slot a preset and a finish (cast | machined | sheet | rough) that suits its part: cast for cast-iron bases/finials, machined for turned fittings, sheet for housings/panels, rough for galvanized poles and raw concrete. Match colors to the asset's implied style. Only add weathering (0-1) if the setting implies age; otherwise leave it 0.",
  },
  {
    label: "✨ Add realistic detail",
    title: "Believable secondary detail without changing the overall form",
    message:
      "Add believable secondary detail to this asset and return the FULL updated AssetSpec JSON. Add caps, seams, trim rings, fillets, and visible fasteners only where a real one would appear, using the fabrication kinds where apt (lathe for finials/domes, sweep for curved members, loft for tapered housings, tube for hollow posts). Do NOT change the overall silhouette or violate the code ranges; keep existing component names and ids, and keep the part count reasonable (favor readable massing over micro-detail).",
  },
];

/** Live "the AI is generating" card: shows the streaming tail so the user
 * can see progress without needing to read it. Reasoning ("thinking") models
 * wrap their chain of thought in <think> tags — strip the literal tags for
 * display and flag when the model is still thinking. */
function StreamCard({ title, text }: { title: string; text: string }) {
  const boxRef = useRef<HTMLDivElement>(null);
  const thinking = text.lastIndexOf("<think>") > text.lastIndexOf("</think>");
  const clean = text.replace(/<\/?think>/g, "");
  useEffect(() => {
    boxRef.current?.scrollTo({ top: boxRef.current.scrollHeight });
  }, [text]);
  return (
    <div className="stream-card" aria-live="off">
      <div className="stream-card__title">
        <span className="stream-card__dot" /> {title}
        {thinking && <span className="stream-card__thinking"> · thinking…</span>}
      </div>
      <div className="stream-card__text" ref={boxRef}>
        {clean.slice(-800) || "…"}
      </div>
    </div>
  );
}

/** Minimal markdown rendering for the install guide (headings + lines). */
function GuideText({ text }: { text: string }) {
  return (
    <div className="guide">
      {text.split("\n").map((line, i) => {
        const heading = line.match(/^#{1,3}\s+(.*)/);
        if (heading) return <h4 key={i}>{heading[1]}</h4>;
        if (!line.trim()) return <div key={i} className="guide__gap" />;
        return <p key={i}>{line.replace(/\*\*/g, "")}</p>;
      })}
    </div>
  );
}

function download(filename: string, content: string, type = "text/plain") {
  const blob = new Blob([content], { type });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

export default function PromptPanel({
  spec,
  violations,
  theme,
  onTheme,
  onName,
  onSpec,
  onVariations,
  variationsBusy,
}: {
  spec: AssetSpec;
  violations: Record<string, CodeViolation>;
  theme: "light" | "dark";
  onTheme: (t: "light" | "dark") => void;
  onName: (name: string) => void;
  onSpec: (spec: AssetSpec) => string | null; // returns error message if spec unusable
  /** "✨ Give me 4 variants": requests a batch of alternate specs and opens
   * the grid to preview + pick one (App owns the request/state; this panel
   * only triggers it). */
  onVariations: () => void;
  /** Disables the trigger and swaps its label while a batch is in flight. */
  variationsBusy: boolean;
}) {
  const [prompt, setPrompt] = useState("");
  const [refineMsg, setRefineMsg] = useState("");
  const [focusArea, setFocusArea] = useState("");
  const [busy, setBusy] = useState<Busy>(false);
  const [streamText, setStreamText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [chat, setChat] = useState<ChatEntry[]>([]);
  const [guide, setGuide] = useState<string | null>(null);
  const [standardsResult, setStandardsResult] = useState<StandardsUpdateResult | null>(null);
  const [model, setModel] = useState<DeepseekModel>(
    () => (localStorage.getItem(MODEL_KEY) as DeepseekModel) || "deepseek-chat",
  );
  const [wizardStep, setWizardStep] = useState<number | null>(null); // null = not in guided build
  const [wizardMsg, setWizardMsg] = useState("");
  const [clarify, setClarify] = useState<ClarifyState | null>(null);
  const [clarifyBusy, setClarifyBusy] = useState<false | "generate" | "wizard">(false);
  const guideCache = useRef<{ key: string; text: string } | null>(null);
  const violationCount = Object.keys(violations).length;

  // the clarify popup is open from the moment questions are requested (shows
  // a loading spinner) through to the user answering or skipping — not just
  // while `clarify` itself (the loaded questions) is set.
  const clarifyOpen = clarify !== null || clarifyBusy !== false;
  const clarifyFirstSelectRef = useRef<HTMLSelectElement>(null);

  useEffect(() => {
    localStorage.setItem(MODEL_KEY, model);
  }, [model]);

  // clarify popup: lock page scroll while it's mounted, restore on close.
  useEffect(() => {
    if (!clarifyOpen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [clarifyOpen]);

  // clarify popup: move focus into the dialog (first question's dropdown)
  // once the questions have loaded.
  useEffect(() => {
    if (clarify) clarifyFirstSelectRef.current?.focus();
  }, [clarify]);

  const run = async (kind: Exclude<Busy, false>, task: () => Promise<void>) => {
    if (busy) return;
    setBusy(kind);
    setError(null);
    setStreamText("");
    try {
      await task();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setStreamText("");
    }
  };

  const runGenerate = (text: string, clarifications: Clarification[] = []) =>
    run("generate", async () => {
      if (!text) return;
      const { spec: newSpec, brief, panel } = await generateSpecStream(
        text, setStreamText, model, clarifications,
      );
      const problem = onSpec(newSpec);
      if (problem) throw new Error(problem);
      const entries: ChatEntry[] = [{ role: "you", text }];
      if (clarifications.length) {
        entries.push({
          role: "you",
          text: `📎 ${clarifications.map((c) => c.answer).join(" · ")}`,
        });
      }
      if (brief && brief.toLowerCase() !== text.toLowerCase()) {
        const shown = brief.length > 220 ? `${brief.slice(0, 220)}…` : brief;
        entries.push({ role: "assetforge", text: `Interpreted as: ${shown}` });
      }
      entries.push({
        role: "assetforge",
        text: `Built "${newSpec.name}" (${newSpec.asset_type}). Refine it below or tweak the sliders.${statusSuffix(newSpec)}`,
      });
      entries.push(...panelEntries(panel));
      setChat(entries);
      setPrompt("");
    });

  /** Start the guided 4-step build: generate the raw form, then enter the
   * wizard at step 1. Steps never auto-chain from here. */
  const runGuidedStart = (text: string, clarifications: Clarification[] = []) =>
    run("wizard", async () => {
      if (!text) return;
      const { spec: newSpec, brief, panel } = await generateSpecStream(
        text, setStreamText, model, clarifications,
      );
      const problem = onSpec(newSpec);
      if (problem) throw new Error(problem);
      const entries: ChatEntry[] = [{ role: "you", text }];
      if (clarifications.length) {
        entries.push({
          role: "you",
          text: `📎 ${clarifications.map((c) => c.answer).join(" · ")}`,
        });
      }
      if (brief && brief.toLowerCase() !== text.toLowerCase()) {
        const shown = brief.length > 220 ? `${brief.slice(0, 220)}…` : brief;
        entries.push({ role: "assetforge", text: `Interpreted as: ${shown}` });
      }
      entries.push({
        role: "assetforge",
        text: `Step 1 — built the form of "${newSpec.name}". Review it, then refine or accept.${statusSuffix(newSpec)}`,
      });
      entries.push(...panelEntries(panel));
      setChat(entries);
      setPrompt("");
      setWizardMsg("");
      setWizardStep(0);
    });

  /** Step 0 of every generation: ask the AI for a handful of clarifying
   * questions (3 offered answers each + a type-your-own blank) so a basic
   * request surfaces the real one behind it. Clarifying is an enhancement,
   * never a gate — if the call fails, generation proceeds directly. */
  const startClarify = async (mode: "generate" | "wizard") => {
    const text = prompt.trim();
    if (!text || busy !== false || clarifyBusy !== false) return;
    setError(null);
    setClarifyBusy(mode);
    try {
      const questions = await clarifyRequest(text, model);
      setClarify({
        forPrompt: text,
        mode,
        questions,
        choices: questions.map(() => ""),
        custom: questions.map(() => ""),
      });
    } catch {
      setClarify(null);
      if (mode === "wizard") void runGuidedStart(text);
      else void runGenerate(text);
    } finally {
      setClarifyBusy(false);
    }
  };

  const setClarifyChoice = (i: number, value: string) =>
    setClarify((c) =>
      c ? { ...c, choices: c.choices.map((v, j) => (j === i ? value : v)) } : c,
    );

  const setClarifyCustom = (i: number, value: string) =>
    setClarify((c) =>
      c ? { ...c, custom: c.custom.map((v, j) => (j === i ? value : v)) } : c,
    );

  /** Continue the flow the questions belong to — with the answered pairs,
   * or with none when the user skips. */
  const finishClarify = (useAnswers: boolean) => {
    if (!clarify || busy !== false) return;
    const answers: Clarification[] = useAnswers
      ? clarify.questions
          .map((q, i) => ({
            question: q.question,
            answer:
              clarify.choices[i] === CUSTOM_ANSWER
                ? clarify.custom[i].trim()
                : clarify.choices[i],
          }))
          .filter((c) => c.answer)
      : [];
    const { mode, forPrompt } = clarify;
    setClarify(null);
    if (mode === "wizard") void runGuidedStart(forPrompt, answers);
    else void runGenerate(forPrompt, answers);
  };

  // clarify popup: Esc = the same skip path as the backdrop/✕ click (a no-op
  // while questions are still loading, same as those — there's no in-flight
  // request to cancel, `finishClarify` requires `clarify` to be loaded).
  useEffect(() => {
    if (!clarifyOpen) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") finishClarify(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [clarifyOpen, clarify, busy]);

  /** Apply a user change to the CURRENT step (stays on the same step). Form
   * is a plain refine; later steps re-run their scoped pass with the note.
   * Form/Connections are the only steps whose job is geometry/joints, so
   * only those get grounded with live measured findings — materials/details
   * are explicitly told not to touch geometry, and grounding them with a
   * connection complaint would fight that scope. */
  const applyWizardChange = () =>
    run("wizard", async () => {
      if (wizardStep === null) return;
      const msg = wizardMsg.trim();
      if (!msg) return;
      const step = WIZARD_STEPS[wizardStep];
      const groundGeometry = step.key === "form" || step.key === "connections";
      const sent = groundGeometry ? msg + groundingBlock(spec) : msg;
      const { spec: newSpec, changes } =
        step.key === "form"
          ? await refineSpecStream(spec, sent, setStreamText, model)
          : await wizardStepStream(spec, step.key as WizardStep, sent, setStreamText, model);
      const problem = onSpec(newSpec);
      if (problem) throw new Error(problem);
      setChat((c) => [
        ...c,
        { role: "you", text: `✎ ${step.title}: ${msg}` },
        { role: "assetforge", text: `Updated the ${step.title.toLowerCase()}.${statusSuffix(newSpec)}` },
        ...changeEntries(changes),
      ]);
      setWizardMsg("");
    });

  /** Accept the current step and move on: run the NEXT step's pass, or finish
   * on the last step. This is the only place a step advances. Advancing INTO
   * Connections — the step whose job is exactly fixing joints/load paths —
   * is grounded with live measured findings the same way applyWizardChange
   * is, so a bad Form step's known problems reach the Connections pass on
   * its very first attempt instead of only after the user notices and asks
   * again. */
  const acceptWizardStep = () =>
    run("wizard", async () => {
      if (wizardStep === null) return;
      if (wizardStep >= WIZARD_STEPS.length - 1) {
        setChat((c) => [
          ...c,
          { role: "assetforge", text: "Guided build complete — keep tweaking with the tools below." },
        ]);
        setWizardStep(null);
        setWizardMsg("");
        return;
      }
      const next = WIZARD_STEPS[wizardStep + 1];
      const note = next.key === "connections" ? groundingBlock(spec) : "";
      const { spec: newSpec, changes } = await wizardStepStream(
        spec,
        next.key as WizardStep,
        note,
        setStreamText,
        model,
      );
      const problem = onSpec(newSpec);
      if (problem) throw new Error(problem);
      setChat((c) => [
        ...c,
        {
          role: "assetforge",
          text: `Step ${next.n} — worked on the ${next.title.toLowerCase()}. Review, then refine or accept.${statusSuffix(newSpec)}`,
        },
        ...changeEntries(changes),
      ]);
      setWizardMsg("");
      setWizardStep(wizardStep + 1);
    });

  /** Load a bundled example asset. Synchronous (no AI) — swaps the spec,
   * leaves any guided build, and logs it. The <select> resets to its
   * placeholder so it reads as an action, not a current-asset indicator. */
  const loadExample = (id: string) => {
    if (busy) return;
    const example = EXAMPLE_ASSETS.find((e) => e.id === id);
    if (!example) return;
    const problem = onSpec(structuredClone(example.spec));
    if (problem) {
      setError(problem);
      return;
    }
    setError(null);
    setWizardStep(null);
    setWizardMsg("");
    setClarify(null);
    setChat([{ role: "assetforge", text: `Loaded example: ${example.label}. Refine it or tweak the sliders.` }]);
  };

  const runRefine = () =>
    run("refine", async () => {
      const msg = refineMsg.trim();
      if (!msg) return;
      const { spec: newSpec, changes } = await refineSpecStream(
        spec, msg + groundingBlock(spec), setStreamText, model,
      );
      const problem = onSpec(newSpec);
      if (problem) throw new Error(problem);
      setChat((c) => [
        ...c,
        { role: "you", text: msg },
        { role: "assetforge", text: `Updated "${newSpec.name}".${statusSuffix(newSpec)}` },
        ...changeEntries(changes),
      ]);
      setRefineMsg("");
    });

  /** Deep-detail ONE area, leaving everything else untouched. */
  const runFocus = () =>
    run("focus", async () => {
      const area = focusArea.trim();
      if (!area) return;
      const newSpec = await focusSpecStream(spec, area, setStreamText, model);
      const problem = onSpec(newSpec);
      if (problem) throw new Error(problem);
      setChat((c) => [
        ...c,
        { role: "you", text: `🔍 focus: ${area}` },
        { role: "assetforge", text: `Detailed "${area}" — rest of the asset kept as-is.` },
      ]);
      setFocusArea("");
    });

  /** One-click "quick fix" presets: canned refine instructions run through
   * the existing refine stream (reuse the "refine" busy tag). The
   * connections audit also fetches the server's machine findings (floating
   * parts, dead declarations) so the AI fixes measured problems, not vibes. */
  const runPreset = (preset: { label: string; message: string }) =>
    run("refine", async () => {
      let message = preset.message;
      if (preset.label.includes("connections")) {
        const findings = await buildabilityFindings(spec);
        if (findings.length) {
          message += `\nMachine findings to fix first:\n- ${findings.join("\n- ")}`;
        }
      }
      const { spec: newSpec, changes } = await refineSpecStream(spec, message, setStreamText, model);
      const problem = onSpec(newSpec);
      if (problem) throw new Error(problem);
      setChat((c) => [
        ...c,
        { role: "you", text: `🔧 ${preset.label}` },
        { role: "assetforge", text: `Ran "${preset.label}".` },
        ...changeEntries(changes),
      ]);
    });

  /** Cached per spec: reopening the guide without changing the asset is
   * instant; "Regenerate" in the modal forces a fresh one. */
  const runGuide = (force = false) => {
    const key = JSON.stringify(spec);
    if (!force && guideCache.current?.key === key) {
      setGuide(guideCache.current.text);
      return;
    }
    void run("guide", async () => {
      setGuide(null);
      const text = await installGuideStream(spec, setStreamText);
      guideCache.current = { key, text };
      setGuide(text);
    });
  };

  const runStandardsUpdate = () =>
    run("standards", async () => {
      setStandardsResult(await updateStandardsStream(setStreamText));
    });

  const downloadSpec = () => {
    download(`${spec.name || spec.asset_type}.json`, JSON.stringify(spec, null, 2), "application/json");
  };

  return (
    <div className="panel">
      <div className="panel__header">
        <h2 className="brand">AssetForge</h2>
        <button
          className="theme-toggle"
          onClick={() => onTheme(theme === "dark" ? "light" : "dark")}
          title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
        >
          {theme === "dark" ? "☀️" : "🌙"}
        </button>
      </div>

      <label className="model-row" title="Load one of the bundled example assets">
        <span>Examples</span>
        <select
          value=""
          onChange={(e) => loadExample(e.target.value)}
          disabled={busy !== false}
        >
          <option value="" disabled>
            Load an example asset…
          </option>
          {EXAMPLE_ASSETS.map((e) => (
            <option key={e.id} value={e.id}>
              {e.label}
            </option>
          ))}
        </select>
      </label>

      <h3>Describe any asset</h3>
      <label className="model-row" title="Which DeepSeek model the AI uses for generate, refine, and focus">
        <span>AI model</span>
        <select
          value={model}
          onChange={(e) => setModel(e.target.value as DeepseekModel)}
          disabled={busy !== false}
        >
          {MODEL_OPTIONS.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label} — {m.hint}
            </option>
          ))}
        </select>
      </label>
      <textarea
        value={prompt}
        onChange={(e) => {
          setPrompt(e.target.value);
          // questions belong to the prompt they were asked about
          if (clarify && e.target.value.trim() !== clarify.forPrompt) setClarify(null);
        }}
        placeholder='e.g. "a 12 ft art-deco pedestrian lamp with a fluted cast-iron pole and a glowing acorn globe" — anything: benches, bollards, signs, props…'
        rows={4}
        disabled={busy !== false || clarifyBusy !== false}
      />
      <div className="gen-row">
        <button
          onClick={() => void startClarify("wizard")}
          disabled={busy !== false || clarifyBusy !== false || !prompt.trim()}
          title="Build in 4 reviewable steps: form → connections → materials → working parts"
        >
          {busy === "wizard" && wizardStep === null ? "Building…" : "🪄 Build step by step"}
        </button>
        <button
          className="secondary"
          onClick={() => void startClarify("generate")}
          disabled={busy !== false || clarifyBusy !== false || !prompt.trim()}
          title="Generate the whole asset in one pass"
        >
          {busy === "generate" ? "Generating…" : "Generate all at once"}
        </button>
      </div>

      {clarifyOpen && (
        <div className="modal-overlay" onClick={() => finishClarify(false)}>
          <div
            className="modal clarify-modal"
            role="dialog"
            aria-modal="true"
            aria-label="Clarifying questions"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal__header">
              <h3 className="clarify__title">
                🎯{" "}
                {clarify
                  ? `${clarify.questions.length === 1 ? "One quick question" : "A few quick questions"} first`
                  : "A few quick questions first"}
              </h3>
              <button
                className="close"
                onClick={() => finishClarify(false)}
                title="Skip and generate directly"
                aria-label="Skip and generate directly"
              >
                ✕
              </button>
            </div>
            <div className="modal__body">
              {clarify === null ? (
                <div className="clarify-modal__loading">
                  <span className="clarify-modal__spinner" aria-hidden="true" />
                  <p>Thinking of a few quick questions…</p>
                </div>
              ) : (
                <>
                  <p className="clarify__blurb">
                    So the AI designs what you actually meant — pick an answer, type
                    your own, or leave any as “no preference”.
                  </p>
                  {clarify.questions.map((q, i) => (
                    <div key={q.id} className="clarify__q">
                      {q.persona && (
                        <span className="clarify__persona" title={q.persona.label}>
                          {q.persona.icon} {q.persona.label}
                        </span>
                      )}
                      <span className="clarify__label">{q.question}</span>
                      <select
                        ref={i === 0 ? clarifyFirstSelectRef : undefined}
                        value={clarify.choices[i]}
                        onChange={(e) => setClarifyChoice(i, e.target.value)}
                        disabled={busy !== false}
                      >
                        <option value="">No preference</option>
                        {q.options.map((o) => (
                          <option key={o} value={o}>
                            {o}
                          </option>
                        ))}
                        <option value={CUSTOM_ANSWER}>✏️ My own answer…</option>
                      </select>
                      {clarify.choices[i] === CUSTOM_ANSWER && (
                        <input
                          type="text"
                          className="clarify__custom"
                          value={clarify.custom[i]}
                          onChange={(e) => setClarifyCustom(i, e.target.value)}
                          placeholder="type exactly what you want"
                          maxLength={300}
                          disabled={busy !== false}
                        />
                      )}
                    </div>
                  ))}
                </>
              )}
              <div className="clarify__actions">
                <button
                  className="secondary"
                  onClick={() => finishClarify(false)}
                  disabled={busy !== false || clarify === null}
                >
                  Skip — just generate
                </button>
                <button onClick={() => finishClarify(true)} disabled={busy !== false || clarify === null}>
                  {clarify?.mode === "wizard" ? "🪄 Build with these answers" : "Generate with these answers"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {wizardStep !== null && (
        <div className="wizard">
          <div className="wizard__steps">
            {WIZARD_STEPS.map((s, i) => (
              <div
                key={s.key}
                className={`wizard__pill${
                  i === wizardStep ? " is-current" : i < wizardStep ? " is-done" : ""
                }`}
                title={s.blurb}
              >
                <span className="wizard__pill-n">{i < wizardStep ? "✓" : s.n}</span>
                {s.short}
              </div>
            ))}
          </div>
          <div className="wizard__body">
            <h4 className="wizard__title">
              {WIZARD_STEPS[wizardStep].icon} Step {WIZARD_STEPS[wizardStep].n} of 4 —{" "}
              {WIZARD_STEPS[wizardStep].title}
            </h4>
            <p className="wizard__blurb">{WIZARD_STEPS[wizardStep].blurb}</p>
            <textarea
              value={wizardMsg}
              onChange={(e) => setWizardMsg(e.target.value)}
              placeholder={WIZARD_STEPS[wizardStep].placeholder}
              rows={2}
              disabled={busy !== false}
            />
            <div className="wizard__actions">
              <button
                className="secondary"
                onClick={applyWizardChange}
                disabled={busy !== false || !wizardMsg.trim()}
                title="Apply this change and stay on this step"
              >
                {busy === "wizard" ? "…" : "Apply change"}
              </button>
              <button
                onClick={acceptWizardStep}
                disabled={busy !== false}
                title="Accept this step as-is and move on"
              >
                {busy === "wizard" ? "Working…" : WIZARD_STEPS[wizardStep].accept}
              </button>
            </div>
            <button
              className="wizard__exit"
              onClick={() => {
                setWizardStep(null);
                setWizardMsg("");
              }}
              disabled={busy !== false}
            >
              Exit guided build (keep what's here)
            </button>
          </div>
        </div>
      )}

      {wizardStep === null && (
        <div className="quick-fixes">
          <span className="quick-fixes__label">Quick fixes (AI, on the current asset)</span>
          <div className="quick-fixes__row">
            {QUICK_FIXES.map((qf) => (
              <button
                key={qf.label}
                className="quick-fix-btn"
                title={qf.title}
                onClick={() => runPreset(qf)}
                disabled={busy !== false}
              >
                {qf.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {wizardStep === null && (
        <div className="variations-trigger">
          <button
            className="variations-trigger__btn"
            onClick={onVariations}
            disabled={busy !== false || clarifyBusy !== false || variationsBusy}
            title="Ask the AI for 4 alternate takes on the current asset, preview each in 3D, and pick one to adopt"
          >
            {variationsBusy ? "✨ Requesting 4 variants…" : "✨ Give me 4 variants"}
          </button>
        </div>
      )}

      {chat.length > 0 && (
        <div className="chat">
          {chat.map((entry, i) => (
            <p key={i} className={`chat__msg chat__msg--${entry.role}`}>
              <strong>{entry.role === "you" ? "You" : "AssetForge"}:</strong> {entry.text}
            </p>
          ))}
        </div>
      )}

      {chat.length > 0 && wizardStep === null && (
        <>
          <div className="refine-row">
            <input
              type="text"
              value={refineMsg}
              onChange={(e) => setRefineMsg(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && runRefine()}
              placeholder='refine: "make it bronze, add a second arm"'
              disabled={busy !== false}
            />
            <button onClick={runRefine} disabled={busy !== false || !refineMsg.trim()}>
              {busy === "refine" ? "…" : "Send"}
            </button>
          </div>

          <div className="focus-box">
            <span className="focus-box__label">🔍 Focus one area (deep detail, rest untouched)</span>
            <textarea
              value={focusArea}
              onChange={(e) => setFocusArea(e.target.value)}
              placeholder='e.g. "the luminaire head — add a hinged door, gasket, and internal reflector"'
              rows={2}
              disabled={busy !== false}
            />
            <button onClick={runFocus} disabled={busy !== false || !focusArea.trim()}>
              {busy === "focus" ? "Detailing…" : "Focus this area"}
            </button>
          </div>
        </>
      )}

      {busy !== false && <StreamCard title={BUSY_TITLES[busy]} text={streamText} />}

      {error && (
        <div className="violation" role="alert">
          <p>{error}</p>
        </div>
      )}

      <hr />
      <label className="control">
        <span className="control__label">Asset name</span>
        <input type="text" value={spec.name} onChange={(e) => onName(e.target.value)} />
      </label>
      <p className="asset-type">
        type: <code>{spec.asset_type}</code>
      </p>
      <div className={`code-status ${violationCount ? "code-status--bad" : "code-status--ok"}`}>
        {violationCount
          ? `${violationCount} code violation${violationCount > 1 ? "s" : ""} — see controls`
          : "All dimensions within US code"}
      </div>

      <button onClick={downloadSpec}>Download spec (.json)</button>
      <button onClick={() => runGuide()} disabled={busy !== false}>
        {busy === "guide" ? "Writing guide…" : "📋 Installation guide"}
      </button>
      <button onClick={runStandardsUpdate} disabled={busy !== false} className="secondary">
        {busy === "standards" ? "Researching standards…" : "🏛 Refresh US standards DB"}
      </button>
      <p className="hint">
        Turn the spec into a real Blender / SketchUp file (see README → “Open
        your asset in Blender”):
        <code>blender -b -P blender/build_cli.py -- spec.json out.blend</code>
      </p>

      {guide !== null && (
        <Modal title={`Installing "${spec.name}"`} onClose={() => setGuide(null)}>
          <GuideText text={guide} />
          <div className="modal__actions">
            <button onClick={() => download(`${spec.name || "asset"}-install-guide.md`, guide, "text/markdown")}>
              Download guide (.md)
            </button>
            <button
              className="secondary-btn"
              onClick={() => runGuide(true)}
              title="Write a fresh guide even though the asset hasn't changed"
            >
              Regenerate
            </button>
          </div>
        </Modal>
      )}

      {standardsResult !== null && (
        <Modal title="US standards DB refresh" onClose={() => setStandardsResult(null)}>
          <p className={standardsResult.committed ? "code-status code-status--ok" : "code-status code-status--bad"}>
            {standardsResult.committed
              ? `Committed to GitHub — the app picks it up on the next deploy. ${standardsResult.detail}`
              : standardsResult.detail}
          </p>
          {standardsResult.url && (
            <p>
              <a href={standardsResult.url} target="_blank" rel="noreferrer">
                View the commit on GitHub
              </a>
            </p>
          )}
          <p className="hint">{standardsResult.note}</p>
          <h4>Proposed changes ({standardsResult.changes.length})</h4>
          {standardsResult.changes.length === 0 && <p>No changes proposed — the DB is up to date.</p>}
          <ul className="changes">
            {standardsResult.changes.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
          {!standardsResult.committed && (
            <div className="modal__actions">
              <button
                onClick={() =>
                  download("us_codes.json", JSON.stringify(standardsResult.proposal, null, 2), "application/json")
                }
              >
                Download proposed us_codes.json
              </button>
              <p className="hint">
                Review it, then replace <code>standards/us_codes.json</code> in
                the GitHub repository (open the file → pencil icon → paste →
                Commit changes).
              </p>
            </div>
          )}
        </Modal>
      )}
    </div>
  );
}
