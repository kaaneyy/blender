/** "📚 Library": a modal listing specs saved to the local asset library (the
 * frozen store in ../library/store.ts), independent of the single-slot
 * autosave and the one-file Save/Open flow. Lets the user save the current
 * design under a name + tags, search/filter saved entries, load one, fork
 * one, or delete one.
 *
 * Load (and any future "load the thing I just forked") routes through the
 * app's one validated spec-swap path (adoptSpec, passed in as `onAdopt`) —
 * never a direct setSpec — so an entry that somehow fails to build can never
 * blank the viewport; the failure surfaces inline and the panel stays open.
 * This mirrors VariationsPanel's onPick contract exactly. */
import { useMemo, useState } from "react";
import type { AssetSpec } from "../types";
import {
  allTags,
  forkEntry,
  listEntries,
  removeEntry,
  saveEntry,
  searchEntries,
  type LibraryEntry,
} from "../library/store";

/** Split a comma/space-separated tags input into trimmed, de-duped,
 * non-empty tags, preserving first-seen order. */
function parseTags(input: string): string[] {
  const seen = new Set<string>();
  const tags: string[] = [];
  for (const raw of input.split(/[,\s]+/)) {
    const t = raw.trim();
    if (!t || seen.has(t)) continue;
    seen.add(t);
    tags.push(t);
  }
  return tags;
}

export default function LibraryPanel({
  spec,
  onAdopt,
  onClose,
}: {
  /** The app's current live spec — what "Save to library" captures. */
  spec: AssetSpec;
  /** Adopt a loaded/forked spec — the SAME validated path (App's adoptSpec)
   * every other spec swap uses. Returns an error string on failure, null on
   * success. */
  onAdopt: (spec: AssetSpec) => string | null;
  onClose: () => void;
}) {
  const [entries, setEntries] = useState<LibraryEntry[]>(() => listEntries());
  const [query, setQuery] = useState("");
  const [activeTags, setActiveTags] = useState<string[]>([]);
  const [saveName, setSaveName] = useState(spec.name ?? "");
  const [saveTagsInput, setSaveTagsInput] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = () => setEntries(listEntries());

  const tags = useMemo(() => allTags(entries), [entries]);
  const filtered = useMemo(
    () => searchEntries(entries, query, activeTags),
    [entries, query, activeTags],
  );

  const toggleTag = (t: string) => {
    setActiveTags((prev) => (prev.includes(t) ? prev.filter((x) => x !== t) : [...prev, t]));
  };

  const handleSave = () => {
    const name = saveName.trim() || spec.name || "Untitled";
    const parsedTags = parseTags(saveTagsInput);
    saveEntry({ name, tags: parsedTags, spec });
    setSaveTagsInput("");
    refresh();
  };

  const handleLoad = (entry: LibraryEntry) => {
    if (loading) return;
    setLoading(true);
    const err = onAdopt(entry.spec);
    if (err) {
      setLoadError(err);
      setLoading(false);
    } else {
      setLoadError(null);
      onClose();
    }
  };

  const handleFork = (id: string) => {
    forkEntry(id);
    refresh();
  };

  const handleDelete = (id: string) => {
    removeEntry(id);
    refresh();
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal library-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Asset library"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h3>📚 Library</h3>
          <button className="close" onClick={onClose} title="Close">
            ✕
          </button>
        </div>
        <div className="modal__body library-modal__body">
          <div className="library-save">
            <h4>Save current design</h4>
            <div className="library-save__row">
              <input
                type="text"
                className="library-save__name"
                value={saveName}
                onChange={(e) => setSaveName(e.target.value)}
                placeholder="Name"
              />
              <input
                type="text"
                className="library-save__tags"
                value={saveTagsInput}
                onChange={(e) => setSaveTagsInput(e.target.value)}
                placeholder="tags, comma or space separated"
              />
              <button onClick={handleSave}>💾 Save to library</button>
            </div>
          </div>

          <div className="library-filters">
            <input
              type="text"
              className="library-search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search name or tag…"
            />
            {tags.length > 0 && (
              <div className="library-tags">
                {tags.map((t) => (
                  <button
                    key={t}
                    className={`library-tag-chip${activeTags.includes(t) ? " is-active" : ""}`}
                    onClick={() => toggleTag(t)}
                  >
                    {t}
                  </button>
                ))}
              </div>
            )}
          </div>

          {loadError && (
            <div className="violation" role="alert">
              <p>{loadError}</p>
            </div>
          )}

          {filtered.length === 0 ? (
            <p className="hint">
              {entries.length === 0
                ? "No saved designs yet — save the current design above."
                : "No saved designs match your search."}
            </p>
          ) : (
            <div className="library-list">
              {filtered.map((entry) => (
                <div className="library-row" key={entry.id}>
                  <div className="library-row__info">
                    <div className="library-row__name">{entry.name}</div>
                    {entry.tags.length > 0 && (
                      <div className="library-row__tags">{entry.tags.join(", ")}</div>
                    )}
                    <div className="library-row__time">
                      {new Date(entry.updatedAt).toLocaleString()}
                    </div>
                  </div>
                  <div className="library-row__actions">
                    <button onClick={() => handleLoad(entry)} disabled={loading}>
                      Load
                    </button>
                    <button onClick={() => handleFork(entry.id)}>Fork</button>
                    <button className="reset" onClick={() => handleDelete(entry.id)}>
                      Delete
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
