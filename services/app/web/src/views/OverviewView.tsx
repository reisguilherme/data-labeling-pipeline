import { useMemo } from "react";
import type { PipelineStage, VideoListItem } from "../api/types";
import { navigate } from "../lib/routes";
import { useLibrary } from "../store/library";
import { useSession } from "../store/session";

const CARDS: { stage: PipelineStage; label: string; description: string; tone: string }[] = [
  { stage: "triage", label: "Triagem", description: "Pendentes e em andamento", tone: "text-sky-300" },
  { stage: "sam3", label: "SAM3", description: "Prontos, processando e falhas", tone: "text-violet-300" },
  { stage: "review", label: "Revisão", description: "Máscaras aguardando validação", tone: "text-amber-300" },
  { stage: "completed", label: "Concluídos", description: "Integralmente revisados", tone: "text-emerald-300" },
];

export function nextOperationalVideo(
  videos: VideoListItem[],
  currentUser?: string,
): { video: VideoListItem; editor: "triage" | "sam3" | "review" } | null {
  const mine = videos.find(
    (video) => video.lock?.user === currentUser && video.pipeline_stage === "triage" && video.stage_status === "in_progress",
  );
  if (mine) return { video: mine, editor: "triage" };
  const review = videos.find((video) => video.pipeline_stage === "review" && video.stage_status === "in_progress")
    ?? videos.find((video) => video.pipeline_stage === "review");
  if (review) return { video: review, editor: "review" };
  const sam3 = videos.find((video) => video.pipeline_stage === "sam3" && ["error", "invalid", "ready", "cancelled"].includes(video.stage_status));
  if (sam3) return { video: sam3, editor: "sam3" };
  const triage = videos.find((video) => video.pipeline_stage === "triage" && !video.lock)
    ?? videos.find((video) => video.pipeline_stage === "triage");
  return triage ? { video: triage, editor: "triage" } : null;
}

export function OverviewView() {
  const objectId = useSession((state) => state.activeObject?.object_id);
  const user = useSession((state) => state.user);
  const videos = useLibrary((state) => state.videos);
  const counts = useLibrary((state) => state.pipelineCounts);

  const next = useMemo(() => nextOperationalVideo(videos, user?.display_name), [videos, user?.display_name]);

  if (!objectId) return null;
  return (
    <div className="h-full overflow-y-auto p-5 lg:p-8">
      <div className="mx-auto max-w-6xl">
        <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-emerald-500">Operação</p>
        <div className="mt-1 flex flex-wrap items-end gap-4">
          <div>
            <h1 className="text-3xl font-semibold tracking-tight text-zinc-50">Visão operacional</h1>
            <p className="mt-2 text-sm text-zinc-400">Cada vídeo aparece na etapa que realmente precisa de atenção.</p>
          </div>
          <div className="flex-1" />
          <button
            type="button"
            disabled={!next}
            onClick={() => next && navigate({ page: "editor", objectId, videoId: next.video.video_id, editor: next.editor })}
            className="rounded-lg bg-emerald-500 px-4 py-2.5 text-sm font-semibold text-zinc-950 hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Continuar próximo trabalho
          </button>
        </div>

        <section aria-label="Etapas do pipeline" className="mt-8 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {CARDS.map((card) => (
            <button
              type="button"
              key={card.stage}
              onClick={() => navigate({ page: "operations", objectId, stage: card.stage })}
              className="group rounded-xl border border-zinc-800 bg-zinc-900/50 p-5 text-left hover:border-zinc-600 hover:bg-zinc-900"
            >
              <span className="flex items-start justify-between gap-4">
                <span className={`text-sm font-semibold ${card.tone}`}>{card.label}</span>
                <span className="tnum text-3xl font-semibold text-zinc-100">{counts[card.stage] ?? 0}</span>
              </span>
              <span className="mt-8 block text-xs text-zinc-400">{card.description}</span>
              <span className="mt-3 block text-xs font-medium text-zinc-400 group-hover:text-zinc-100">Abrir fila →</span>
            </button>
          ))}
        </section>

        <section className="mt-6 grid gap-4 lg:grid-cols-3">
          <div className="rounded-xl border border-zinc-800 bg-zinc-900/30 p-5 lg:col-span-2">
            <h2 className="text-sm font-medium text-zinc-200">Regra de conclusão</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-400">
              Um vídeo só entra em Concluídos depois que todas as máscaras SAM3 esperadas existem, são válidas e cada frame foi aprovado ou editado.
            </p>
          </div>
          <div className="rounded-xl border border-zinc-800 bg-zinc-900/30 p-5">
            <p className="text-xs text-zinc-400">Sem objeto</p>
            <p className="tnum mt-2 text-2xl font-semibold text-zinc-300">{counts.discarded ?? 0}</p>
            <p className="mt-2 text-xs text-zinc-400">Descartados não contam como concluídos.</p>
          </div>
        </section>
      </div>
    </div>
  );
}
