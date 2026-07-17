/** Versioned localStorage asset library: a named, tagged set of saved specs,
 * independent of the single-slot autosave (App.tsx's AUTOSAVE_KEY,
 * "af-spec-autosave"). This module never reads/writes/clears that key —
 * autosave and the library are separate features that happen to both live
 * in localStorage.
 *
 * Corruption/version resilience mirrors App.tsx's loadAutosavedSpec: a
 * JSON.parse failure, a non-array payload, or an entry with the wrong shape
 * is treated as an empty library and the bad key is cleared; every
 * localStorage access is wrapped so quota errors or private-mode storage
 * bans degrade to "empty library" / a best-effort (silently dropped) write
 * rather than throwing into caller code. */
import type { AssetSpec } from "../types";

/** localStorage key the library is stored under. Distinct from and never
 * shared with AUTOSAVE_KEY ("af-spec-autosave") in App.tsx. */
const LIBRARY_KEY = "af-library-v1";

export interface LibraryEntry {
  id: string;
  name: string;
  tags: string[];
  spec: AssetSpec;
  createdAt: number;
  updatedAt: number;
}

/** Deep-clone a spec so a later mutation of the app's live spec object can
 * never retroactively edit a saved library entry. Prefers structuredClone
 * (widely available in modern browsers); falls back to a JSON round-trip
 * for environments without it. */
function cloneSpec(spec: AssetSpec): AssetSpec {
  if (typeof structuredClone === "function") {
    return structuredClone(spec);
  }
  return JSON.parse(JSON.stringify(spec)) as AssetSpec;
}

/** Collision-safe id generator: crypto.randomUUID() when available, else a
 * timestamp+counter fallback that's unique within this session. */
let fallbackCounter = 0;
function makeId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  fallbackCounter += 1;
  return `lib-${Date.now().toString(36)}-${fallbackCounter}-${Math.random().toString(36).slice(2, 8)}`;
}

/** Structural check that an unknown parsed value is a plausible
 * LibraryEntry — just enough to keep a corrupt/old-shape stored value from
 * poisoning the library, mirroring the tolerant-but-not-exhaustive style of
 * validateSpecForAdoption in App.tsx. */
function isLibraryEntry(value: unknown): value is LibraryEntry {
  if (!value || typeof value !== "object") return false;
  const e = value as Record<string, unknown>;
  return (
    typeof e.id === "string" &&
    typeof e.name === "string" &&
    Array.isArray(e.tags) &&
    e.tags.every((t) => typeof t === "string") &&
    typeof e.spec === "object" &&
    e.spec !== null &&
    typeof e.createdAt === "number" &&
    typeof e.updatedAt === "number"
  );
}

/** Read + validate the stored library. A corrupt or wrong-shape payload is
 * discarded silently (the key is cleared) so it can never come back to bite
 * a later load. Any localStorage failure (quota, private mode, disabled
 * storage) degrades to an empty library rather than throwing. */
function readAll(): LibraryEntry[] {
  try {
    const raw = localStorage.getItem(LIBRARY_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      localStorage.removeItem(LIBRARY_KEY);
      return [];
    }
    const entries = parsed.filter(isLibraryEntry);
    if (entries.length !== parsed.length) {
      // Some entries were malformed; persist only the valid ones so the
      // corruption doesn't recur on every subsequent read.
      writeAll(entries);
    }
    return entries;
  } catch {
    try {
      localStorage.removeItem(LIBRARY_KEY);
    } catch {
      /* storage unavailable — nothing to clear, nothing to crash */
    }
    return [];
  }
}

/** Best-effort write: swallows quota/private-mode failures rather than
 * throwing into caller code. */
function writeAll(entries: LibraryEntry[]): void {
  try {
    localStorage.setItem(LIBRARY_KEY, JSON.stringify(entries));
  } catch {
    /* quota exceeded / storage disabled — best effort, drop silently */
  }
}

/** All saved entries, newest-updated first. */
export function listEntries(): LibraryEntry[] {
  return readAll().sort((a, b) => b.updatedAt - a.updatedAt);
}

/** A single entry by id, or null if it doesn't exist (including when the
 * whole library failed to load). */
export function getEntry(id: string): LibraryEntry | null {
  return readAll().find((e) => e.id === id) ?? null;
}

/** Save a brand-new entry: fresh id, createdAt = updatedAt = now, and a
 * deep-cloned spec so the caller's live spec object can't later mutate it. */
export function saveEntry(input: { name: string; tags: string[]; spec: AssetSpec }): LibraryEntry {
  const now = Date.now();
  const entry: LibraryEntry = {
    id: makeId(),
    name: input.name,
    tags: [...input.tags],
    spec: cloneSpec(input.spec),
    createdAt: now,
    updatedAt: now,
  };
  const entries = readAll();
  entries.push(entry);
  writeAll(entries);
  return entry;
}

/** Patch an existing entry's name/tags/spec, bumping updatedAt. Returns the
 * updated entry, or null if no entry with that id exists. A patched spec is
 * deep-cloned like on save. */
export function updateEntry(
  id: string,
  patch: Partial<Pick<LibraryEntry, "name" | "tags" | "spec">>,
): LibraryEntry | null {
  const entries = readAll();
  const idx = entries.findIndex((e) => e.id === id);
  if (idx === -1) return null;
  const current = entries[idx];
  const updated: LibraryEntry = {
    ...current,
    ...(patch.name !== undefined ? { name: patch.name } : {}),
    ...(patch.tags !== undefined ? { tags: [...patch.tags] } : {}),
    ...(patch.spec !== undefined ? { spec: cloneSpec(patch.spec) } : {}),
    updatedAt: Date.now(),
  };
  entries[idx] = updated;
  writeAll(entries);
  return updated;
}

/** Duplicate an existing entry under a new id with fresh timestamps and a
 * deep-cloned spec. Name defaults to "<orig> (copy)". Returns null if the
 * source entry doesn't exist. */
export function forkEntry(id: string, name?: string): LibraryEntry | null {
  const source = getEntry(id);
  if (!source) return null;
  const now = Date.now();
  const forked: LibraryEntry = {
    id: makeId(),
    name: name ?? `${source.name} (copy)`,
    tags: [...source.tags],
    spec: cloneSpec(source.spec),
    createdAt: now,
    updatedAt: now,
  };
  const entries = readAll();
  entries.push(forked);
  writeAll(entries);
  return forked;
}

/** Remove an entry by id. A no-op (not an error) if it doesn't exist. */
export function removeEntry(id: string): void {
  const entries = readAll();
  const next = entries.filter((e) => e.id !== id);
  if (next.length !== entries.length) {
    writeAll(next);
  }
}

/** Pure search over an already-loaded entry list: case-insensitive substring
 * match on name OR any tag for `query`, AND the entry must contain every tag
 * in `tags` (case-sensitive tag match, since tags are expected to already be
 * normalized by the caller/allTags). An empty query matches everything;
 * an empty `tags` filter requires nothing. */
export function searchEntries(entries: LibraryEntry[], query: string, tags: string[]): LibraryEntry[] {
  const q = query.trim().toLowerCase();
  return entries.filter((e) => {
    if (q) {
      const nameMatch = e.name.toLowerCase().includes(q);
      const tagMatch = e.tags.some((t) => t.toLowerCase().includes(q));
      if (!nameMatch && !tagMatch) return false;
    }
    if (tags.length > 0) {
      if (!tags.every((t) => e.tags.includes(t))) return false;
    }
    return true;
  });
}

/** Pure: every distinct tag across the given entries, deduped and sorted
 * ascending. */
export function allTags(entries: LibraryEntry[]): string[] {
  const set = new Set<string>();
  for (const e of entries) {
    for (const t of e.tags) set.add(t);
  }
  return [...set].sort((a, b) => a.localeCompare(b));
}
