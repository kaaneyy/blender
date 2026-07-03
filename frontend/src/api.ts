/** Client for the AssetForge API. On Vercel this hits the bundled Python
 * function under /api; in dev the Vite proxy forwards /api to localhost:8000.
 * Override with VITE_API_URL when the backend lives elsewhere. */
import type { AssetSpec } from "./types";

const API_BASE: string =
  (import.meta as { env?: Record<string, string> }).env?.VITE_API_URL ?? "/api";

async function post(path: string, body: unknown): Promise<AssetSpec> {
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
  const data = await resp.json();
  if (!data?.spec) throw new Error("Backend returned no spec");
  return data.spec as AssetSpec;
}

export function generateSpec(prompt: string): Promise<AssetSpec> {
  return post("/generate-spec", { prompt, code_mode: "strict" });
}

export function refineSpec(spec: AssetSpec, message: string): Promise<AssetSpec> {
  return post("/refine-spec", { spec, message, code_mode: spec.code_mode ?? "strict" });
}
