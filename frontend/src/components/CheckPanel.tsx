/** Connection-check panel: renders the deterministic audit report (from
 * builders/audit.ts) with a checkbox per fixable finding. NOTHING is applied
 * until the user clicks "Apply" — and hovering that button first previews
 * the fixed asset live in the 3D view (App swaps the rendered spec) while a
 * tooltip lists exactly what changes, before → after. */
import { useState } from "react";
import type { AuditFinding, AuditReport } from "../builders";

const SEV_ICON: Record<string, string> = { error: "⛔", warning: "⚠️" };

export default function CheckPanel({
  report,
  isChecked,
  onToggleFinding,
  onHoverFinding,
  onPreview,
  onApply,
  onClose,
  fixable,
}: {
  report: AuditReport;
  /** whether a finding's fix is selected for application */
  isChecked: (f: AuditFinding) => boolean;
  onToggleFinding: (f: AuditFinding) => void;
  /** hovering a finding highlights its component/joint in the 3D view */
  onHoverFinding: (f: AuditFinding | null) => void;
  /** hovering the Apply button previews the fixed asset in the 3D view */
  onPreview: (on: boolean) => void;
  onApply: () => void;
  onClose: () => void;
  /** findings whose fix is currently selected (drives the tooltip) */
  fixable: AuditFinding[];
}) {
  const [tooltip, setTooltip] = useState(false);
  const errors = report.findings.filter((f) => f.severity === "error").length;
  const warnings = report.findings.length - errors;
  const clean = report.findings.length === 0;

  const enterApply = () => {
    setTooltip(true);
    onPreview(true);
  };
  const leaveApply = () => {
    setTooltip(false);
    onPreview(false);
  };

  return (
    <div className="panel check-panel">
      <div className="panel__header">
        <h3>🔍 Connection check</h3>
        <button className="close" onClick={onClose} title="Close the connection check">
          ✕
        </button>
      </div>

      <div className={`code-status ${clean ? "code-status--ok" : "code-status--bad"}`}>
        {clean
          ? `All clear — ${report.joints} joint${report.joints === 1 ? "" : "s"} across ${report.components} components look buildable`
          : `${errors} problem${errors === 1 ? "" : "s"}, ${warnings} warning${warnings === 1 ? "" : "s"} across ${report.joints} joint${report.joints === 1 ? "" : "s"}`}
      </div>
      {clean && (
        <p className="hint">
          Every part has a load path to the ground, every declared joint has
          real contact, and all generated hardware bears on the members it
          joins.
        </p>
      )}

      {report.findings.map((f) => (
        <div
          key={f.id}
          className={`finding finding--${f.severity}`}
          onMouseEnter={() => onHoverFinding(f)}
          onMouseLeave={() => onHoverFinding(null)}
        >
          <label className="finding__head">
            {f.fix ? (
              <input
                type="checkbox"
                checked={isChecked(f)}
                onChange={() => onToggleFinding(f)}
                title="Include this fix when applying"
              />
            ) : (
              <span
                className="finding__nofix"
                title="No safe automatic fix — adjust this one by hand (or re-check after applying the others)"
              >
                ✋
              </span>
            )}
            <span className="finding__title">
              {SEV_ICON[f.severity]} {f.title}
            </span>
          </label>
          <p className="finding__detail">{f.detail}</p>
          {f.fix && <p className="finding__fixline">🔧 {f.fix.summary}</p>}
        </div>
      ))}

      {!clean && (
        <div className="check-apply">
          <button
            className="check-apply__btn"
            disabled={fixable.length === 0}
            onMouseEnter={enterApply}
            onMouseLeave={leaveApply}
            onFocus={enterApply}
            onBlur={leaveApply}
            onClick={() => {
              leaveApply();
              onApply();
            }}
            title="Nothing changes until you click — hover to preview the result in 3D"
          >
            {fixable.length === 0
              ? "No fixes selected"
              : `Apply ${fixable.length} fix${fixable.length === 1 ? "" : "es"}`}
          </button>
          {tooltip && fixable.length > 0 && (
            <div className="check-apply__tooltip" role="tooltip">
              <p className="check-apply__tooltip-head">
                The 3D view is now showing the result. Clicking will change:
              </p>
              <ul>
                {fixable.map((f) => (
                  <li key={f.id}>
                    <strong>{f.fix!.summary}</strong>
                    <span className="check-apply__diff">
                      {f.fix!.before} <em>→</em> {f.fix!.after}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <p className="hint">
            Hover the button to preview the fixes in the 3D view — nothing is
            applied until you click. Findings without a checkbox need a manual
            edit.
          </p>
        </div>
      )}
    </div>
  );
}
