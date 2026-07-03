/** Left panel (T4.1 + Phase 2 client): describe any asset → AI generates a
 * spec; then keep refining it conversationally. */
import { useState } from "react";
import type { AssetSpec } from "../types";
import type { CodeViolation } from "../standards";
import { generateSpec, refineSpec } from "../api";

interface ChatEntry {
  role: "you" | "assetforge";
  text: string;
}

export default function PromptPanel({
  spec,
  violations,
  onName,
  onSpec,
}: {
  spec: AssetSpec;
  violations: Record<string, CodeViolation>;
  onName: (name: string) => void;
  onSpec: (spec: AssetSpec) => string | null; // returns error message if spec unusable
}) {
  const [prompt, setPrompt] = useState("");
  const [refineMsg, setRefineMsg] = useState("");
  const [busy, setBusy] = useState<false | "generate" | "refine">(false);
  const [error, setError] = useState<string | null>(null);
  const [chat, setChat] = useState<ChatEntry[]>([]);
  const violationCount = Object.keys(violations).length;

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
      <h2 className="brand">AssetForge</h2>

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
      <p className="hint">
        Turn the spec into a real Blender / SketchUp file (see README → “Open
        your asset in Blender”):
        <code>blender -b -P blender/build_cli.py -- spec.json out.blend</code>
      </p>
    </div>
  );
}
