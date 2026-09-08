import type { PipelineStage, VideoListItem } from "../api/types";
import { formatDuration } from "../lib/format";

const STATUS_LABELS: Record<string, string> = {
  pending: "Pendente",
  in_progress: "Em andamento",
  discarded: "Sem objeto",
  ready: "Pronto para propagar",
  queued: "Na fila",
  leased: "Iniciando",
  running: "Processando",
  error: "Com erro",
  cancelled: "Cancelado",
  invalid: "Precisa reprocessar",
  waiting: "Aguardando",
  inconsistent: "Com inconsistência",
  validated: "Validado",
};

function primaryLabel(video: VideoListItem, stage: PipelineStage): string {
  if (stage === "triage") return video.stage_status === "in_progress" ? "Continuar" : "Triar";
  if (stage === "sam3") {
    if (video.stage_status === "error" || video.stage_status === "cancelled" || video.stage_status === "invalid") return "Tentar novamente";
    if (["queued", "leased", "running"].includes(video.stage_status)) return "Acompanhar";
    return "Configurar e propagar";
  }
  if (stage === "review") return video.stage_progress.reviewed_frames > 0 ? "Continuar" : "Revisar";
  return "Consultar";
}

export function PipelineVideoCard({
  video,
  stage,
  onPrimary,
  onDetails,
}: {
  video: VideoListItem;
  stage: PipelineStage;
  onPrimary: (video: VideoListItem) => void;
  onDetails?: (video: VideoListItem) => void;
}) {
  const progress = video.stage_progress;
  const percent = progress.expected_frames > 0
    ? Math.round((progress.reviewed_frames / progress.expected_frames) * 100)
    : 0;
  return (
    <article className="flex flex-col overflow-hidden rounded-xl border border-zinc-800 bg-zinc-900/55 [content-visibility:auto] [contain-intrinsic-size:auto_260px]">
      <div className="relative aspect-video overflow-hidden bg-zinc-950">
        <img
          src={video.thumb_url}
          alt=""
          loading="lazy"
          decoding="async"
          className={`h-full w-full object-cover ${stage === "completed" || stage === "discarded" ? "opacity-60 blur-[2px]" : ""}`}
        />
        <span className="absolute top-2 left-2 rounded-md bg-zinc-950/90 px-2 py-1 text-[10px] font-medium text-zinc-200">
          {STATUS_LABELS[video.stage_status] ?? video.stage_status}
        </span>
        {video.duration_sec != null && (
          <span className="tnum absolute right-2 bottom-2 rounded bg-zinc-950/90 px-1.5 py-0.5 text-[10px] text-zinc-300">
            {formatDuration(video.duration_sec)}
          </span>
        )}
      </div>
      <div className="flex flex-1 flex-col gap-3 p-3">
        <div>
          <h3 className="truncate text-sm font-medium text-zinc-100" title={video.relpath}>{video.name}</h3>
          <p className="mt-1 text-xs text-zinc-400">
            {video.interval_count} {video.interval_count === 1 ? "trecho" : "trechos"}
            {video.lock && ` · ${video.lock.user} está triando`}
          </p>
        </div>

        {(stage === "review" || stage === "completed") && progress.expected_frames > 0 && (
          <div aria-label={`${progress.reviewed_frames} de ${progress.expected_frames} frames revisados`}>
            <div className="mb-1 flex justify-between text-[11px] text-zinc-400">
              <span>Revisão</span>
              <span className="tnum">{progress.reviewed_frames}/{progress.expected_frames}</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-zinc-800">
              <div className="h-full rounded-full bg-violet-500" style={{ width: `${percent}%` }} />
            </div>
          </div>
        )}

        {stage === "sam3" && video.sam3 && (
          <p className="text-xs text-zinc-400">
            {video.sam3.progress.segments_done ?? 0}/{video.sam3.progress.segments_total ?? video.sam3.segments} segmentos
            {video.sam3.progress.frames_done != null && ` · ${video.sam3.progress.frames_done} frames`}
            {` · tentativa ${video.sam3.attempts}`}
          </p>
        )}
        <div className="mt-auto flex gap-2">
          <button
            type="button"
            onClick={() => onPrimary(video)}
            className="flex-1 rounded-lg bg-emerald-500 px-3 py-2 text-xs font-semibold text-zinc-950 hover:bg-emerald-400"
          >
            {primaryLabel(video, stage)}
          </button>
          {onDetails && (
            <button type="button" onClick={() => onDetails(video)} className="rounded-lg border border-zinc-700 px-3 py-2 text-xs text-zinc-300 hover:bg-zinc-800">
              Detalhes
            </button>
          )}
        </div>
      </div>
    </article>
  );
}
