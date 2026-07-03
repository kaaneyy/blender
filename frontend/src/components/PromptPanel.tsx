/** Left panel: describe any asset → AI generates a spec (streamed live);
 * keep refining conversationally. Also hosts the installation-guide and
 * standards-refresh tools, both streamed. The guide is cached per spec so
 * reopening it costs nothing when the asset hasn't changed. */
import { useEffect, useRef, useState } from "react";
import type { AssetSpec } from "../types";
import type { CodeViolation } from "../standards";
import {
  generateSpecStream,
  installGuideStream,
  refineSpecStream,
  updateStandardsStream,
  type StandardsUpdateResult,
} from "../api";
import Modal from "./Modal";

interface ChatEntry {
  role: "you" | "assetforge";
  text: string;
}

type Busy = false | "generate" | "refine" | "guide" | "standards";

const BUSY_TITLES: Record<Exclude<Busy, false>, string> = {
  generate: "Generating your asset…",
  refine: "Applying your change…",
  guide: "Writing the installation guide…",
  standards: "Researching standards…",
};

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
  const [busy, setBusy] = useState<Busy>(false);
  const [streamText, setStreamText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [chat, setChat] = useState<ChatEntry[]>([]);
  const [guide, setGuide] = useState<string | null>(null);
  const [standardsResult, setStandardsResult] = useState<StandardsUpdateResult | null>(null);
  const guideCache = useRef<{ key: string; text: string } | null>(null);
  const violationCount = Object.keys(violations).length;

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
      const newSpec = await generateSpecStream(text, setStreamText);
      const problem = onSpec(newSpec);
      if (problem) throw new Error(problem);
      setChat([
        { role: "you", text },
        { role: "assetforge", text: `Built "${newSpec.name}" (${newSpec.asset_type}). Refine it below or tweak the sliders.` },
      ]);
      setPrompt("");
    });

  const runRefine = () =>
    run("refine", async () => {
      const msg = refineMsg.trim();
      if (!msg) return;
      const newSpec = await refineSpecStream(spec, msg, setStreamText);
      const problem = onSpec(newSpec);
      if (problem) throw new Error(problem);
      setChat((c) => [
        ...c,
        { role: "you", text: msg },
        { role: "assetforge", text: `Updated "${newSpec.name}".` },
      ]);
      setRefineMsg("");
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
