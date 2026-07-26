/** Share the current design as a URL: the whole spec is base64url-encoded
 * into the location hash (#s=…), so a shared link carries the asset itself —
 * no server, no storage, and the hash never leaves the browser. On load the
 * decoded spec round-trips through App's validated `adoptSpec`. */
import type { AssetSpec } from "./types";

const HASH_KEY = "s";

/** UTF-8-safe base64url (no `+ / =`). */
function toBase64Url(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(b64url: string): string {
  const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}

/** An absolute, shareable URL that encodes `spec` in its hash fragment. */
export function encodeSpecToUrl(spec: AssetSpec): string {
  const encoded = toBase64Url(JSON.stringify(spec));
  const { origin, pathname } = window.location;
  return `${origin}${pathname}#${HASH_KEY}=${encoded}`;
}

/** Parse a shared spec out of the current location hash, or `null` if there
 * isn't one / it's malformed. Never throws. */
export function readSharedSpec(): AssetSpec | null {
  try {
    const hash = window.location.hash.replace(/^#/, "");
    const encoded = new URLSearchParams(hash).get(HASH_KEY);
    if (!encoded) return null;
    const spec = JSON.parse(fromBase64Url(encoded)) as AssetSpec;
    return spec && typeof spec === "object" ? spec : null;
  } catch {
    return null;
  }
}

/** Drop the shared-spec hash from the URL without reloading, so a later
 * refresh falls back to the autosave instead of re-loading the link. */
export function clearShareHash(): void {
  try {
    if (!new URLSearchParams(window.location.hash.replace(/^#/, "")).has(HASH_KEY)) return;
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  } catch {
    /* history unavailable — harmless */
  }
}
