/** Client for the AssetForge API. On Vercel this hits the bundled Python
 * function under /api; in dev the Vite proxy forwards /api to localhost:8000.
 * Override with VITE_API_URL when the backend lives elsewhere. */
import type { AssetSpec } from "./types";
import type { AuditReport } from "./builders/audit";

const API_BASE: string =
  (import.meta as { env?: Record<string, string> }).env?.VITE_API_URL ?? "/api";

/** DeepSeek models the model dropdown offers. Empty string = server default. */
export type DeepseekModel = "deepseek-chat" | "deepseek-v4-flash" | "deepseek-v4-pro";

export const MODEL_OPTIONS: Array<{ id: DeepseekModel; label: string; hint: string }> = [
  { id: "deepseek-chat", label: "DeepSeek Chat", hint: "balanced · cheapest" },
  { id: "deepseek-v4-flash", label: "DeepSeek V4 Flash", hint: "faster, lighter" },
  { id: "deepseek-v4-pro", label: "DeepSeek V4 Pro", hint: "most capable" },
];

async function post(path: string, body: unknown): Promise<Record<string, unknown>> {
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error(
      "Could not reach the AI backend. If you deployed to Vercel, make sure " +
        "the last deployment succeeded; for local dev, start it with " +
        "`uvicorn backend.app.main:app` (see README → Turn on the AI).",
    );
  }
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`;
    try {
      const data = await resp.json();
      if (data?.detail) detail = String(data.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return (await resp.json()) as Record<string, unknown>;
}

async function postForSpec(path: string, body: unknown): Promise<AssetSpec> {
  const data = await post(path, body);
  if (!data?.spec) throw new Error("Backend returned no spec");
  return data.spec as AssetSpec;
}

export function generateSpec(prompt: string): Promise<AssetSpec> {
  return postForSpec("/generate-spec", { prompt, code_mode: "strict" });
}

/** Which discipline is asking a clarifying question (or whose take appears
 * in the post-generation design panel) — an older backend omits this. */
export interface Persona {
  id: "architecture" | "mechanical" | "civil" | "design";
  label: string;
  icon: string;
}

const PERSONA_IDS = new Set(["architecture", "mechanical", "civil", "design"]);

/** Tolerant parse of an optional `persona` field: any malformed shape
 * degrades to `undefined` rather than throwing or dropping the question. */
function parsePersona(raw: unknown): Persona | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const p = raw as Record<string, unknown>;
  if (typeof p.id !== "string" || !PERSONA_IDS.has(p.id)) return undefined;
  if (typeof p.label !== "string") return undefined;
  if (typeof p.icon !== "string") return undefined;
  return { id: p.id as Persona["id"], label: p.label, icon: p.icon };
}

/** One clarifying question with AI-written answers for the dropdown. The
 * backend may ask any number of questions — never assume a fixed count. */
export interface ClarifyQuestion {
  id: string;
  question: string;
  options: string[];
  /** Which discipline is asking, when the backend provides one. */
  persona?: Persona;
}

/** An answered clarifying question, folded into the generation request. */
export interface Clarification {
  question: string;
  answer: string;
}

/** A handful of clarifying questions (any count >= 1) x 3 offered answers
 * each for a raw request — shown as dropdowns (plus a type-your-own blank)
 * before generating, so a basic request surfaces the real one behind it. */
export async function clarifyRequest(
  prompt: string,
  model: DeepseekModel | "" = "",
): Promise<ClarifyQuestion[]> {
  const data = await post("/clarify-request", { prompt, model });
  if (!Array.isArray(data?.questions)) throw new Error("Backend returned no questions");
  return (data.questions as Array<Record<string, unknown>>).map((q) => {
    const question: ClarifyQuestion = {
      id: q.id as string,
      question: q.question as string,
      options: q.options as string[],
    };
    const persona = parsePersona(q.persona);
    if (persona) question.persona = persona;
    return question;
  });
}

export function refineSpec(spec: AssetSpec, message: string): Promise<AssetSpec> {
  return postForSpec("/refine-spec", { spec, message, code_mode: spec.code_mode ?? "strict" });
}

export async function installGuide(spec: AssetSpec): Promise<string> {
  const data = await post("/install-guide", { spec });
  if (typeof data?.guide !== "string") throw new Error("Backend returned no guide");
  return data.guide;
}

export interface StandardsUpdateResult {
  proposal: Record<string, unknown>;
  changes: string[];
  note: string;
  committed: boolean;
  detail: string;
  url: string | null;
}

export async function updateStandards(): Promise<StandardsUpdateResult> {
  return (await post("/update-standards", {})) as unknown as StandardsUpdateResult;
}

/** Server-side buildability findings (floating parts, below-grade geometry,
 * declared connections with no contact) for the current spec. Best-effort:
 * returns [] when the backend is unreachable so callers can proceed. */
export async function buildabilityFindings(spec: AssetSpec): Promise<string[]> {
  try {
    const data = await post("/validate-spec", spec);
    const violations = (data?.violations ?? []) as Array<Record<string, unknown>>;
    return violations
      .filter((v) => v.parameter_id === "__buildability__")
      .map((v) => String(v.message));
  } catch {
    return [];
  }
}

/** One AI-proposed alternate take on the current asset, from
 * /variations-spec — a full spec plus a short label describing what's
 * different, mirroring the same `changes` diff envelope the edit endpoints
 * return and any code violations the variant carries. */
export interface Variant {
  spec: AssetSpec;
  label: string;
  changes?: SpecChanges;
  violations: unknown[];
}

/** Tolerant parse of one entry in the `variants` array: an entry missing a
 * usable `spec` is unrecoverable and dropped entirely; every other field
 * degrades to a safe default rather than invalidating the whole entry, same
 * spirit as the other tolerant parsers in this file. `index` only backstops
 * the display label when the backend omits one. */
function parseVariant(raw: unknown, index: number): Variant | null {
  if (!raw || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  if (!v.spec || typeof v.spec !== "object") return null;
  const label = typeof v.label === "string" && v.label.trim() ? v.label : `Variant ${index + 1}`;
  const violations = Array.isArray(v.violations) ? v.violations : [];
  return {
    spec: v.spec as AssetSpec,
    label,
    changes: parseChanges(v.changes),
    violations,
  };
}

/** Ask the AI for `count` alternate takes on the current spec — a
 * non-streaming call (the response is a batch of full specs, not prose to
 * render live). Malformed entries (no usable `spec`) are dropped rather than
 * failing the whole batch; only an absent or fully-empty (after filtering)
 * `variants` list throws. */
export async function variationsSpec(
  spec: AssetSpec,
  count = 4,
  model: DeepseekModel | "" = "",
): Promise<Variant[]> {
  const data = await post("/variations-spec", {
    spec,
    count,
    code_mode: spec.code_mode ?? "strict",
    model,
  });
  if (!Array.isArray(data?.variants)) throw new Error("Backend returned no variants");
  const out: Variant[] = [];
  (data.variants as unknown[]).forEach((item, i) => {
    const v = parseVariant(item, i);
    if (v) out.push(v);
  });
  if (!out.length) throw new Error("Backend returned no usable variants");
  return out;
}

/* ------------------------------------------------------------------------
 * Streaming variants: the backend streams the raw LLM text, then a sentinel
 * followed by a JSON payload {ok, result|error}. onChunk receives the
 * accumulated visible text so the UI can show generation live.
 * ---------------------------------------------------------------------- */

const SENTINEL = "<<<ASSETFORGE_RESULT>>>";

async function streamPost(
  path: string,
  body: unknown,
  onChunk: (text: string) => void,
): Promise<Record<string, unknown>> {
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error(
      "Could not reach the AI backend. If you deployed to Vercel, make sure " +
        "the last deployment succeeded; for local dev, start it with " +
        "`uvicorn backend.app.main:app` (see README → Turn on the AI).",
    );
  }
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`;
    try {
      const data = await resp.json();
      if (data?.detail) detail = String(data.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }

  let buf = "";
  if (resp.body) {
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const idx = buf.indexOf(SENTINEL);
      // hold back a potential partially received sentinel at the tail
      onChunk(idx === -1 ? buf.slice(0, Math.max(0, buf.length - SENTINEL.length)) : buf.slice(0, idx));
    }
    buf += dec.decode();
  } else {
    buf = await resp.text(); // environments without ReadableStream support
  }

  const idx = buf.indexOf(SENTINEL);
  if (idx === -1) throw new Error("The stream ended without a result — try again.");
  onChunk(buf.slice(0, idx));
  const payload = JSON.parse(buf.slice(idx + SENTINEL.length));
  if (!payload.ok) throw new Error(String(payload.error ?? "Generation failed"));
  return payload.result as Record<string, unknown>;
}

/** One persona's 1-2 sentence take on what the user deep-down asked for,
 * part of the post-generation design panel. */
export interface PanelEntry {
  id: "architecture" | "mechanical" | "civil" | "design";
  label: string;
  icon: string;
  take: string;
}

/** Tolerant parse of the optional `panel` envelope key: an older backend
 * omits it entirely, and any malformed shape must degrade to `undefined`
 * rather than throw — generation still succeeds either way. */
function parsePanel(raw: unknown): PanelEntry[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const out: PanelEntry[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") return undefined;
    const p = item as Record<string, unknown>;
    if (typeof p.id !== "string" || !PERSONA_IDS.has(p.id)) return undefined;
    if (typeof p.label !== "string") return undefined;
    if (typeof p.icon !== "string") return undefined;
    if (typeof p.take !== "string") return undefined;
    out.push({ id: p.id as PanelEntry["id"], label: p.label, icon: p.icon, take: p.take });
  }
  return out;
}

/** What an AI edit (refine/focus/wizard/improve) actually touched, from a
 * server-side diff of the spec before/after. Absent on an older backend or
 * when the diff itself failed — callers must treat it as fully optional and
 * degrade to today's plain "Updated ..." messaging when it's missing. */
export interface SpecChanges {
  added: string[];
  removed: string[];
  changed: string[];
  params_changed: string[];
  summary: string;
}

function isStringArray(v: unknown): v is string[] {
  return Array.isArray(v) && v.every((x) => typeof x === "string");
}

/** Tolerant parse of the optional `changes` envelope key: any malformed
 * shape degrades to `undefined` rather than throw — the edit already
 * succeeded either way, this only gates the "what changed" chat line. */
function parseChanges(raw: unknown): SpecChanges | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const c = raw as Record<string, unknown>;
  if (!isStringArray(c.added)) return undefined;
  if (!isStringArray(c.removed)) return undefined;
  if (!isStringArray(c.changed)) return undefined;
  if (!isStringArray(c.params_changed)) return undefined;
  if (typeof c.summary !== "string") return undefined;
  return {
    added: c.added,
    removed: c.removed,
    changed: c.changed,
    params_changed: c.params_changed,
    summary: c.summary,
  };
}

/** Compact one-line rendering of a `changes` envelope: the backend's own
 * summary when it wrote one, else composed from the added/changed/removed
 * part names. Returns null when there's nothing worth showing (no changes
 * object, or an empty diff) so callers can fall back to today's output. */
export function summarizeChanges(changes: SpecChanges | undefined): string | null {
  if (!changes) return null;
  const summary = changes.summary.trim();
  if (summary) return summary;
  const parts: string[] = [];
  if (changes.changed.length) parts.push(`Changed: ${changes.changed.join(", ")}`);
  if (changes.added.length) parts.push(`Added: ${changes.added.join(", ")}`);
  if (changes.removed.length) parts.push(`Removed: ${changes.removed.join(", ")}`);
  return parts.length ? parts.join(" · ") : null;
}

export async function generateSpecStream(
  prompt: string,
  onChunk: (text: string) => void,
  model: DeepseekModel | "" = "",
  clarifications: Clarification[] = [],
): Promise<{ spec: AssetSpec; brief?: string; panel?: PanelEntry[] }> {
  const result = await streamPost(
    "/generate-spec-stream",
    { prompt, code_mode: "strict", model, clarifications },
    onChunk,
  );
  if (!result?.spec) throw new Error("Backend returned no spec");
  return {
    spec: result.spec as AssetSpec,
    brief: typeof result.brief === "string" ? result.brief : undefined,
    panel: parsePanel(result.panel),
  };
}

export async function refineSpecStream(
  spec: AssetSpec,
  message: string,
  onChunk: (text: string) => void,
  model: DeepseekModel | "" = "",
): Promise<{ spec: AssetSpec; changes?: SpecChanges }> {
  const result = await streamPost(
    "/refine-spec-stream",
    { spec, message, code_mode: spec.code_mode ?? "strict", model },
    onChunk,
  );
  if (!result?.spec) throw new Error("Backend returned no spec");
  return { spec: result.spec as AssetSpec, changes: parseChanges(result.changes) };
}

/** Deep-detail one named area of the current spec, keeping the rest intact. */
export async function focusSpecStream(
  spec: AssetSpec,
  area: string,
  onChunk: (text: string) => void,
  model: DeepseekModel | "" = "",
): Promise<AssetSpec> {
  const result = await streamPost(
    "/focus-spec-stream",
    { spec, area, code_mode: spec.code_mode ?? "strict", model },
    onChunk,
  );
  if (!result?.spec) throw new Error("Backend returned no spec");
  return result.spec as AssetSpec;
}

/** One guided-build step: a scoped refinement (connections | materials |
 * details), optionally steered by a user message (empty runs the default
 * pass). Returns the updated spec + any violations. */
export type WizardStep = "connections" | "materials" | "details";

export async function wizardStepStream(
  spec: AssetSpec,
  step: WizardStep,
  message: string,
  onChunk: (text: string) => void,
  model: DeepseekModel | "" = "",
): Promise<{ spec: AssetSpec; changes?: SpecChanges }> {
  const result = await streamPost(
    "/wizard-step-stream",
    { spec, step, message, code_mode: spec.code_mode ?? "strict", model },
    onChunk,
  );
  if (!result?.spec) throw new Error("Backend returned no spec");
  return { spec: result.spec as AssetSpec, changes: parseChanges(result.changes) };
}

/** AI fabrication review of the spec's connections. The backend answers in
 * the deterministic auditor's findings format (same nudge/declare/undeclare
 * fix ops), sanitized against the real component names and test-built — so
 * the same CheckPanel previews and applies the proposals, and nothing
 * changes without the user's confirmation. */
export async function reviewConnectionsStream(
  spec: AssetSpec,
  onChunk: (text: string) => void,
  model: DeepseekModel | "" = "",
): Promise<AuditReport> {
  const result = await streamPost("/review-connections-stream", { spec, model }, onChunk);
  if (!Array.isArray(result?.findings)) throw new Error("Backend returned no findings");
  return result as unknown as AuditReport;
}

/** One deterministic-check finding surfaced by /improve-spec[-stream] before
 * the AI improves the asset — informational only (no fix ops; the backend
 * already folded the fix into the returned spec). */
export interface Finding {
  severity: string;
  kind: string;
  message: string;
}

/** One finding inside a perspective card — same severity/kind/message shape
 * as `Finding` plus which pass surfaced it: the deterministic Python checks
 * for that discipline, or that persona's own AI read of the asset. */
export interface PerspectiveFinding {
  severity: string;
  kind: string;
  message: string;
  source: "checks" | "ai";
}

/** One peer reaction to a perspective card, from another discipline's
 * evaluator — surfaced under that card's own findings so the panel reads
 * like the consultants reviewed each other's notes. `from` is another
 * perspective's id (never the card's own — enforced by the backend). */
export interface PeerNote {
  from: "architecture" | "mechanical" | "civil" | "design";
  stance: "concur" | "dispute" | "refine";
  note: string;
}

/** One professional-evaluator card returned by /improve-spec[-stream]:
 * a discipline (architecture/mechanical/civil/design) with what its checks
 * and its AI persona found on the pre-improvement asset. `summary` is ""
 * and `error` is set when that persona's AI call failed — the other
 * perspectives are unaffected. `peer_notes` is absent on an older backend
 * or when that card's notes were malformed — the rest of the card still
 * renders as it does today. */
export interface Perspective {
  id: "architecture" | "mechanical" | "civil" | "design";
  label: string;
  icon: string;
  summary: string;
  findings: PerspectiveFinding[];
  error: string | null;
  peer_notes?: PeerNote[];
}

const PERSPECTIVE_IDS = new Set(["architecture", "mechanical", "civil", "design"]);

/** Tolerant parse of one perspective card's optional `peer_notes`: an older
 * backend omits it entirely, and any malformed shape degrades to
 * `undefined` for that card only — the card's own findings still render,
 * it just has no peer reactions shown. Capped at 3 to match the backend's
 * own contract even if a malformed payload sends more. */
function parsePeerNotes(raw: unknown): PeerNote[] | undefined {
  if (raw === undefined) return undefined;
  if (!Array.isArray(raw)) return undefined;
  const out: PeerNote[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") return undefined;
    const n = item as Record<string, unknown>;
    if (typeof n.from !== "string" || !PERSPECTIVE_IDS.has(n.from)) return undefined;
    if (n.stance !== "concur" && n.stance !== "dispute" && n.stance !== "refine") return undefined;
    if (typeof n.note !== "string") return undefined;
    out.push({ from: n.from as PeerNote["from"], stance: n.stance, note: n.note });
  }
  return out.slice(0, 3);
}

/** Tolerant parse of the optional `perspectives` envelope key: an older
 * backend omits it entirely, and any malformed shape must degrade to
 * `undefined` rather than throw — the flat findings list below still
 * renders either way. */
function parsePerspectives(raw: unknown): Perspective[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const out: Perspective[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") return undefined;
    const p = item as Record<string, unknown>;
    if (typeof p.id !== "string" || !PERSPECTIVE_IDS.has(p.id)) return undefined;
    if (typeof p.label !== "string") return undefined;
    if (typeof p.icon !== "string") return undefined;
    if (typeof p.summary !== "string") return undefined;
    if (p.error !== null && typeof p.error !== "string") return undefined;
    if (!Array.isArray(p.findings)) return undefined;
    const findings: PerspectiveFinding[] = [];
    for (const f of p.findings) {
      if (!f || typeof f !== "object") return undefined;
      const rec = f as Record<string, unknown>;
      if (typeof rec.severity !== "string") return undefined;
      if (typeof rec.kind !== "string") return undefined;
      if (typeof rec.message !== "string") return undefined;
      if (rec.source !== "checks" && rec.source !== "ai") return undefined;
      findings.push({
        severity: rec.severity,
        kind: rec.kind,
        message: rec.message,
        source: rec.source,
      });
    }
    out.push({
      id: p.id as Perspective["id"],
      label: p.label,
      icon: p.icon,
      summary: p.summary,
      findings,
      error: (p.error as string | null) ?? null,
      peer_notes: parsePeerNotes(p.peer_notes),
    });
  }
  return out;
}

/** What the panel jointly agreed matters most, over the same pre-improvement
 * asset the four perspective cards reviewed — absent on an older backend or
 * when the payload is malformed (`priorities` must be 1-3 short strings). */
export interface Consensus {
  summary: string;
  priorities: string[];
}

/** Tolerant parse of the optional `consensus` envelope key: any malformed
 * shape degrades to `undefined` rather than throw — the banner is simply
 * omitted and the cards below render unaffected. */
function parseConsensus(raw: unknown): Consensus | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const c = raw as Record<string, unknown>;
  if (typeof c.summary !== "string") return undefined;
  if (!isStringArray(c.priorities)) return undefined;
  if (c.priorities.length < 1 || c.priorities.length > 3) return undefined;
  return { summary: c.summary, priorities: c.priorities };
}

/** AI-improved spec plus the findings the Python checks flagged beforehand.
 * `findings` may be empty when the asset already passed every check.
 * `perspectives` is absent on an older backend that doesn't return it.
 * `consensus` likewise — absent on an older backend or a malformed payload. */
export interface ImproveResult {
  spec: AssetSpec;
  findings: Finding[];
  perspectives?: Perspective[];
  changes?: SpecChanges;
  consensus?: Consensus;
}

/** Runs the app's deterministic checks against the current spec, then asks
 * the AI to improve the asset in one pass. The backend returns the same
 * result envelope as /refine-spec plus `findings` — what the checks found
 * before the AI pass ran (may be empty) — and, optionally, `perspectives`:
 * four professional-evaluator cards over the same pre-improvement asset,
 * each optionally carrying `peer_notes` from the other three, plus an
 * optional panel-wide `consensus`. */
export async function improveSpecStream(
  spec: AssetSpec,
  onChunk: (text: string) => void,
  model: DeepseekModel | "" = "",
): Promise<ImproveResult> {
  const result = await streamPost(
    "/improve-spec-stream",
    { spec, code_mode: spec.code_mode ?? "strict", model },
    onChunk,
  );
  if (!result?.spec) throw new Error("Backend returned no spec");
  const findings = Array.isArray(result.findings) ? (result.findings as Finding[]) : [];
  const perspectives = parsePerspectives(result.perspectives);
  const changes = parseChanges(result.changes);
  const consensus = parseConsensus(result.consensus);
  return { spec: result.spec as AssetSpec, findings, perspectives, changes, consensus };
}

export async function installGuideStream(
  spec: AssetSpec,
  onChunk: (text: string) => void,
): Promise<string> {
  const result = await streamPost("/install-guide-stream", { spec }, onChunk);
  if (typeof result?.guide !== "string") throw new Error("Backend returned no guide");
  return result.guide;
}

export async function updateStandardsStream(
  onChunk: (text: string) => void,
): Promise<StandardsUpdateResult> {
  return (await streamPost("/update-standards-stream", {}, onChunk)) as unknown as StandardsUpdateResult;
}
