import { useEffect, useRef, useState } from "react";
import type { BBox } from "../api/types";
import { cx } from "../lib/format";

type Handle = "nw" | "ne" | "sw" | "se" | "move" | null;

interface Drag {
  mode: "create" | "resize" | "move";
  index: number;
  handle: Handle;
  originX: number;
  originY: number;
  startBox: [number, number, number, number];
}

/** Lado da alça e espessura do traço, em pixels DE TELA (ver `chrome` abaixo). */
const HANDLE_PX = 10;
const STROKE_PX = 2;

const HANDLES: {
  id: Handle;
  className: string;
  vertical: "top" | "bottom";
  horizontal: "left" | "right";
}[] = [
  { id: "nw", className: "cursor-nwse-resize", vertical: "top", horizontal: "left" },
  { id: "ne", className: "cursor-nesw-resize", vertical: "top", horizontal: "right" },
  { id: "sw", className: "cursor-nesw-resize", vertical: "bottom", horizontal: "left" },
  { id: "se", className: "cursor-nwse-resize", vertical: "bottom", horizontal: "right" },
];

const MIN_SIZE = 0.004; // ~2px num frame de 480px: evita bbox degenerado por clique

function clamp01(value: number) {
  return Math.max(0, Math.min(1, value));
}

function normalize(box: [number, number, number, number]): [number, number, number, number] {
  const [x1, y1, x2, y2] = box;
  return [Math.min(x1, x2), Math.min(y1, y2), Math.max(x1, x2), Math.max(y1, y2)];
}

/**
 * Overlay de bboxes sobre o frame.
 *
 * Trabalha inteiramente em coordenadas normalizadas 0-1, medidas contra o
 * retângulo RENDERIZADO da imagem. Assim nada depende da largura do proxy (480px)
 * nem do zoom da janela, e o valor gravado é exatamente o que o SAM3 consome.
 *
 * CROMAGEM CONTRA-ESCALADA (`chrome`, abaixo). Este overlay é filho da div que
 * recebe o `transform: scale(zoom)` do FramePreview — de propósito, é o que mantém
 * as coordenadas normalizadas válidas de graça. O efeito colateral é que TODO
 * comprimento em px autorado aqui sai multiplicado por `zoom` na tela: em 8× o
 * traço de 2px virava 16px e a alça de 10px virava 80px, cobrindo exatamente os
 * pixels que se foi inspecionar — dar zoom passava a atrapalhar em vez de ajudar.
 *
 * Por isso todo px de cromagem é dividido por `zoom`. E o ponto que não é óbvio:
 * isso NÃO deixa a alça mais difícil de pegar. Uma alça de `10/zoom` renderiza a
 * 10 pixels DE TELA em qualquer zoom, e pixel de tela é o que o ponteiro acerta.
 * Não "conserte" de volta para um valor fixo.
 *
 * TRAÇO COM `box-shadow`, NUNCA `border`. O Chrome arredonda `border-width` para
 * um número INTEIRO de pixels, com mínimo de 1px quando não-zero. Em 8× a conta
 * dá 2/8 = 0.25px, que vira 1px usado e reaparece como 8px na tela — medido. Pior
 * na alça: a borda de 1px não cabe na largura de 1.2px e o box-sizing empurra a
 * largura usada para 2px, inflando a alça para 16px de tela. `box-shadow` é
 * pintura pura: aceita sub-pixel, é antialiasado, e (como `outline`) não entra no
 * modelo de caixa — então o traço `inset` não come área útil nem desloca o overlay
 * em relação à imagem, que é a razão pela qual a moldura do frame já evitava
 * `border` (ver FramePreview). A cor vem de `currentColor`, para o Tailwind
 * continuar dono da paleta.
 */
export function BboxCanvas({
  bboxes,
  selected,
  onChange,
  onSelect,
  enabled,
  zoom,
  ariaLabel,
}: {
  bboxes: BBox[];
  selected: number;
  onChange: (boxes: BBox[]) => void;
  onSelect: (index: number) => void;
  enabled: boolean;
  zoom: number;
  /** Nome acessível para overlays somente de leitura, como a bbox da máscara. */
  ariaLabel?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<Drag | null>(null);

  // px de tela -> px autorado, desfazendo o scale do wrapper.
  const chrome = (screenPx: number) => screenPx / Math.max(zoom, 1);

  const toLocal = (event: { clientX: number; clientY: number }) => {
    const rect = ref.current!.getBoundingClientRect();
    return {
      x: clamp01((event.clientX - rect.left) / rect.width),
      y: clamp01((event.clientY - rect.top) / rect.height),
    };
  };

  useEffect(() => {
    if (!drag) return;

    const onMove = (event: PointerEvent) => {
      const { x, y } = toLocal(event);
      const [sx1, sy1, sx2, sy2] = drag.startBox;
      let box: [number, number, number, number];

      if (drag.mode === "move") {
        const dx = x - drag.originX;
        const dy = y - drag.originY;
        const width = sx2 - sx1;
        const height = sy2 - sy1;
        const nx = Math.max(0, Math.min(sx1 + dx, 1 - width));
        const ny = Math.max(0, Math.min(sy1 + dy, 1 - height));
        box = [nx, ny, nx + width, ny + height];
      } else if (drag.mode === "create") {
        box = normalize([drag.originX, drag.originY, x, y]);
      } else {
        const corner: Record<string, [number, number, number, number]> = {
          nw: [x, y, sx2, sy2],
          ne: [sx1, y, x, sy2],
          sw: [x, sy1, sx2, y],
          se: [sx1, sy1, x, y],
        };
        box = normalize(corner[drag.handle as string] ?? drag.startBox);
      }

      const next = [...bboxes];
      if (drag.mode === "create" && drag.index >= next.length) {
        next.push({ obj_id: next.length + 1, label: "boom", normalized: box });
      } else {
        next[drag.index] = { ...next[drag.index], normalized: box };
      }
      onChange(next);
    };

    const onUp = () => {
      setDrag(null);
      // Descarta bbox degenerado (clique sem arrasto). O limiar acompanha o zoom:
      // é fixo em coordenada normalizada, então em 8× um bbox legítimo de poucos
      // pixels do frame cairia abaixo dele e seria apagado em silêncio — logo o
      // caso que se ampliou a imagem justamente para conseguir desenhar.
      const minSize = MIN_SIZE / Math.max(zoom, 1);
      const box = bboxes[drag.index];
      if (box) {
        const [x1, y1, x2, y2] = box.normalized;
        if (x2 - x1 < minSize || y2 - y1 < minSize) {
          onChange(bboxes.filter((_, index) => index !== drag.index));
          onSelect(-1);
        }
      }
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [drag, bboxes, onChange, onSelect, zoom]);

  const startCreate = (event: React.PointerEvent) => {
    // Alt+arrastar é reservado para mover a imagem com zoom; sem esta guarda o
    // evento criaria um bbox antes de chegar ao handler de pan.
    if (!enabled || event.button !== 0 || event.altKey) return;
    const { x, y } = toLocal(event);
    const index = bboxes.length;
    onChange([...bboxes, { obj_id: index + 1, label: "boom", normalized: [x, y, x, y] }]);
    onSelect(index);
    setDrag({ mode: "create", index, handle: null, originX: x, originY: y, startBox: [x, y, x, y] });
  };

  return (
    <div
      ref={ref}
      aria-label={ariaLabel}
      onPointerDown={startCreate}
      className={cx(
        "absolute inset-0",
        enabled ? "cursor-crosshair" : "pointer-events-none",
      )}
    >
      {ariaLabel && <span className="sr-only">{ariaLabel}</span>}
      {bboxes.map((box, index) => {
        const [x1, y1, x2, y2] = box.normalized;
        const isSelected = index === selected;
        return (
          <div
            key={index}
            onPointerDown={(event) => {
              if (!enabled) return;
              event.stopPropagation();
              onSelect(index);
              const { x, y } = toLocal(event);
              setDrag({
                mode: "move",
                index,
                handle: "move",
                originX: x,
                originY: y,
                startBox: box.normalized,
              });
            }}
            style={{
              left: `${x1 * 100}%`,
              top: `${y1 * 100}%`,
              width: `${(x2 - x1) * 100}%`,
              height: `${(y2 - y1) * 100}%`,
              // `inset`: o traço é desenhado PARA DENTRO, então a aresta externa
              // do retângulo é exatamente o rect normalizado — sem superestimar
              // um alvo de 20px num 4K, que é o oposto do que se quer ao ampliar.
              boxShadow: `inset 0 0 0 ${chrome(STROKE_PX)}px currentColor`,
            }}
            className={cx(
              "absolute",
              enabled && "cursor-move",
              isSelected
                ? "bg-amber-400/10 text-amber-400"
                : "text-emerald-500/80 hover:text-emerald-400",
            )}
          >
            <span
              style={{
                // Escalar o transform, não o font-size: tamanho de fonte
                // fracionário abaixo de ~6px renderiza de forma inconsistente
                // entre níveis de zoom. Com origem no canto inferior esquerdo a
                // etiqueta fica colada na aresta superior da caixa em vez de
                // fugir do frame em 8×.
                transform: `scale(${1 / Math.max(zoom, 1)})`,
                transformOrigin: "bottom left",
              }}
              className={cx(
                // pointer-events-none porque a etiqueta fica DENTRO da caixa em
                // termos de eventos: sem isso ela engolia o pointerdown que
                // iniciaria o arrasto perto do canto superior esquerdo.
                "tnum pointer-events-none absolute bottom-full left-0 rounded px-1 text-[10px] leading-tight",
                isSelected ? "bg-amber-400 text-zinc-950" : "bg-emerald-600 text-white",
              )}
            >
              #{box.obj_id || index + 1}
            </span>

            {enabled &&
              isSelected &&
              HANDLES.map((handle) => (
                <span
                  key={handle.id}
                  onPointerDown={(event) => {
                    event.stopPropagation();
                    const { x, y } = toLocal(event);
                    setDrag({
                      mode: "resize",
                      index,
                      handle: handle.id,
                      originX: x,
                      originY: y,
                      startBox: box.normalized,
                    });
                  }}
                  style={{
                    width: `${chrome(HANDLE_PX)}px`,
                    height: `${chrome(HANDLE_PX)}px`,
                    borderRadius: `${chrome(2)}px`,
                    // O contorno escuro que destaca a alça sobre imagem clara —
                    // box-shadow em vez de border pelo motivo no topo do arquivo.
                    boxShadow: `inset 0 0 0 ${chrome(1)}px rgb(24 24 27)`,
                    // Centrada na quina: metade do lado para fora, em cada eixo.
                    [handle.vertical]: `${chrome(-HANDLE_PX / 2)}px`,
                    [handle.horizontal]: `${chrome(-HANDLE_PX / 2)}px`,
                  }}
                  className={cx("absolute bg-amber-400", handle.className)}
                />
              ))}
          </div>
        );
      })}
    </div>
  );
}
