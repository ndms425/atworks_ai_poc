// A thin rail on the transcript's right edge with one marker per question the operator asked,
// placed where that question sits in the conversation. Hover shows the question, a click scrolls
// to it, and the marker for the part on screen is highlighted. Follows open-design's message rail
// (Apache-2.0, apps/web/src/components/ChatPane.tsx `chat-message-rail`).

"use client";

import { type RefObject, useCallback, useEffect, useState } from "react";

interface Marker {
  top: number;
  ratio: number;
  text: string;
}

const MIN_QUESTIONS = 3;

export function MessageRail({ containerRef, version }: { containerRef: RefObject<HTMLElement | null>; version: number }) {
  const [markers, setMarkers] = useState<Marker[]>([]);
  const [active, setActive] = useState(-1);
  const [hover, setHover] = useState<number | null>(null);

  const measure = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;
    const box = container.getBoundingClientRect();
    const height = container.scrollHeight;
    if (height <= container.clientHeight + 40) return setMarkers([]);
    const found = [...container.querySelectorAll<HTMLElement>("[data-user-msg]")].map((el) => {
      const top = el.getBoundingClientRect().top - box.top + container.scrollTop;
      return { top, ratio: Math.min(0.98, Math.max(0.01, top / height)), text: el.innerText.trim() };
    });
    setMarkers(found.length >= MIN_QUESTIONS ? found : []);
    const at = container.scrollTop + 48;
    setActive(found.reduce((current, m, index) => (m.top <= at ? index : current), -1));
  }, [containerRef]);

  useEffect(() => {
    measure();
    const container = containerRef.current;
    if (!container) return;
    container.addEventListener("scroll", measure, { passive: true });
    window.addEventListener("resize", measure);
    return () => {
      container.removeEventListener("scroll", measure);
      window.removeEventListener("resize", measure);
    };
  }, [containerRef, measure, version]);

  if (!markers.length) return null;
  return (
    <nav aria-label="내 질문으로 이동" className="pointer-events-none absolute inset-y-3 right-1 z-10 w-3">
      {markers.map((marker, index) => (
        <button
          key={index}
          type="button"
          aria-label={`질문 ${index + 1}: ${marker.text.slice(0, 60)}`}
          onClick={() => containerRef.current?.scrollTo({ top: Math.max(0, marker.top - 8), behavior: "smooth" })}
          onMouseEnter={() => setHover(index)}
          onMouseLeave={() => setHover(null)}
          onFocus={() => setHover(index)}
          onBlur={() => setHover(null)}
          style={{ top: `${marker.ratio * 100}%` }}
          className="pointer-events-auto absolute right-0 flex h-3 w-3 -translate-y-1/2 items-center justify-end"
        >
          <span
            className={`block h-[3px] rounded-full transition-all ${
              index === active ? "w-3 bg-(--accent)" : "w-2 bg-(--line-strong) hover:w-3 hover:bg-(--ink-soft)"
            }`}
          />
          {hover === index ? (
            <span className="absolute right-4 top-1/2 w-max max-w-[240px] -translate-y-1/2 truncate rounded-lg bg-(--ink) px-2 py-1 text-[11.5px] text-(--surface) shadow-(--shadow)">
              {index + 1}. {marker.text}
            </span>
          ) : null}
        </button>
      ))}
    </nav>
  );
}
