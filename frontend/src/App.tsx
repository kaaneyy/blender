/**
 * App shell (T4.1): prompt/chat panel left, 3D viewport center,
 * schema-driven controls right. The viewport and controls are built out in
 * milestone 2; this scaffold pins the layout so those land as drop-ins.
 */
export default function App() {
  return (
    <div style={{ display: "flex", height: "100vh", fontFamily: "system-ui" }}>
      <aside style={{ width: 300, borderRight: "1px solid #ddd", padding: 16 }}>
        <h2>AssetForge</h2>
        <p>Prompt &amp; refine panel (T4.1) — milestone 2.</p>
      </aside>
      <main style={{ flex: 1, display: "grid", placeItems: "center" }}>
        <p>3D viewport (react-three-fiber, T4.3/T4.4) — milestone 2.</p>
      </main>
      <aside style={{ width: 300, borderLeft: "1px solid #ddd", padding: 16 }}>
        <p>Schema-driven controls (T4.2) — milestone 2.</p>
      </aside>
    </div>
  );
}
