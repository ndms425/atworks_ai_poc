// Select part of an assistant reply and a small "인용해서 묻기" button floats over the selection;
// clicking it hands the selected text to `onQuote`. Follows open-design's QuoteBar (Apache-2.0,
// apps/web/src/components/chat/QuoteBar.tsx): above the selection, centred, flipped below when
// the panel's top edge would cut it off. `position: fixed` so the transcript's own scrolling and
// overflow never clip it.

"use client";

import { type RefObject, useEffect, useState } from "react";
import { Icon } from "./icons";

const MAX_QUOTE = 600;

interface Anchor {
  text: string;
  x: number;
  y: number;
  below: boolean;
}

export function QuoteBar({ containerRef, onQuote }: { containerRef: RefObject<HTMLElement | null>; onQuote: (text: string) => void }) {
  const [anchor, setAnchor] = useState<Anchor | null>(null);

  useEffect(() => {
    const read = () => {
      const selection = window.getSelection();
      const container = containerRef.current;
      if (!selection || selection.isCollapsed || !container || selection.rangeCount === 0) return setAnchor(null);
      const range = selection.getRangeAt(0);
      const start = range.startContainer instanceof Element ? range.startContainer : range.startContainer.parentElement;
      const end = range.endContainer instanceof Element ? range.endContainer : range.endContainer.parentElement;
      // Only the assistant's own words: both ends inside one reply, never a user bubble or a control.
      const turn = start?.closest("[data-turn]");
      if (!turn || !container.contains(turn) || end?.closest("[data-turn]") !== turn) return setAnchor(null);
      const text = selection.toString().replace(/\s+\n/g, "\n").trim();
      if (!text) return setAnchor(null);
      const box = range.getBoundingClientRect();
      const top = container.getBoundingClientRect().top;
      const below = box.top - 44 < top;
      setAnchor({ text: text.slice(0, MAX_QUOTE), x: box.left + box.width / 2, y: below ? box.bottom + 8 : box.top - 8, below });
    };
    const onUp = () => window.setTimeout(read, 0);
    const onScroll = () => setAnchor(null);
    document.addEventListener("mouseup", onUp);
    document.addEventListener("keyup", onUp);
    const container = containerRef.current;
    container?.addEventListener("scroll", onScroll);
    return () => {
      document.removeEventListener("mouseup", onUp);
      document.removeEventListener("keyup", onUp);
      container?.removeEventListener("scroll", onScroll);
    };
  }, [containerRef]);

  if (!anchor) return null;
  return (
    <button
      type="button"
      // mousedown, not click: the selection must not collapse before the text is read.
      onMouseDown={(event) => {
        event.preventDefault();
        onQuote(anchor.text);
        window.getSelection()?.removeAllRanges();
        setAnchor(null);
      }}
      style={{ left: anchor.x, top: anchor.y }}
      className={`fixed z-50 flex -translate-x-1/2 items-center gap-1.5 rounded-full bg-(--ink) px-3 py-1.5 text-[12.5px] font-semibold text-(--surface) shadow-(--shadow-lg) ${
        anchor.below ? "" : "-translate-y-full"
      }`}
    >
      <Icon name="message" size={13} />
      인용해서 묻기
    </button>
  );
}
