import type { FlagGroup } from "../api/types";
import { cx } from "../lib/format";
import { useSession } from "../store/session";
import { useAnnotator } from "../store/annotator";
import { Button, Panel } from "./ui";

export function IntervalList() {
  const intervals = useAnnotator((s) => s.intervals);
  const selected = useAnnotator((s) => s.selected);
  const selectInterval = useAnnotator((s) => s.selectInterval);
  const removeInterval = useAnnotator((s) => s.removeInterval);
  const addInterval = useAnnotator((s) => s.addInterval);
  const setFrame = useAnnotator((s) => s.setFrame);
  const setPhase = useAnnotator((s) => s.setPhase);

  return (
    <Panel
      title="Intervalos"
      right={
        <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => addInterval()} kbd="N">
          novo
        </Button>
      }
    >
      {intervals.length === 0 ? (
        <p className="text-xs text-zinc-600">
          Assista ao vídeo e aperte <span className="text-zinc-400">I</span> quando o boom
          aparecer.
        </p>
      ) : (
        <ul className="space-y-1">
          {intervals.map((interval, index) => {
            const incomplete =
              interval.start === null ||
              interval.end === null ||
              interval.bboxes.length === 0 ||
              !interval.flags.dificuldade;
            const provisional = interval.startProvisional || interval.endProvisional;

            return (
              <li key={interval.key}>
                <button
                  onClick={() => {
                    selectInterval(index);
                    if (interval.start !== null) {
                      setFrame(interval.start, interval.startProvisional);
                      setPhase("refine");
                    }
                  }}
                  className={cx(
                    "flex w-full items-center gap-2 rounded border px-2 py-1.5 text-left text-xs",
                    index === selected
                      ? "border-amber-700 bg-amber-950/40"
                      : "border-zinc-800 bg-zinc-950/60 hover:border-zinc-700",
                  )}
                >
                  <span className="tnum text-zinc-500">{index + 1}</span>
                  <span className="tnum flex-1 text-zinc-200">
                    {interval.start ?? "—"}
                    <span className="text-zinc-600"> → </span>
                    {interval.end ?? "—"}
                    {provisional && <span className="ml-1 text-amber-500">~</span>}
                  </span>
                  <span className="tnum text-zinc-600">
                    {interval.start !== null && interval.end !== null
                      ? `${interval.end - interval.start + 1}f`
                      : ""}
                  </span>
                  {interval.bboxes.length > 0 && (
                    <span className="rounded bg-emerald-950 px-1 text-[10px] text-emerald-400">
                      {interval.bboxes.length}
                    </span>
                  )}
                  {incomplete && <span className="text-amber-500">●</span>}
                  <span
                    role="button"
                    tabIndex={0}
                    onClick={(event) => {
                      event.stopPropagation();
                      removeInterval(index);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") removeInterval(index);
                    }}
                    className="px-1 text-zinc-600 hover:text-red-400"
                  >
                    ×
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

export function BboxPanel() {
  const intervals = useAnnotator((s) => s.intervals);
  const selected = useAnnotator((s) => s.selected);
  const selectedBbox = useAnnotator((s) => s.selectedBbox);
  const selectBbox = useAnnotator((s) => s.selectBbox);
  const setBboxes = useAnnotator((s) => s.setBboxes);
  const setPhase = useAnnotator((s) => s.setPhase);
  const setFrame = useAnnotator((s) => s.setFrame);

  const interval = selected >= 0 ? intervals[selected] : null;
  if (!interval) return null;

  return (
    <Panel
      title="Bboxes do frame inicial"
      right={
        <Button
          variant="ghost"
          className="px-2 py-0.5 text-xs"
          disabled={interval.start === null}
          onClick={() => {
            setFrame(interval.start!, false);
            setPhase("bbox");
          }}
          kbd="B"
        >
          desenhar
        </Button>
      }
    >
      {interval.bboxes.length === 0 ? (
        <p className="text-xs text-zinc-600">
          Nenhum bbox. Aperte <span className="text-zinc-400">B</span> e arraste sobre o objeto.
        </p>
      ) : (
        <ul className="space-y-1">
          {interval.bboxes.map((box, index) => (
            <li
              key={index}
              onClick={() => selectBbox(index)}
              className={cx(
                "flex cursor-pointer items-center gap-2 rounded border px-2 py-1 text-xs",
                index === selectedBbox
                  ? "border-amber-700 bg-amber-950/40"
                  : "border-zinc-800 bg-zinc-950/60",
              )}
            >
              <span className="tnum rounded bg-zinc-800 px-1 text-[10px] text-zinc-300">
                #{box.obj_id || index + 1}
              </span>
              <span className="tnum flex-1 text-zinc-500">
                {box.normalized.map((v) => v.toFixed(3)).join(", ")}
              </span>
              <button
                onClick={(event) => {
                  event.stopPropagation();
                  setBboxes(interval.bboxes.filter((_, at) => at !== index));
                }}
                className="text-zinc-600 hover:text-red-400"
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function FlagGroupControl({ group }: { group: FlagGroup }) {
  const intervals = useAnnotator((s) => s.intervals);
  const selected = useAnnotator((s) => s.selected);
  const setFlag = useAnnotator((s) => s.setFlag);

  const interval = selected >= 0 ? intervals[selected] : null;
  const value = interval?.flags[group.id];

  const toggle = (optionId: string) => {
    if (!interval) return;
    if (group.multi) {
      const current = Array.isArray(value) ? value : [];
      setFlag(
        group.id,
        current.includes(optionId)
          ? current.filter((item) => item !== optionId)
          : [...current, optionId],
      );
    } else {
      setFlag(group.id, value === optionId ? null : optionId);
    }
  };

  const isOn = (optionId: string) =>
    group.multi ? Array.isArray(value) && value.includes(optionId) : value === optionId;

  return (
    <div>
      <div className="mb-1 flex items-baseline gap-1.5">
        <span className="text-[11px] text-zinc-400">{group.label}</span>
        {group.required && !value && <span className="text-[10px] text-amber-500">obrigatório</span>}
      </div>
      <div className="flex flex-wrap gap-1">
        {group.options.map((option) => (
          <button
            key={option.id}
            onClick={() => toggle(option.id)}
            disabled={!interval}
            className={cx(
              "rounded border px-2 py-0.5 text-[11px] transition-colors disabled:opacity-40",
              isOn(option.id)
                ? "border-emerald-700 bg-emerald-950 text-emerald-300"
                : "border-zinc-800 bg-zinc-950/60 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200",
            )}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}

export function FlagsPanel() {
  // Renderizado inteiramente a partir de /api/config: nenhum nome de flag é
  // conhecido pelo React. Editar server/flags.py muda UI e validação juntas.
  // Assina `config` (referência estável) e deriva fora do seletor.
  const config = useSession((s) => s.config);
  const groups = config?.flag_groups ?? [];
  const intervals = useAnnotator((s) => s.intervals);
  const selected = useAnnotator((s) => s.selected);
  const patchInterval = useAnnotator((s) => s.patchInterval);

  const interval = selected >= 0 ? intervals[selected] : null;
  const suggested = useAnnotator((s) => s.suggestedFlags);
  const hasSuggestion = Object.keys(suggested).length > 0;

  return (
    <Panel title={`Flags${interval ? ` · intervalo ${selected + 1}` : ""}`}>
      {!interval ? (
        <p className="text-xs text-zinc-600">selecione um intervalo</p>
      ) : (
        <div id="flags-panel" className="space-y-3">
          {hasSuggestion && (
            <p className="rounded border border-zinc-800 bg-zinc-950/60 px-2 py-1 text-[10px] text-zinc-500">
              pré-marcado pelo nome do arquivo — confira
            </p>
          )}
          {groups.map((group) => (
            <FlagGroupControl key={group.id} group={group} />
          ))}

          <div>
            <span className="mb-1 block text-[11px] text-zinc-400">Observações do intervalo</span>
            <textarea
              aria-label="Observações do intervalo"
              value={interval.notes}
              onChange={(event) => patchInterval(selected, { notes: event.target.value })}
              rows={2}
              className="w-full resize-none rounded border border-zinc-800 bg-zinc-950 px-2 py-1 text-xs text-zinc-200"
            />
          </div>
        </div>
      )}
    </Panel>
  );
}
