// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import {
  type CSSProperties,
  type PointerEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

const DEFAULT_WIDTH = 420;
const MIN_WIDTH = 360;

const clampWidth = (width: number) => Math.max(MIN_WIDTH, Math.min(width, Math.round(Math.min(window.innerWidth * 0.7, 960))));

// Motion: a quick, decelerating "snap" in, a slightly shorter accelerating exit.
const IN = { duration: 280, easing: "cubic-bezier(0.22, 1, 0.36, 1)" };
const OUT = { duration: 200, easing: "cubic-bezier(0.4, 0, 1, 1)" };
const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
// `lg` and up the rail sits in the row and pushes the page; below it, it slides over the page.
const docked = () => window.matchMedia("(min-width: 1024px)").matches;

/** clip-path that shows only `rect` of the viewport. */
const insetOf = (rect: DOMRect) =>
  `inset(${rect.top}px ${window.innerWidth - rect.right}px ${window.innerHeight - rect.bottom}px ${rect.left}px)`;

export interface RailControls {
  fullscreen: boolean;
  onToggleFullscreen: () => void;
  onClose: () => void;
}

/** Closing resets full screen: below `lg` there is no Escape or toggle to leave it with. */
export function AssistantRail({
  open,
  storageKey,
  onClose,
  children,
}: {
  open: boolean;
  storageKey: string;
  onClose: () => void;
  children: (rail: RailControls) => ReactNode;
}) {
  const [width, setWidth] = useState(DEFAULT_WIDTH);
  const [fullscreen, setFullscreen] = useState(false);
  // Stays true through the closing animation, so the rail can leave instead of vanishing.
  const [rendered, setRendered] = useState(open);
  const widthRef = useRef(DEFAULT_WIDTH);
  // The rail's place in the layout (animates its width/position on open and close)...
  const railRef = useRef<HTMLDivElement>(null);
  // ...and the panel inside it (animates its clip when it grows to full screen and back).
  const panelRef = useRef<HTMLDivElement>(null);
  const enterFrom = useRef<DOMRect | null>(null);
  const motion = useRef<Animation | null>(null);

  const run = (el: HTMLElement | null, keyframes: Keyframe[], timing: KeyframeAnimationOptions, done?: () => void) => {
    motion.current?.cancel();
    if (!el || reducedMotion()) {
      motion.current = null;
      done?.();
      return;
    }
    const animation = el.animate(keyframes, timing);
    motion.current = animation;
    let settled = false;
    const settle = () => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      if (motion.current === animation) motion.current = null;
      done?.();
    };
    // A frame-starved page (a background tab, a paused renderer) can hold an animation at t=0
    // forever and never fire onfinish; the state change must not wait on it. A superseded
    // animation is cancelled by the next `run` and settles nothing — the newer one owns the state.
    const timer = window.setTimeout(() => {
      if (motion.current !== animation) return;
      animation.cancel();
      settle();
    }, Number(timing.duration ?? 0) + 80);
    animation.onfinish = settle;
    animation.oncancel = () => {
      if (!settled && motion.current !== animation) window.clearTimeout(timer);
    };
  };

  const slideIn = () => {
    const rail = railRef.current;
    if (docked()) run(rail, [{ width: "0px" }, { width: `${widthRef.current}px` }], IN);
    else run(rail, [{ transform: "translateX(100%)" }, { transform: "translateX(0)" }], IN);
  };

  // Open: mount first, then slide in once the box exists. Close: slide out, then unmount.
  useEffect(() => {
    if (open) {
      // Reopened mid-close: the box is still mounted, so turn the exit around instead of
      // letting it finish (its onfinish would unmount a rail that is meant to be open).
      if (rendered) slideIn();
      else setRendered(true);
      return;
    }
    if (!rendered) return;
    const rail = railRef.current;
    const finish = () => {
      setRendered(false);
      setFullscreen(false);
    };
    if (fullscreen) {
      run(panelRef.current, [{ opacity: 1, transform: "scale(1)" }, { opacity: 0, transform: "scale(0.98)" }], OUT, finish);
    } else if (docked()) {
      run(rail, [{ width: `${widthRef.current}px` }, { width: "0px" }], OUT, finish);
    } else {
      run(rail, [{ transform: "translateX(0)" }, { transform: "translateX(100%)" }], OUT, finish);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useLayoutEffect(() => {
    if (rendered && open) slideIn();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rendered]);

  // Full screen grows out of the rail's own rectangle; leaving folds back into it.
  useLayoutEffect(() => {
    if (!fullscreen || !enterFrom.current) return;
    run(panelRef.current, [{ clipPath: insetOf(enterFrom.current) }, { clipPath: "inset(0px 0px 0px 0px)" }], IN);
    enterFrom.current = null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fullscreen]);

  const toggleFullscreen = useCallback((next: boolean) => {
    if (next) {
      enterFrom.current = panelRef.current?.getBoundingClientRect() ?? null;
      setFullscreen(true);
      return;
    }
    const target = railRef.current?.getBoundingClientRect();
    if (!target) {
      setFullscreen(false);
      return;
    }
    run(panelRef.current, [{ clipPath: "inset(0px 0px 0px 0px)" }, { clipPath: insetOf(target) }], OUT, () => setFullscreen(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const saved = Number(window.localStorage.getItem(storageKey));
    if (saved > 0) {
      widthRef.current = clampWidth(saved);
      setWidth(widthRef.current);
    }
    const onResize = () => {
      widthRef.current = clampWidth(widthRef.current);
      setWidth(widthRef.current);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [storageKey]);

  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") toggleFullscreen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreen, toggleFullscreen]);

  const beginResize = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      // The drag writes the CSS variable directly; state (and the dashboard re-render) updates
      // once, on release.
      const onMove = (move: globalThis.PointerEvent) => {
        widthRef.current = clampWidth(window.innerWidth - move.clientX);
        railRef.current?.style.setProperty("--rail-w", `${widthRef.current}px`);
      };
      const finish = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", finish);
        window.removeEventListener("pointercancel", finish);
        setWidth(widthRef.current);
        window.localStorage.setItem(storageKey, String(widthRef.current));
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", finish);
      window.addEventListener("pointercancel", finish);
    },
    [storageKey],
  );

  if (!rendered) return null;
  const controls: RailControls = {
    fullscreen,
    onToggleFullscreen: () => toggleFullscreen(!fullscreen),
    // Full screen is reset when the closing animation ends (see the open/close effect).
    onClose,
  };
  return (
    <div
      ref={railRef}
      // The rail keeps its place in the row even in full screen, so the page behind never reflows
      // and the panel can fold back into exactly this box. `justify-end` + overflow-hidden make the
      // fixed-width panel slide in from the right while the box's width animates.
      className="fixed inset-y-0 right-0 z-30 flex w-[min(94vw,420px)] justify-end overflow-hidden shadow-2xl lg:relative lg:z-auto lg:w-(--rail-w) lg:shrink-0 lg:shadow-none"
      style={{ "--rail-w": `${width}px` } as CSSProperties}
    >
      {fullscreen ? null : (
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize assistant panel"
          onPointerDown={beginResize}
          className="absolute inset-y-0 left-0 z-10 hidden w-1.5 cursor-col-resize transition-colors hover:bg-(--line) lg:block"
        />
      )}
      <div ref={panelRef} className={fullscreen ? "fixed inset-0 z-40" : "h-full w-full shrink-0 lg:w-(--rail-w)"}>
        {children(controls)}
      </div>
    </div>
  );
}
