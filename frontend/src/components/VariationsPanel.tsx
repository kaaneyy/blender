/** "✨ Give me 4 variants": a modal grid of AI-proposed alternate takes on
 * the current asset, each a real live 3D thumbnail (built with the SAME
 * computePrimitives + AssetMesh path the main viewport uses — never a
 * default-Euler-order stand-in, see AssetMesh's blenderEuler doc), so what
 * you pick is exactly what you get. Picking a card routes through the app's
 * one validated spec-swap path (adoptSpec, passed in as `onPick`) — never a
 * direct setSpec — so a variant that fails to build can never blank the
 * viewport; the failure surfaces inline and the grid stays open. */
import { useEffect, useMemo, useState } from "react";
import { Canvas, useThree } from "@react-three/fiber";
import type { AssetSpec, Primitive } from "../types";
import { aabb, computePrimitives } from "../builders";
import AssetMesh from "./AssetMesh";
import type { Variant } from "../api";

/** Bounding center + radius of a primitive list in its own authored (Z-up)
 * coordinate frame — used only to frame the static thumbnail camera, not a
 * geometry computation, so it stays local to this preview component rather
 * than living in builders/. */
function boundsOf(primitives: Primitive[]): { center: [number, number, number]; radius: number } {
  const lo = [Infinity, Infinity, Infinity];
  const hi = [-Infinity, -Infinity, -Infinity];
  for (const p of primitives) {
    if (p.cut) continue;
    const box = aabb(p);
    for (let k = 0; k < 3; k++) {
      lo[k] = Math.min(lo[k], box.center[k] - box.half[k]);
      hi[k] = Math.max(hi[k], box.center[k] + box.half[k]);
    }
  }
  if (!Number.isFinite(lo[0])) return { center: [0, 0, 0], radius: 1 };
  const center: [number, number, number] = [
    (lo[0] + hi[0]) / 2,
    (lo[1] + hi[1]) / 2,
    (lo[2] + hi[2]) / 2,
  ];
  const radius = Math.max(0.25, Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) / 2);
  return { center, radius };
}

/** Positions the (otherwise static) camera once per framing and stops —
 * frameloop="demand" below means nothing re-renders after that until the
 * component unmounts, keeping 4 simultaneous canvases cheap. */
function StaticCamera({ position }: { position: readonly [number, number, number] }) {
  const camera = useThree((s) => s.camera);
  const invalidate = useThree((s) => s.invalidate);
  useEffect(() => {
    camera.position.set(position[0], position[1], position[2]);
    camera.lookAt(0, 0, 0);
    if ("updateProjectionMatrix" in camera) {
      (camera as unknown as { updateProjectionMatrix(): void }).updateProjectionMatrix();
    }
    invalidate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera, invalidate, position[0], position[1], position[2]]);
  return null;
}

/** One variant's live thumbnail: its own primitives computed synchronously
 * (computePrimitives is pure/sync, same as the main viewport's useMemo) and
 * rendered through AssetMesh — no orbit, no gizmos, low dpr, a fixed diagonal
 * camera framed to the variant's own bounds. */
function VariantThumbnail({
  variant,
  onPick,
  picking,
}: {
  variant: Variant;
  onPick: () => void;
  picking: boolean;
}) {
  const primitives = useMemo(() => {
    try {
      return computePrimitives(variant.spec);
    } catch {
      return null;
    }
  }, [variant.spec]);

  const framing = useMemo(() => (primitives ? boundsOf(primitives) : null), [primitives]);
  const dist = framing ? Math.max(framing.radius * 2.4, 1.2) : 1.2;
  const camPos: [number, number, number] = [dist * 0.85, dist * 0.7, dist];

  return (
    <div className="variant-card">
      <div className="variant-card__viewport">
        {primitives && framing ? (
          <Canvas
            dpr={1}
            frameloop="demand"
            gl={{ antialias: false, preserveDrawingBuffer: false }}
            camera={{ fov: 40 }}
          >
            <color attach="background" args={["#eef1f5"]} />
            <ambientLight intensity={0.75} />
            <directionalLight position={[4, 6, 4]} intensity={0.9} />
            <StaticCamera position={camPos} />
            <group rotation={[-Math.PI / 2, 0, 0]}>
              <group position={[-framing.center[0], -framing.center[1], -framing.center[2]]}>
                <AssetMesh
                  primitives={primitives}
                  spec={variant.spec}
                  selected={null}
                  onSelect={() => {}}
                />
              </group>
            </group>
          </Canvas>
        ) : (
          <div className="variant-card__error">Could not render this variant.</div>
        )}
      </div>
      <div className="variant-card__label" title={variant.label}>
        {variant.label}
      </div>
      <button
        className="variant-card__pick"
        onClick={onPick}
        disabled={!primitives || picking}
      >
        Use this variant
      </button>
    </div>
  );
}

export default function VariationsPanel({
  busy,
  error,
  variants,
  onPick,
  onClose,
  onRetry,
}: {
  busy: boolean;
  /** Fetch failure (empty/absent/unreachable), or null once variants loaded. */
  error: string | null;
  /** Loaded variants, or null while busy / before any request completed. */
  variants: Variant[] | null;
  /** Adopt this spec — the SAME validated path (App's adoptSpec) every other
   * spec swap uses. Returns an error string on failure, null on success. */
  onPick: (spec: AssetSpec) => string | null;
  onClose: () => void;
  onRetry: () => void;
}) {
  const [pickError, setPickError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);

  const handlePick = (spec: AssetSpec) => {
    if (picking) return;
    setPicking(true);
    const err = onPick(spec);
    if (err) {
      setPickError(err);
      setPicking(false);
    } else {
      setPickError(null);
      onClose();
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal variations-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Pick a variant"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h3>✨ Pick a variant</h3>
          <button className="close" onClick={onClose} title="Close">
            ✕
          </button>
        </div>
        <div className="modal__body">
          {busy && (
            <div className="stream-card" aria-live="off">
              <div className="stream-card__title">
                <span className="stream-card__dot" /> Requesting 4 variants…
              </div>
            </div>
          )}
          {!busy && error && (
            <>
              <div className="violation" role="alert">
                <p>{error}</p>
              </div>
              <button onClick={onRetry}>Try again</button>
            </>
          )}
          {!busy && pickError && (
            <div className="violation" role="alert">
              <p>{pickError}</p>
            </div>
          )}
          {!busy && !error && variants && variants.length === 0 && (
            <p className="hint">No variants came back — try again.</p>
          )}
          {!busy && !error && variants && variants.length > 0 && (
            <div className="variants-grid">
              {variants.map((v, i) => (
                <VariantThumbnail
                  key={i}
                  variant={v}
                  picking={picking}
                  onPick={() => handlePick(v.spec)}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
