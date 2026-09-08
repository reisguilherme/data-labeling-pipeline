import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { cx } from "../lib/format";
import { useAnnotator } from "../store/annotator";

const ITEM_WIDTH = 96;
const OVERSCAN = 8;
const STRIDES = [1, 2, 5, 10, 30, 60, 120];

/**
 * Filmstrip virtualizado.
 *
 * Aguenta ~6000 frames sem alocar 6000 imagens: largura de item fixa dispensa
 * medição e biblioteca de virtualização — um spacer dá a largura total e só a
 * fatia visível (±overscan) existe no DOM, ~60-80 nós, constante.
 *
 * O zoom é por STRIDE, não por escala: em stride 120 um vídeo de 6000 frames vira
 * ~50 miniaturas (visão geral); em stride 1 é exato ao frame. As alças de in/out
 * aparecem na posição verdadeira em qualquer stride.
 */
export function Filmstrip({ videoId }: { videoId: string }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [scrollLeft, setScrollLeft] = useState(0);
  const [viewport, setViewport] = useState(0);

  const frame = useAnnotator((s) => s.currentFrame);
  const stride = useAnnotator((s) => s.filmstripStride);
  const setStride = useAnnotator((s) => s.setStride);
  const setFrame = useAnnotator((s) => s.setFrame);
  const setPhase = useAnnotator((s) => s.setPhase);
  const frameCount = useAnnotator((s) => s.frameCount());
  const intervals = useAnnotator((s) => s.intervals);
  const selected = useAnnotator((s) => s.selected);
  // Assina o objeto (referência estável) e deriva fora do seletor — `?? []`
  // dentro de um seletor cria array novo a cada chamada e trava o React.
  const proxy = useAnnotator((s) => s.proxy);
  const ranges = proxy?.available_ranges;
  const complete = proxy?.complete ?? false;

  const slots = Math.max(Math.ceil(frameCount / stride), 1);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const observer = new ResizeObserver(() => setViewport(element.clientWidth));
    observer.observe(element);
    setViewport(element.clientWidth);
    return () => observer.disconnect();
  }, []);

  // Mantém o playhead visível quando o frame muda por teclado/timeline.
  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const target = (frame / stride) * ITEM_WIDTH;
    const left = element.scrollLeft;
    const right = left + element.clientWidth;
    if (target < left + ITEM_WIDTH || target > right - ITEM_WIDTH * 2) {
      element.scrollTo({ left: Math.max(target - element.clientWidth / 2, 0) });
    }
  }, [frame, stride]);

  const onWheel = useCallback(
    (event: React.WheelEvent) => {
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        const at = STRIDES.indexOf(stride);
        const next = STRIDES[Math.max(0, Math.min(at + (event.deltaY > 0 ? 1 : -1), STRIDES.length - 1))];
        setStride(next);
      } else if (event.deltaY !== 0 && scrollRef.current) {
        scrollRef.current.scrollLeft += event.deltaY;
      }
    },
    [stride, setStride],
  );

  const first = Math.max(Math.floor(scrollLeft / ITEM_WIDTH) - OVERSCAN, 0);
  const last = Math.min(first + Math.ceil(viewport / ITEM_WIDTH) + OVERSCAN * 2, slots);

  const items = [];
  for (let slot = first; slot < last; slot += 1) {
    const target = slot * stride;
    if (target >= frameCount) break;

    const available =
      complete || (ranges?.some(([a, b]) => a <= target && target <= b) ?? false);
    const isCurrent = Math.abs(target - frame) < stride / 2;
    const inInterval = intervals.some(
      (interval) =>
        interval.start !== null &&
        interval.end !== null &&
        target >= interval.start &&
        target <= interval.end,
    );
    const active = intervals[selected];
    const isBoundary =
      active && (active.start === target || active.end === target);

    items.push(
      <button
        key={slot}
        onClick={() => {
          // Clicar no filmstrip torna o frame EXATO: este índice indexa um JPEG
          // realmente extraído, não uma estimativa de tempo do player.
          setFrame(target, false);
          setPhase("refine");
        }}
        style={{ left: slot * ITEM_WIDTH, width: ITEM_WIDTH }}
        className={cx(
          "absolute top-0 bottom-0 overflow-hidden border-r border-zinc-950",
          isCurrent && "ring-2 ring-amber-400 ring-inset",
        )}
      >
        {available ? (
          <img
            src={api.frameUrl(videoId, target)}
            alt=""
            loading="lazy"
            decoding="async"
            className={cx(
              "h-full w-full object-cover",
              inInterval ? "opacity-100" : "opacity-55",
            )}
          />
        ) : (
          <div className="h-full w-full bg-zinc-900/60" />
        )}

        {inInterval && <span className="absolute inset-x-0 bottom-0 h-0.5 bg-emerald-500" />}
        {isBoundary && <span className="absolute inset-y-0 left-0 w-0.5 bg-amber-400" />}

        <span className="tnum absolute top-0.5 left-0.5 rounded bg-zinc-950/75 px-1 text-[9px] text-zinc-400">
          {target}
        </span>
      </button>,
    );
  }

  return (
    <div className="relative shrink-0 border-t border-zinc-800 bg-zinc-950">
      <div className="flex items-center justify-between px-3 py-1">
        <span className="text-[10px] tracking-wide text-zinc-600 uppercase">
          Filmstrip
        </span>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-zinc-600">ctrl+scroll = zoom</span>
          <span className="tnum rounded bg-zinc-900 px-1.5 py-0.5 text-[10px] text-zinc-400">
            1 : {stride}
          </span>
        </div>
      </div>

      <div
        ref={scrollRef}
        onScroll={(event) => setScrollLeft(event.currentTarget.scrollLeft)}
        onWheel={onWheel}
        className="relative h-20 overflow-x-auto overflow-y-hidden"
      >
        <div className="relative h-full" style={{ width: slots * ITEM_WIDTH }}>
          {items}
        </div>
      </div>
    </div>
  );
}
