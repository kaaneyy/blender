/** Left panel (T4.1): asset identity, spec download, and the prompt box —
 * disabled until the LLM layer ships (milestone 3, Phase 2). */
import type { AssetSpec } from "../types";
import type { CodeViolation } from "../standards";

export default function PromptPanel({
  spec,
  violations,
  onName,
}: {
  spec: AssetSpec;
  violations: Record<string, CodeViolation>;
  onName: (name: string) => void;
}) {
  const violationCount = Object.keys(violations).length;

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

      <h3>Describe an asset</h3>
      <textarea
        disabled
        placeholder={'"a 30 ft cobra-head street light with a banner bracket"\n\nAI prompt → spec ships in milestone 3 (Phase 2 of docs/BUILD_PLAN.md). Until then, use the controls on the right.'}
        rows={6}
      />

      <button onClick={downloadSpec}>Download spec (.json)</button>
      <p className="hint">
        Build the downloaded spec headless for SketchUp:
        <code>
          blender -b -P blender/build_cli.py -- spec.json out.dae
        </code>
      </p>
    </div>
  );
}
