/** Client for the AssetForge API. On Vercel this hits the bundled Python
 * function under /api; in dev the Vite proxy forwards /api to localhost:8000.
 * Override with VITE_API_URL when the backend lives elsewhere. */
import type { AssetSpec } from "./types";

const API_BASE: string =
  (import.meta as { env?: Record<string, string> }).env?.VITE_API_URL ?? "/api";

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

export async function generateSpecStream(
  prompt: string,
  onChunk: (text: string) => void,
): Promise<AssetSpec> {
  const result = await streamPost("/generate-spec-stream", { prompt, code_mode: "strict" }, onChunk);
  if (!result?.spec) throw new Error("Backend returned no spec");
  return result.spec as AssetSpec;
}

export async function refineSpecStream(
  spec: AssetSpec,
  message: string,
  onChunk: (text: string) => void,
): Promise<AssetSpec> {
  const result = await streamPost(
    "/refine-spec-stream",
    { spec, message, code_mode: spec.code_mode ?? "strict" },
    onChunk,
  );
  if (!result?.spec) throw new Error("Backend returned no spec");
  return result.spec as AssetSpec;
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
