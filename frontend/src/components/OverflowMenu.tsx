/** A small "burger"/overflow menu: a trigger button that toggles a popover
 * list of actions. Used to collapse secondary toolbar buttons so panels and
 * the viewport nav stay uncluttered. Closes on outside-click and Escape.
 * No dependencies — a plain positioned popover. */
import { useEffect, useRef, useState, type ReactNode } from "react";

export interface MenuItem {
  /** Rendered label (may include an emoji/icon and dynamic text). */
  label: ReactNode;
  onClick: () => void;
  title?: string;
  disabled?: boolean;
  /** Highlight as the active/on state (for toggles moved into the menu). */
  active?: boolean;
}

export default function OverflowMenu({
  trigger,
  title = "More",
  items,
  className = "",
  placement = "below",
}: {
  /** Trigger button contents (an icon/emoji or short label). */
  trigger: ReactNode;
  title?: string;
  items: MenuItem[];
  /** Extra class on the trigger button (e.g. "btn btn--secondary" or "nav-btn"). */
  className?: string;
  /** Where the popover opens relative to the trigger. */
  placement?: "below" | "left" | "above";
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="overflow-menu" ref={ref}>
      <button
        type="button"
        className={`overflow-menu__trigger ${className}`}
        title={title}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        {trigger}
      </button>
      {open && (
        <div className={`overflow-menu__list overflow-menu__list--${placement}`} role="menu">
          {items.map((it, i) => (
            <button
              key={i}
              type="button"
              role="menuitem"
              className={`overflow-menu__item${it.active ? " overflow-menu__item--active" : ""}`}
              title={it.title}
              disabled={it.disabled}
              onClick={() => {
                it.onClick();
                setOpen(false);
              }}
            >
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
