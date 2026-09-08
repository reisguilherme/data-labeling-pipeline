import { useState } from "react";
import { cx } from "../lib/format";
import {
  BOOST_ADJUST,
  NEUTRAL_ADJUST,
  useAnnotator,
  ZOOM_MAX,
  ZOOM_MIN,
} from "../store/annotator";
import { Kbd } from "./ui";

const SLIDERS = [
  { key: "brightness", label: "brilho", min: 0.5, max: 2 },
  { key: "contrast", label: "contraste", min: 0.5, max: 2.5 },
  { key: "saturate", label: "saturação", min: 0, max: 2.5 },
] as const;

/** Zoom e realce de imagem, sobrepostos ao palco. */
export function StageControls() {
  const zoom = useAnnotator((s) => s.zoom);
  const setZoom = useAnnotator((s) => s.setZoom);
  const resetView = useAnnotator((s) => s.resetView);
  const adjust = useAnnotator((s) => s.adjust);
  const setAdjust = useAnnotator((s) => s.setAdjust);
  const toggleBoost = useAnnotator((s) => s.toggleBoost);

  const [open, setOpen] = useState(false);
  const boosted = adjust.contrast !== NEUTRAL_ADJUST.contrast || adjust.brightness !== NEUTRAL_ADJUST.brightness;

  return (
    <div className="absolute right-3 bottom-3 flex flex-col items-end gap-2">
      {open && (
        <div className="w-56 rounded-md border border-zinc-800 bg-zinc-900/95 p-2.5">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[10px] tracking-wide text-zinc-500 uppercase">
              Realce
            </span>
            <button
              onClick={() => setAdjust(NEUTRAL_ADJUST)}
              className="text-[10px] text-zinc-500 hover:text-zinc-200"
            >
              neutro
            </button>
          </div>

          {SLIDERS.map((slider) => (
            <label key={slider.key} className="mb-1.5 block">
              <span className="flex justify-between text-[10px] text-zinc-400">
                {slider.label}
                <span className="tnum text-zinc-600">
                  {adjust[slider.key].toFixed(2)}
                </span>
              </span>
              <input
                type="range"
                min={slider.min}
                max={slider.max}
                step={0.01}
                value={adjust[slider.key]}
                onChange={(event) =>
                  setAdjust({ [slider.key]: Number(event.target.value) })
                }
                className="w-full accent-emerald-500"
              />
            </label>
          ))}

          <button
            onClick={() => setAdjust(BOOST_ADJUST)}
            className="mt-1 w-full rounded border border-zinc-800 py-1 text-[10px] text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
          >
            preset para material log
          </button>
        </div>
      )}

      <div className="flex items-center gap-1 rounded-md border border-zinc-800 bg-zinc-900/95 px-1.5 py-1">
        <button
          onClick={toggleBoost}
          title="Realçar contraste — material log/flat esconde reflexos (G)"
          className={cx(
            "rounded px-1.5 py-0.5 text-[11px]",
            boosted
              ? "bg-emerald-950 text-emerald-300"
              : "text-zinc-400 hover:text-zinc-100",
          )}
        >
          realce
        </button>
        <button
          onClick={() => setOpen((value) => !value)}
          className="rounded px-1 py-0.5 text-[11px] text-zinc-500 hover:text-zinc-200"
          title="ajustes finos"
        >
          {open ? "▾" : "▸"}
        </button>

        <span className="mx-0.5 h-4 w-px bg-zinc-800" />

        <button
          onClick={() => setZoom(zoom / 1.5)}
          disabled={zoom <= ZOOM_MIN}
          className="rounded px-1.5 py-0.5 text-[11px] text-zinc-400 hover:text-zinc-100 disabled:opacity-30"
        >
          −
        </button>
        <button
          onClick={resetView}
          className="tnum min-w-10 rounded px-1 py-0.5 text-[11px] text-zinc-300 hover:text-zinc-100"
          title="clique para resetar (0)"
        >
          {zoom.toFixed(1)}×
        </button>
        <button
          onClick={() => setZoom(zoom * 1.5)}
          disabled={zoom >= ZOOM_MAX}
          className="rounded px-1.5 py-0.5 text-[11px] text-zinc-400 hover:text-zinc-100 disabled:opacity-30"
        >
          +
        </button>
        <Kbd>scroll</Kbd>
      </div>
    </div>
  );
}
