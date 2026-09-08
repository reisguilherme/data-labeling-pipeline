import { memo } from "react";
import type { Sam3Info, VideoListItem } from "../api/types";
import { cx, formatDuration } from "../lib/format";
import { CheckBadge } from "./ui";

const STATUS_LABEL: Record<string, string> = {
  pending: "pendente",
  in_progress: "em andamento",
  done: "concluído",
  // O status continua "no_boom" no arquivo (anotações antigas dependem disso),
  // mas o texto é genérico porque a ferramenta agora serve qualquer objeto.
  no_boom: "sem o objeto",
};

/** Estado da anotação automática, no canto inferior esquerdo da thumb. */
function Sam3Badge({ info }: { info: Sam3Info }) {
  const done = info.progress?.segments_done ?? 0;
  const total = info.progress?.segments_total ?? info.segments ?? 0;
  const percent = total > 0 ? Math.round((done / total) * 100) : 0;

  const style: Record<Sam3Info["state"], { text: string; className: string }> = {
    queued: { text: "SAM3 na fila", className: "bg-zinc-900/90 text-zinc-300" },
    leased: { text: "SAM3 iniciando", className: "bg-violet-950/90 text-violet-300" },
    running: {
      text: total > 1 ? `SAM3 ${done}/${total}` : `SAM3 ${percent}%`,
      className: "bg-violet-950/90 text-violet-300",
    },
    done: { text: "SAM3 pronto", className: "bg-violet-900/90 text-violet-200" },
    error: { text: "SAM3 falhou", className: "bg-red-950/90 text-red-300" },
    cancelled: { text: "SAM3 cancelado", className: "bg-zinc-900/90 text-zinc-500" },
  };
  const { text, className } = style[info.state] ?? style.queued;

  return (
    <span
      // O erro completo no title: um selo de 10px não comporta a mensagem, mas
      // esconder a causa transforma "falhou" num beco sem saída.
      title={info.error ?? undefined}
      className={cx(
        "tnum absolute bottom-1.5 left-1.5 max-w-[calc(100%-4rem)] truncate rounded px-1.5 py-0.5 text-[10px]",
        className,
      )}
    >
      {text}
      {info.attempts > 1 && info.state !== "done" && ` (${info.attempts}ª)`}
    </span>
  );
}

function VideoCardImpl({
  video,
  onOpen,
  onReview,
  onSam3,
}: {
  video: VideoListItem;
  onOpen: (video: VideoListItem) => void;
  onReview?: (video: VideoListItem) => void;
  onSam3?: (video: VideoListItem) => void;
}) {
  const finished = video.status === "done" || video.status === "no_boom";
  const locked = video.lock != null;
  // Cada etapa só aparece quando a anterior deixou algo para ela fazer: sem
  // export não há o que segmentar, sem propagação não há o que revisar.
  const podeSam3 = onSam3 && video.status === "done";
  const podeRevisar = onReview && video.sam3?.state === "done";

  return (
    <button
      onClick={() => onOpen(video)}
      // Travado abre em LEITURA, não fica morto: ver o que a outra pessoa está
      // marcando é útil; um card que não responde ao clique só parece quebrado.
      title={locked ? `${video.lock!.user} está triando — abre em leitura` : video.relpath}
      className={cx(
        "group relative flex flex-col overflow-hidden rounded-md border text-left transition-colors",
        locked
          ? "border-amber-900/70 bg-zinc-900/50 hover:border-amber-700"
          : "border-zinc-800 bg-zinc-900/50 hover:border-zinc-600",
        // Off-screen não custa layout: essencial com centenas de cards.
        "[content-visibility:auto] [contain-intrinsic-size:auto_190px]",
      )}
    >
      <div className="relative aspect-video overflow-hidden bg-zinc-950">
        <img
          src={video.thumb_url}
          alt=""
          loading="lazy"
          decoding="async"
          className={cx(
            "h-full w-full object-cover transition-all duration-150",
            // O blur sinaliza "já tratado" — mas só na thumb, nunca no nome.
            finished
              ? "opacity-60 blur-[2px] grayscale-[30%] group-hover:blur-none group-hover:opacity-90"
              : "group-hover:scale-[1.02]",
            locked && "opacity-45",
          )}
        />

        {locked && (
          <span className="absolute inset-x-1.5 top-1.5 truncate rounded bg-amber-950/90 px-1.5 py-0.5 text-[10px] text-amber-300">
            {video.lock!.user} está triando
          </span>
        )}

        {finished && (
          <CheckBadge
            className={cx(
              "absolute top-2 right-2",
              video.status === "no_boom" && "bg-zinc-600",
            )}
          />
        )}

        {video.missing && (
          <span className="absolute top-2 left-2 rounded bg-red-950/90 px-1.5 py-0.5 text-[10px] text-red-300">
            arquivo sumiu
          </span>
        )}

        {video.duration_sec != null && (
          <span className="tnum absolute right-1.5 bottom-1.5 rounded bg-zinc-950/85 px-1.5 py-0.5 text-[10px] text-zinc-300">
            {formatDuration(video.duration_sec)}
          </span>
        )}

        {/* Canto inferior ESQUERDO: a trava ocupa o topo e a duração o canto
            inferior direito, então este é o único lugar livre. */}
        {video.sam3 && <Sam3Badge info={video.sam3} />}

        {/* As etapas seguintes, no hover: o card tem um clique só, e abrir o
            SAM3 ou a revisão são ações diferentes de abrir a triagem. */}
        {(podeSam3 || podeRevisar) && (
          <span className="absolute inset-x-0 bottom-0 hidden group-hover:flex">
            {podeSam3 && (
              <span
                role="button"
                tabIndex={0}
                onClick={(event) => {
                  event.stopPropagation();
                  onSam3!(video);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.stopPropagation();
                    onSam3!(video);
                  }
                }}
                className="flex-1 bg-sky-900/90 py-1 text-center text-[11px] font-medium text-sky-100 hover:bg-sky-800"
              >
                SAM3
              </span>
            )}
            {podeRevisar && (
              <span
                role="button"
                tabIndex={0}
                onClick={(event) => {
                  event.stopPropagation();
                  onReview!(video);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.stopPropagation();
                    onReview!(video);
                  }
                }}
                className="flex-1 bg-violet-900/90 py-1 text-center text-[11px] font-medium text-violet-100 hover:bg-violet-800"
              >
                revisar
              </span>
            )}
          </span>
        )}
      </div>

      <div className="flex items-center justify-between gap-2 px-2.5 py-2">
        <span className="truncate text-sm text-zinc-200">{video.name}</span>
        <span
          className={cx(
            "tnum shrink-0 text-[10px]",
            video.status === "done"
              ? "text-emerald-500"
              : video.status === "no_boom"
                ? "text-zinc-500"
                : video.status === "in_progress"
                  ? "text-amber-500"
                  : "text-zinc-600",
          )}
        >
          {video.status === "done" && video.interval_count > 0
            ? `${video.interval_count} ${video.interval_count === 1 ? "trecho" : "trechos"}`
            : STATUS_LABEL[video.status]}
        </span>
      </div>
    </button>
  );
}

export const VideoCard = memo(VideoCardImpl);
