import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { ReviewFrame } from "../api/types";
import { cx } from "../lib/format";

const ITEM_WIDTH = 72;
const OVERSCAN = 6;

/**
 * Tira de frames do segmento, colorida pelo estado da revisão.
 *
 * Virtualizada pelo mesmo motivo do Filmstrip da triagem: o maior segmento tem
 * 799 frames, e montar 799 <img> de 4K derrubaria a navegação — que é
 * exatamente o que precisa ser instantâneo aqui. Largura fixa dispensa medição:
 * um spacer dá a largura total e só a fatia visível existe no DOM.
 *
 * É um componente separado do Filmstrip, e não uma generalização dele, porque
 * os dois respondem a stores diferentes; unificá-los exigiria parametrizar
 * origem dos frames, marcações e navegação — mais acoplamento do que os ~70
 * nós de DOM que eles compartilham justificam.
 */
export function ReviewStrip({
  videoId,
  segment,
  exportVersion,
  frames,
  current,
  onPick,
}: {
  videoId: string;
  segment: string;
  exportVersion: string;
  frames: ReviewFrame[];
  current: number;
  onPick: (frame: number) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [scrollLeft, setScrollLeft] = useState(0);
  const [viewport, setViewport] = useState(0);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const observer = new ResizeObserver(() => setViewport(element.clientWidth));
    observer.observe(element);
    setViewport(element.clientWidth);
    return () => observer.disconnect();
  }, []);

  // Mantém o frame atual visível quando a navegação vem do teclado.
  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const target = current * ITEM_WIDTH;
    const left = element.scrollLeft;
    const right = left + element.clientWidth;
    if (target < left + ITEM_WIDTH || target > right - ITEM_WIDTH * 2) {
      element.scrollTo({ left: Math.max(target - element.clientWidth / 2, 0) });
    }
  }, [current]);

  const first = Math.max(Math.floor(scrollLeft / ITEM_WIDTH) - OVERSCAN, 0);
  const last = Math.min(
    Math.ceil((scrollLeft + viewport) / ITEM_WIDTH) + OVERSCAN,
    frames.length,
  );
  const visible = frames.slice(first, last);

  return (
    <div className="shrink-0 border-t border-zinc-800 bg-zinc-950">
      <div
        ref={scrollRef}
        onScroll={(event) => setScrollLeft(event.currentTarget.scrollLeft)}
        className="overflow-x-auto overflow-y-hidden"
      >
        <div
          className="relative h-[68px]"
          style={{ width: `${frames.length * ITEM_WIDTH}px` }}
        >
          {visible.map((frame) => {
            const isCurrent = frame.frame === current;
            return (
              <button
                key={frame.frame}
                onClick={() => onPick(frame.frame)}
                title={`frame ${frame.frame}`}
                style={{
                  left: `${frame.frame * ITEM_WIDTH}px`,
                  width: `${ITEM_WIDTH}px`,
                }}
                className={cx(
                  "absolute top-0 h-full border-r border-zinc-900 p-0.5",
                  isCurrent && "ring-1 ring-violet-400 ring-inset",
                )}
              >
                <img
                  src={api.segmentFrameUrl(videoId, segment, frame.frame, exportVersion)}
                  alt=""
                  loading="lazy"
                  decoding="async"
                  className="h-[52px] w-full object-cover"
                />
                {/* A cor é a informação: violeta = conferido, âmbar = corrigido,
                    cinza = ainda não visto. Dá para ver de relance onde parou. */}
                <span
                  className={cx(
                    "block h-1 w-full",
                    frame.status === "edited"
                      ? "bg-amber-500"
                      : frame.status === "ok"
                        ? "bg-violet-500"
                        : "bg-zinc-800",
                  )}
                />
                {frame.boxes.length === 0 && (
                  <span className="tnum absolute top-0.5 right-1 text-[9px] text-zinc-500">
                    —
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
