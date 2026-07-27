/** Compact "AI is streaming" ticker shared by every place that shows a live
 * SSE stream (generate/refine/focus/guide/standards in PromptPanel, the AI
 * connection review and ✨ Improve panels in App.tsx): an animated status dot
 * + a title that never wraps, with the freshest streamed characters trailing
 * on the SAME line. `.stream-card__tail` anchors its text to the right and
 * clips overflow on the left (see styles.css), so as new characters stream
 * in, the newest ones stay pinned at the visible right edge instead of the
 * box scrolling or growing taller. Reasoning ("thinking") models wrap their
 * chain of thought in <think> tags — stripped for display here, flagged with
 * "· thinking…" while the tag is still open.
 *
 * When `onCancel` is given, an ✕ renders beside the title: it aborts the
 * in-flight request and the caller applies NOTHING (see api.ts
 * `isAbortError`) — a cancel leaves the current asset exactly as it was. */
export default function StreamLine({
  title,
  text,
  onCancel,
}: {
  title: string;
  text: string;
  /** Abort the in-flight request; omit where the call can't be cancelled. */
  onCancel?: () => void;
}) {
  const thinking = text.lastIndexOf("<think>") > text.lastIndexOf("</think>");
  const clean = text.replace(/<\/?think>/g, "");
  return (
    <div className="stream-card" aria-live="off">
      <div className="stream-card__title">
        <span className="stream-card__dot" /> {title}
        {thinking && <span className="stream-card__thinking"> · thinking…</span>}
      </div>
      <div className="stream-card__tail">{clean.slice(-200) || "…"}</div>
      {/* a sibling of the title (not a child) so a long title can never push
          the cancel out of the card — the tail between them absorbs the slack */}
      {onCancel && (
        <button
          type="button"
          className="stream-card__cancel"
          onClick={onCancel}
          title="Cancel this request — nothing will be changed"
          aria-label="Cancel this request"
        >
          ✕
        </button>
      )}
    </div>
  );
}
