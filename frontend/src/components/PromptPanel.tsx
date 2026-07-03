/** Left panel (T4.1 + Phase 2 client): describe any asset → AI generates a
 * spec; then keep refining it conversationally. */
import { useState } from "react";
import type { AssetSpec } from "../types";
import type { CodeViolation } from "../standards";
import {
  generateSpec,
  installGuide,
  refineSpec,
  updateStandards,
  type StandardsUpdateResult,
} from "../api";
import Modal from "./Modal";

interface ChatEntry {
  role: "you" | "assetforge";
  text: string;
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
  const [busy, setBusy] = useState<false | "generate" | "refine" | "guide" | "standards">(false);
  const [error, setError] = useState<string | null>(null);
  const [chat, setChat] = useState<ChatEntry[]>([]);
  const [guide, setGuide] = useState<string | null>(null);
  const [standardsResult, setStandardsResult] = useState<StandardsUpdateResult | null>(null);
  const violationCount = Object.keys(violations).length;

  const runGuide = async () => {
    if (busy) return;
    setBusy("guide");
    setError(null);
    try {
      setGuide(await installGuide(spec));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const runStandardsUpdate = async () => {
    if (busy) return;
    setBusy("standards");
    setError(null);
    try {
      setStandardsResult(await updateStandards());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const runGenerate = async () => {
    if (!prompt.trim() || busy) return;
    setBusy("generate");
    setError(null);
    try {
      const newSpec = await generateSpec(prompt.trim());
      const problem = onSpec(newSpec);
      if (problem) throw new Error(problem);
      setChat([
        { role: "you", text: prompt.trim() },
        { role: "assetforge", text: `Built "${newSpec.name}" (${newSpec.asset_type}). Refine it below or tweak the sliders.` },
      ]);
      setPrompt("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const runRefine = async () => {
    if (!refineMsg.trim() || busy) return;
    setBusy("refine");
    setError(null);
    const msg = refineMsg.trim();
    try {
      const newSpec = await refineSpec(spec, msg);
      const problem = onSpec(newSpec);
      if (problem) throw new Error(problem);
      setChat((c) => [
        ...c,
        { role: "you", text: msg },
        { role: "assetforge", text: `Updated "${newSpec.name}".` },
      ]);
      setRefineMsg("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const downloadSpec = () => {
    const blob = new Blob([JSON.stringify(spec, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${spec.name || spec.asset_type}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
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
      <textarea
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        placeholder='e.g. "a 12 ft art-deco pedestrian lamp with a fluted cast-iron pole and a glowing acorn globe" — anything: benches, bollards, signs, props…'
        rows={4}
        disabled={busy !== false}
      />
      <button onClick={runGenerate} disabled={busy !== false || !prompt.trim()}>
        {busy === "generate" ? "Generating… (10–30 s)" : "Generate"}
      </button>

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
        </>
      )}

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
      <button onClick={runGuide} disabled={busy !== false}>
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
