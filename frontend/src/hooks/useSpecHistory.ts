/** Undo/redo wrapper around a single piece of state (the AssetSpec), used
 * by App.tsx so every spec-mutating action — slider tweaks, gizmo edits, AI
 * refine/improve/generate, audit-fix apply, component delete/duplicate,
 * reset — is reversible with Ctrl+Z / Ctrl+Shift+Z.
 *
 * The returned setSpec has the EXACT signature of React's useState setter
 * (a value OR an updater function taking the previous state), so every
 * existing `setSpec((s) => ...)` call site in App.tsx keeps working
 * unmodified. */
import { useCallback, useRef, useState } from "react";

/** How long a burst of rapid edits (slider drags, gizmo drags, repeated
 * number-input keystrokes) coalesces into a single undo step. Any setSpec
 * call arriving within this many ms of the previous one merges into the
 * entry already open for the current burst — that entry's undo point stays
 * the state from BEFORE the whole burst started, not the last intermediate
 * value. A gap larger than this (or the very first edit, or any edit right
 * after an undo/redo) always starts a new, separate history entry. */
export const COALESCE_WINDOW_MS = 500;

/** Maximum number of undo steps kept; the oldest entry is dropped once a
 * push would exceed this. */
export const MAX_HISTORY = 50;

type Updater<T> = T | ((prev: T) => T);

interface HistoryState<T> {
  past: T[];
  present: T;
  future: T[];
}

export interface SpecHistory<T> {
  spec: T;
  setSpec: (update: Updater<T>) => void;
  /** Force the NEXT setSpec call to open a fresh history entry no matter how
   * recently the previous one landed. Callers that swap in a whole new spec
   * from outside the normal edit flow (loading a file, adopting an AI
   * result) call this first so the swap can never silently coalesce into
   * whatever burst of slider/gizmo edits happened to precede it. */
  commitBoundary: () => void;
  undo: () => void;
  redo: () => void;
  canUndo: boolean;
  canRedo: boolean;
}

export function useSpecHistory<T>(init: () => T): SpecHistory<T> {
  const [state, setState] = useState<HistoryState<T>>(() => ({
    past: [],
    present: init(),
    future: [],
  }));
  // 0 guarantees the very first edit ever, and the edit right after an
  // undo/redo, can never coalesce with whatever came before it.
  const lastEditAt = useRef(0);

  const setSpec = useCallback((update: Updater<T>) => {
    setState((s) => {
      const nextPresent =
        typeof update === "function" ? (update as (prev: T) => T)(s.present) : update;
      const now = Date.now();
      const withinBurst = now - lastEditAt.current < COALESCE_WINDOW_MS;
      lastEditAt.current = now;
      if (withinBurst && s.past.length > 0) {
        // Still inside the current burst: the anchor entry (past) is
        // already open, just move the present forward without pushing.
        return { past: s.past, present: nextPresent, future: [] };
      }
      // Discrete action (or burst start): archive the pre-edit state as
      // the new undo point. structuredClone so nothing downstream can
      // mutate a live object out from under an archived snapshot.
      const past = [...s.past, structuredClone(s.present)].slice(-MAX_HISTORY);
      return { past, present: nextPresent, future: [] };
    });
  }, []);

  const commitBoundary = useCallback(() => {
    lastEditAt.current = 0;
  }, []);

  const undo = useCallback(() => {
    setState((s) => {
      if (s.past.length === 0) return s;
      const previous = s.past[s.past.length - 1];
      const past = s.past.slice(0, -1);
      const future = [structuredClone(s.present), ...s.future].slice(0, MAX_HISTORY);
      lastEditAt.current = 0; // next edit must not coalesce across the undo
      return { past, present: previous, future };
    });
  }, []);

  const redo = useCallback(() => {
    setState((s) => {
      if (s.future.length === 0) return s;
      const [next, ...rest] = s.future;
      const past = [...s.past, structuredClone(s.present)].slice(-MAX_HISTORY);
      lastEditAt.current = 0; // next edit must not coalesce across the redo
      return { past, present: next, future: rest };
    });
  }, []);

  return {
    spec: state.present,
    setSpec,
    commitBoundary,
    undo,
    redo,
    canUndo: state.past.length > 0,
    canRedo: state.future.length > 0,
  };
}
