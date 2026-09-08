import { cx } from "../lib/format";
import { useAnnotator } from "../store/annotator";

/** Barra da duração inteira: visão geral dos intervalos + clique para saltar. */
export function Timeline() {
  const frame = useAnnotator((s) => s.currentFrame);
  const frameCount = useAnnotator((s) => s.frameCount());
  const intervals = useAnnotator((s) => s.intervals);
  const selected = useAnnotator((s) => s.selected);
  const setFrame = useAnnotator((s) => s.setFrame);
  const selectInterval = useAnnotator((s) => s.selectInterval);

  if (frameCount <= 0) return null;
  const pct = (value: number) => `${(value / frameCount) * 100}%`;

  return (
    <div
      className="group relative h-8 shrink-0 cursor-pointer border-t border-zinc-800 bg-zinc-900"
      onClick={(event) => {
        const rect = event.currentTarget.getBoundingClientRect();
        const ratio = (event.clientX - rect.left) / rect.width;
        // Salto grosseiro: continua provisório até confirmar no filmstrip.
        setFrame(Math.round(ratio * (frameCount - 1)), true);
      }}
    >
      {intervals.map((interval, index) => {
        if (interval.start === null) return null;
        const end = interval.end ?? interval.start;
        return (
          <button
            key={interval.key}
            onClick={(event) => {
              event.stopPropagation();
              selectInterval(index);
              setFrame(interval.start!, false);
            }}
            title={`intervalo ${index + 1}: ${interval.start}–${end}`}
            style={{ left: pct(interval.start), width: pct(Math.max(end - interval.start, 1)) }}
            className={cx(
              "absolute inset-y-1 min-w-[3px] rounded-sm",
              index === selected
                ? "bg-amber-400/80 ring-1 ring-amber-300"
                : "bg-emerald-600/70 hover:bg-emerald-500",
            )}
          />
        );
      })}

      <span
        style={{ left: pct(frame) }}
        className="pointer-events-none absolute inset-y-0 w-px bg-zinc-100"
      >
        <span className="absolute -top-px -left-[3px] h-1.5 w-1.5 rounded-full bg-zinc-100" />
      </span>
    </div>
  );
}
