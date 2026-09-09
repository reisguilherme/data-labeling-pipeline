import { useCallback, useEffect, useMemo } from "react";
import { api, watchJob } from "../api/client";
import type { VideoListItem } from "../api/types";
import { Filmstrip } from "../components/Filmstrip";
import { FramePreview } from "../components/FramePreview";
import { ShortcutsHelp } from "../components/ShortcutsHelp";
import { StageControls } from "../components/StageControls";
import { BboxPanel, FlagsPanel, IntervalList } from "../components/SidePanel";
import { Timeline } from "../components/Timeline";
import { VideoPlayer } from "../components/VideoPlayer";
import { Button, Panel, Spinner } from "../components/ui";
import { cx, timecode } from "../lib/format";
import { installNavigationGuard } from "../lib/routes";
import { useFramePlayback } from "../lib/useFramePlayback";
import { useKeyboard } from "../lib/useKeyboard";
import { computeBlockers, useAnnotator } from "../store/annotator";
import { useLibrary } from "../store/library";

export function AnnotatorView({
  video,
  onBack,
  onNavigate,
}: {
  video: VideoListItem;
  onBack: () => void;
  onNavigate: (target: VideoListItem) => void;
}) {
  const open = useAnnotator((s) => s.open);
  const close = useAnnotator((s) => s.close);
  const meta = useAnnotator((s) => s.meta);
  const loading = useAnnotator((s) => s.loading);
  const error = useAnnotator((s) => s.error);
  const saving = useAnnotator((s) => s.saving);
  const dirty = useAnnotator((s) => s.dirty);
  const status = useAnnotator((s) => s.status);
  const phase = useAnnotator((s) => s.phase);
  const setPhase = useAnnotator((s) => s.setPhase);
  const frame = useAnnotator((s) => s.currentFrame);
  const frameProvisional = useAnnotator((s) => s.frameProvisional);
  const frameCount = useAnnotator((s) => s.frameCount());
  const job = useAnnotator((s) => s.job);
  const intervals = useAnnotator((s) => s.intervals);
  const blockers = useMemo(() => computeBlockers(intervals), [intervals]);
  const videoNotes = useAnnotator((s) => s.videoNotes);
  const setVideoNotes = useAnnotator((s) => s.setVideoNotes);

  const refresh = useLibrary((s) => s.refresh);
  const neighbours = useLibrary((s) => s.neighbours);
  const nextPending = useLibrary((s) => s.nextPending);

  useEffect(() => {
    void open(video.video_id);
    return () => close();
  }, [video.video_id, open, close]);

  useEffect(
    () =>
      installNavigationGuard(() => {
        const state = useAnnotator.getState();
        if (state.saving) return false;
        if (!state.dirty) return true;
        return window.confirm("Há alterações não salvas neste vídeo. Deseja descartá-las?");
      }),
    [],
  );

  useEffect(() => {
    if (!dirty && !saving) return;
    const protectUnsavedWork = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", protectUnsavedWork);
    return () => window.removeEventListener("beforeunload", protectUnsavedWork);
  }, [dirty, saving]);

  const fps = meta?.media.fps ?? null;
  const offset = meta?.media.start_time_sec ?? 0;
  const seconds = fps ? offset + frame / fps : null;
  const playable = meta?.media.browser_playable !== false;

  // O <video> nativo só entra em cena quando o navegador consegue decodificar o
  // codec. Fora isso — HEVC, por exemplo — quem reproduz é a sequência de frames
  // já extraídos, que ainda por cima é exata ao frame.
  const nativePlayer = phase === "scan" && playable;
  useFramePlayback(!nativePlayer && phase !== "bbox");

  const handleSave = useCallback(async () => {
    const state = useAnnotator.getState();
    const ok = await state.save();
    if (ok) {
      void refresh();
      // Intervalo guardado: o passo natural é voltar a varrer atrás do próximo,
      // não continuar parado no frame do bbox.
      state.setPhase("scan");
      state.resetView();
    }
    return ok;
  }, [refresh]);

  const handleSaveExportNext = useCallback(async () => {
    const state = useAnnotator.getState();
    if (state.blockers().length) return;
    // A lista atual é a referência estável para navegar. A mutação precisa ser
    // confirmada primeiro, mas um refresh lento não deve manter o operador preso.
    const next = useLibrary.getState().nextPending(video.video_id);
    if (!(await handleSave())) return;
    const queued = await state.queueExport();
    if (!queued) return;

    // O observador sobrevive à desmontagem só para atualizar os cards. O worker
    // confirma o resultado no backend antes de concluir o próprio job.
    watchJob(queued.job_id, (job) => {
      if (job.state === "done") {
        void refresh();
      }
    });
    if (next) onNavigate(next);
    else onBack();
    void refresh();
  }, [handleSave, refresh, video.video_id, onNavigate, onBack]);

  const handleNoBoom = useCallback(async () => {
    if (!confirm("Marcar este vídeo como SEM OBJETO? Os frames exportados serão apagados.")) return;
    const next = useLibrary.getState().nextPending(video.video_id);
    if (!(await useAnnotator.getState().markNoBoom())) return;
    if (next) onNavigate(next);
    else onBack();
    void refresh();
  }, [refresh, video.video_id, onNavigate, onBack]);

  const go = useCallback(
    (direction: "prev" | "next") => {
      const target = neighbours(video.video_id)[direction];
      if (target) onNavigate(target);
    },
    [neighbours, video.video_id, onNavigate],
  );

  useKeyboard({
    onSave: () => void handleSave(),
    onSaveExportNext: () => void handleSaveExportNext(),
    onNoBoom: () => void handleNoBoom(),
    onPrevVideo: () => go("prev"),
    onNextVideo: () => go("next"),
  });

  return (
    <div className="flex h-full flex-col">
      <header className="flex shrink-0 items-center gap-3 border-b border-zinc-800 px-3 py-2">
        <Button variant="ghost" onClick={onBack}>
          ← biblioteca
        </Button>

        <div className="flex min-w-0 items-baseline gap-2">
          <span className="truncate text-sm text-zinc-100">{video.name}</span>
          {dirty && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400" title="não salvo" />}
          <span
            className={cx(
              "shrink-0 text-[10px]",
              status === "done" ? "text-emerald-500" : status === "no_boom" ? "text-zinc-500" : "text-amber-500",
            )}
          >
            {status}
          </span>
        </div>

        <div className="flex items-center gap-1">
          <Button variant="ghost" className="px-2" onClick={() => go("prev")} title="vídeo anterior (PageUp)">
            ‹
          </Button>
          <Button variant="ghost" className="px-2" onClick={() => go("next")} title="próximo vídeo (PageDown)">
            ›
          </Button>
        </div>

        <div className="flex-1" />

        {meta && !playable && (
          <span
            className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400"
            title={`${meta.media.codec.toUpperCase()} não toca no navegador — a reprodução usa os frames extraídos, que são exatos ao frame`}
          >
            {meta.media.codec} · frames
          </span>
        )}
        {meta?.media.is_vfr_suspect && (
          <span
            className="rounded bg-amber-950 px-1.5 py-0.5 text-[10px] text-amber-400"
            title="Taxa de quadros variável: o relógio do player não é confiável neste arquivo. Use o filmstrip."
          >
            VFR
          </span>
        )}
        {meta && !meta.media.frame_count_exact && (
          <span
            className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400"
            title={`contagem de frames por ${meta.media.frame_count_source} (estimativa)`}
          >
            ~{frameCount}f
          </span>
        )}

        <Button variant="ghost" onClick={() => useAnnotator.getState().toggleHelp()} kbd="?">
          atalhos
        </Button>
      </header>

      <div className="flex min-h-0 flex-1">
        <main className="flex min-w-0 flex-1 flex-col">
          <div className="relative min-h-0 flex-1 bg-black">
            {loading ? (
              <div className="grid h-full place-items-center">
                <Spinner className="text-zinc-600" />
              </div>
            ) : nativePlayer ? (
              <VideoPlayer videoId={video.video_id} />
            ) : (
              <>
                <FramePreview videoId={video.video_id} />
                <StageControls />
              </>
            )}

            {job && (
              <div className="absolute bottom-3 left-3 w-64 rounded-md border border-zinc-800 bg-zinc-900/95 p-2.5">
                <div className="mb-1.5 flex items-center justify-between text-[11px]">
                  <span className="text-zinc-300">{job.message || job.kind}</span>
                  <span className="tnum text-zinc-500">
                    {job.total ? `${Math.round(job.progress * 100)}%` : ""}
                  </span>
                </div>
                <div className="h-1 overflow-hidden rounded-full bg-zinc-800">
                  <div
                    className="h-full bg-emerald-600 transition-[width]"
                    style={{ width: `${job.progress * 100}%` }}
                  />
                </div>
                <div className="mt-1.5 flex items-center justify-between">
                  <span className="tnum text-[10px] text-zinc-600">
                    {job.current}/{job.total}
                  </span>
                  <button
                    onClick={() => void api.cancelJob(job.job_id)}
                    className="text-[10px] text-zinc-500 hover:text-red-400"
                  >
                    cancelar
                  </button>
                </div>
              </div>
            )}
          </div>

          <div className="flex shrink-0 items-center gap-3 border-t border-zinc-800 bg-zinc-900 px-3 py-1.5">
            <div className="flex items-center gap-1">
              {(["scan", "refine", "bbox"] as const).map((value) => (
                <button
                  key={value}
                  onClick={() => setPhase(value)}
                  className={cx(
                    "rounded px-2 py-0.5 text-[11px] transition-colors",
                    phase === value
                      ? "bg-zinc-700 text-zinc-100"
                      : "text-zinc-500 hover:text-zinc-300",
                  )}
                >
                  {value === "scan" ? "assistir" : value === "refine" ? "refinar" : "bbox"}
                </button>
              ))}
            </div>

            <span className="tnum text-xs text-zinc-300">
              frame{" "}
              <span className={cx(frameProvisional && "text-amber-400")}>
                {frameProvisional && "~"}
                {frame}
              </span>
              <span className="text-zinc-600"> / {frameCount ? frameCount - 1 : "—"}</span>
            </span>

            <span className="tnum text-xs text-zinc-500">{timecode(seconds)}</span>

            {frameProvisional && (
              <span className="text-[10px] text-amber-500">
                estimativa — confirme no filmstrip
              </span>
            )}

            <div className="flex-1" />

            <Button
              variant="ghost"
              className="px-2 py-0.5 text-xs"
              onClick={() => useAnnotator.getState().setIn(frame, frameProvisional)}
              kbd="I"
            >
              novo intervalo aqui
            </Button>
            <Button
              variant="ghost"
              className="px-2 py-0.5 text-xs"
              onClick={() => useAnnotator.getState().setOut(frame, frameProvisional)}
              kbd="O"
            >
              fim
            </Button>
          </div>

          <Timeline />
          <Filmstrip videoId={video.video_id} />
        </main>

        <aside className="flex w-80 shrink-0 flex-col gap-3 overflow-y-auto border-l border-zinc-800 p-3">
          <IntervalList />
          <BboxPanel />
          <FlagsPanel />

          <Panel title="Observações do vídeo">
            <textarea
              aria-label="Observações do vídeo"
              value={videoNotes}
              onChange={(event) => setVideoNotes(event.target.value)}
              rows={2}
              className="w-full resize-none rounded border border-zinc-800 bg-zinc-950 px-2 py-1 text-xs text-zinc-200"
            />
          </Panel>

          {error && (
            <p className="rounded border border-red-900/60 bg-red-950/40 p-2 text-xs whitespace-pre-wrap text-red-300">
              {error}
            </p>
          )}

          {blockers.length > 0 && (
            <div className="rounded border border-amber-900/50 bg-amber-950/25 p-2">
              <p className="mb-1 text-[11px] text-amber-400">falta para exportar:</p>
              <ul className="space-y-0.5 text-[11px] text-amber-200/70">
                {blockers.slice(0, 6).map((item) => (
                  <li key={item}>· {item}</li>
                ))}
              </ul>
            </div>
          )}

          <div className="mt-auto space-y-2 pt-2">
            <div className="flex gap-2">
              <Button variant="danger" className="flex-1 justify-center" onClick={() => void handleNoBoom()} disabled={saving} kbd="X">
                {saving && <Spinner />} Sem objeto
              </Button>
              <Button
                className="flex-1 justify-center"
                onClick={() => void handleSave()}
                disabled={saving}
                kbd="Ctrl+S"
              >
                {saving && <Spinner />} salvar
              </Button>
            </div>
            <Button
              variant="primary"
              className="w-full justify-center"
              onClick={() => void handleSaveExportNext()}
              disabled={saving || blockers.length > 0}
              kbd="Ctrl+↵"
            >
              Salvar, enviar ao SAM3 e próximo
            </Button>
            <button
              onClick={() => {
                const next = nextPending(video.video_id);
                if (next) onNavigate(next);
              }}
              className="w-full text-center text-[11px] text-zinc-600 hover:text-zinc-300"
            >
              pular para o próximo pendente
            </button>
          </div>
        </aside>
      </div>

      <ShortcutsHelp />
    </div>
  );
}
