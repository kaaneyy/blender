/** Left panel: describe any asset → AI generates a spec (streamed live);
 * keep refining conversationally. Also hosts the installation-guide and
 * standards-refresh tools, both streamed. The guide is cached per spec so
 * reopening it costs nothing when the asset hasn't changed. */
import { useEffect, useRef, useState } from "react";
import type { AssetSpec } from "../types";
import type { CodeViolation } from "../standards";
import {
  focusSpecStream,
  generateSpecStream,
  installGuideStream,
  refineSpecStream,
  updateStandardsStream,
  MODEL_OPTIONS,
  type DeepseekModel,
  type StandardsUpdateResult,
} from "../api";
import Modal from "./Modal";

interface ChatEntry {
  role: "you" | "assetforge";
  text: string;
}

type Busy = false | "generate" | "refine" | "focus" | "guide" | "standards";

const BUSY_TITLES: Record<Exclude<Busy, false>, string> = {
  generate: "Generating your asset…",
  refine: "Applying your change…",
  focus: "Detailing that area…",
  guide: "Writing the installation guide…",
  standards: "Researching standards…",
};

const MODEL_KEY = "af-model";

/** One-click quick-fix presets. Each `message` is a canned refine
 * instruction (kept well under the 2000-char API cap). */
const QUICK_FIXES: Array<{ label: string; title: string; message: string }> = [
  {
    label: "🔗 Check & fix connections",
    title: "Audit every joint: real load path, parts actually touch, fasteners appropriate to the material",
    message:
      "Audit and fix EVERY connection in this asset, then return the FULL updated AssetSpec JSON (keep ids/values stable where unchanged). " +
      "1) Load path: every part must be supported down to the ground (z=0); add base plates, rails, aprons, stretchers, brackets or collars where a part has nothing to attach to. " +
      "2) Contact: joined parts must interpenetrate 10-20 mm — fix any parts that float or merely touch at a zero-thickness face so the app can place hardware where they truly overlap. " +
      "3) Appropriateness: match the fastening to the asset and its materials. Light-duty or non-structural items (a basic table, wooden furniture, decorative props) must NOT show industrial anchor bolts — set the connection_hardware toggle OFF for them and rely on integral/joinery connections. Reserve visible bolted hardware for structural metal assets (poles, signs, heavy frames), and keep members sized so hardware is not oversized. Nothing below z=0.",
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
 * can see progress without needing to read it. */
function StreamCard({ title, text }: { title: string; text: string }) {
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    boxRef.current?.scrollTo({ top: boxRef.current.scrollHeight });
  }, [text]);
  return (
    <div className="stream-card" aria-live="off">
      <div className="stream-card__title">
        <span className="stream-card__dot" /> {title}
      </div>
      <div className="stream-card__text" ref={boxRef}>
        {text.slice(-800) || "…"}
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
}: {
  spec: AssetSpec;
  violations: Record<string, CodeViolation>;
  theme: "light" | "dark";
  onTheme: (t: "light" | "dark") => void;
  onName: (name: string) => void;
  onSpec: (spec: AssetSpec) => string | null; // returns error message if spec unusable
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
  const guideCache = useRef<{ key: string; text: string } | null>(null);
  const violationCount = Object.keys(violations).length;

  useEffect(() => {
    localStorage.setItem(MODEL_KEY, model);
  }, [model]);

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

  const runGenerate = () =>
    run("generate", async () => {
      const text = prompt.trim();
      if (!text) return;
      const { spec: newSpec, brief } = await generateSpecStream(text, setStreamText, model);
      const problem = onSpec(newSpec);
      if (problem) throw new Error(problem);
      const entries: ChatEntry[] = [{ role: "you", text }];
      if (brief && brief.toLowerCase() !== text.toLowerCase()) {
        const shown = brief.length > 220 ? `${brief.slice(0, 220)}…` : brief;
        entries.push({ role: "assetforge", text: `Interpreted as: ${shown}` });
      }
      entries.push({
        role: "assetforge",
        text: `Built "${newSpec.name}" (${newSpec.asset_type}). Refine it below or tweak the sliders.`,
      });
      setChat(entries);
      setPrompt("");
    });

  const runRefine = () =>
    run("refine", async () => {
      const msg = refineMsg.trim();
      if (!msg) return;
      const newSpec = await refineSpecStream(spec, msg, setStreamText, model);
      const problem = onSpec(newSpec);
      if (problem) throw new Error(problem);
      setChat((c) => [
        ...c,
        { role: "you", text: msg },
        { role: "assetforge", text: `Updated "${newSpec.name}".` },
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
   * the existing refine stream (reuse the "refine" busy tag). */
  const runPreset = (preset: { label: string; message: string }) =>
    run("refine", async () => {
      const newSpec = await refineSpecStream(spec, preset.message, setStreamText, model);
      const problem = onSpec(newSpec);
      if (problem) throw new Error(problem);
      setChat((c) => [
        ...c,
        { role: "you", text: `🔧 ${preset.label}` },
        { role: "assetforge", text: `Ran "${preset.label}".` },
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
        onChange={(e) => setPrompt(e.target.value)}
        placeholder='e.g. "a 12 ft art-deco pedestrian lamp with a fluted cast-iron pole and a glowing acorn globe" — anything: benches, bollards, signs, props…'
        rows={4}
        disabled={busy !== false}
      />
      <button onClick={runGenerate} disabled={busy !== false || !prompt.trim()}>
        {busy === "generate" ? "Generating…" : "Generate"}
      </button>

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

      {chat.length > 0 && (
        <>
          <div className="chat">
            {chat.map((entry, i) => (
              <p key={i} className={`chat__msg chat__msg--${entry.role}`}>
                <strong>{entry.role === "you" ? "You" : "AssetForge"}:</strong> {entry.text}
              </p>
            ))}
          </div>
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
