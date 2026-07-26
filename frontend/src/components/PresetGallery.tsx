/** A visual gallery of the bundled curated starter assets — a richer twin of
 * the "Examples" dropdown. Picking a card loads that preset through the exact
 * same validated path (`loadExample` → App's `adoptSpec`) and closes. */
import Modal from "./Modal";
import { EXAMPLE_ASSETS } from "../examples";

export default function PresetGallery({
  onClose,
  onSelect,
}: {
  onClose: () => void;
  /** Load the preset with this id — the same handler the dropdown uses. */
  onSelect: (id: string) => void;
}) {
  return (
    <Modal title="Preset gallery — start from a curated asset" onClose={onClose}>
      <p className="preset-intro">
        Click a starter to load it, then refine it with the sliders or the AI.
      </p>
      <div className="preset-grid">
        {EXAMPLE_ASSETS.map((e) => (
          <button
            key={e.id}
            className="preset-card"
            onClick={() => {
              onSelect(e.id);
              onClose();
            }}
          >
            <span className="preset-card__emoji" aria-hidden>
              {e.emoji}
            </span>
            <span className="preset-card__label">{e.label}</span>
            <span className="preset-card__desc">{e.desc}</span>
            <span className="preset-card__type">{e.spec.asset_type}</span>
          </button>
        ))}
      </div>
    </Modal>
  );
}
