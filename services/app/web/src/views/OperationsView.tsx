import { useEffect, useMemo, useState } from "react";
import type { PipelineStage, VideoListItem } from "../api/types";
import { PipelineVideoCard } from "../components/PipelineVideoCard";
import { Spinner } from "../components/ui";
import type { OperationsStage } from "../lib/routes";
import { navigate } from "../lib/routes";
import { useLibrary } from "../store/library";
import { useSession } from "../store/session";

const STAGE_COPY: Record<Exclude<OperationsStage, "overview">, { title: string; description: string }> = {
  triage: { title: "Triagem de vídeos", description: "Localize o objeto e marque os intervalos úteis." },
  discarded: { title: "Sem objeto", description: "Vídeos descartados durante a triagem." },
  sam3: { title: "Propagação SAM3", description: "Configure a caixa inicial, acompanhe a fila e recupere falhas." },
  review: { title: "Revisão de máscaras", description: "Aprove ou corrija as máscaras geradas frame a frame." },
  completed: { title: "Concluídos", description: "Somente vídeos com todos os frames SAM3 validados." },
};

const FILTERS: Record<PipelineStage, { id: string; label: string }[]> = {
  triage: [
    { id: "all", label: "Todos" },
    { id: "pending", label: "Pendentes" },
    { id: "in_progress", label: "Em andamento" },
    { id: "discarded", label: "Sem objeto" },
  ],
  sam3: [
    { id: "all", label: "Todos" },
    { id: "ready", label: "Prontos" },
    { id: "queued", label: "Na fila" },
    { id: "running", label: "Processando" },
    { id: "error", label: "Com erro" },
    { id: "cancelled", label: "Cancelados" },
  ],
  review: [
    { id: "all", label: "Todos" },
    { id: "waiting", label: "Aguardando" },
    { id: "in_progress", label: "Em andamento" },
    { id: "inconsistent", label: "Com inconsistência" },
  ],
  completed: [{ id: "all", label: "Validados" }],
  discarded: [{ id: "all", label: "Sem objeto" }],
};

export function OperationsView({ stage }: { stage: Exclude<OperationsStage, "overview"> }) {
  const objectId = useSession((state) => state.activeObject?.object_id);
  const videos = useLibrary((state) => state.videos);
  const loading = useLibrary((state) => state.loading);
  const error = useLibrary((state) => state.error);
  const nextPending = useLibrary((state) => state.nextPending);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const boardStage: PipelineStage = stage;

  useEffect(() => {
    setFilter("all");
  }, [objectId, stage]);

  const items = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    return videos.filter((video) => {
      const onStage = stage === "triage"
        ? video.pipeline_stage === "triage" || video.pipeline_stage === "discarded"
        : video.pipeline_stage === stage;
      if (!onStage) return false;
      if (filter !== "all" && (filter === "discarded" ? video.pipeline_stage !== "discarded" : video.stage_status !== filter)) return false;
      return !needle || video.relpath.toLocaleLowerCase().includes(needle);
    });
  }, [videos, stage, filter, search]);

  const openEditor = (video: VideoListItem, editor: "triage" | "sam3" | "review") => {
    if (!objectId) return;
    navigate({ page: "editor", objectId, videoId: video.video_id, editor });
  };

  const primary = (video: VideoListItem) => {
    if (stage === "triage" || stage === "discarded") openEditor(video, "triage");
    else if (stage === "sam3") openEditor(video, "sam3");
    else openEditor(video, "review");
  };

  const copy = STAGE_COPY[stage];
  return (
    <div className="flex h-full flex-col overflow-hidden">
      <section className="shrink-0 border-b border-zinc-800 bg-zinc-950/60 px-5 py-5 lg:px-7">
        <div className="flex flex-wrap items-end gap-4">
          <div>
            <p className="mb-1 text-[11px] font-semibold uppercase tracking-[0.16em] text-emerald-500">Área operacional</p>
            <h1 className="text-2xl font-semibold tracking-tight text-zinc-50">{copy.title}</h1>
            <p className="mt-1 text-sm text-zinc-400">{copy.description}</p>
          </div>
          <div className="flex-1" />
          {stage === "triage" && (
            <button
              type="button"
              onClick={() => {
                const next = nextPending();
                if (next) openEditor(next, "triage");
              }}
              className="rounded-lg bg-emerald-500 px-4 py-2 text-sm font-semibold text-zinc-950 hover:bg-emerald-400"
            >
              Iniciar próximo
            </button>
          )}
        </div>

        <div className="mt-5 flex flex-wrap items-center gap-2">
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Buscar vídeo…"
            aria-label="Buscar vídeo"
            className="mr-2 w-56 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs text-zinc-200 placeholder:text-zinc-400"
          />
          {FILTERS[boardStage].map((item) => {
            const count = videos.filter((video) => {
              const onStage = stage === "triage" ? ["triage", "discarded"].includes(video.pipeline_stage) : video.pipeline_stage === stage;
              if (!onStage) return false;
              if (item.id === "all") return true;
              return item.id === "discarded" ? video.pipeline_stage === "discarded" : video.stage_status === item.id;
            }).length;
            return (
              <button
                type="button"
                key={item.id}
                onClick={() => setFilter(item.id)}
                aria-pressed={filter === item.id}
                className={`rounded-full border px-3 py-1.5 text-xs ${filter === item.id ? "border-emerald-700 bg-emerald-950/50 text-emerald-300" : "border-zinc-800 text-zinc-400 hover:text-zinc-200"}`}
              >
                {item.label} <span className="tnum ml-1 text-[10px] opacity-70">{count}</span>
              </button>
            );
          })}
        </div>
      </section>

      <section className="min-h-0 flex-1 overflow-y-auto p-5 lg:p-7" aria-live="polite">
        {error && <p className="mb-4 rounded-lg border border-red-900 bg-red-950/30 p-3 text-sm text-red-300">{error}</p>}
        {loading && videos.length === 0 ? (
          <p className="flex items-center gap-2 text-sm text-zinc-400"><Spinner /> Carregando vídeos…</p>
        ) : items.length === 0 ? (
          <div className="grid min-h-56 place-items-center rounded-xl border border-dashed border-zinc-800 text-center">
            <div><p className="text-sm font-medium text-zinc-300">Nenhum vídeo nesta fila</p><p className="mt-1 text-xs text-zinc-400">A etapa é atualizada automaticamente conforme o trabalho avança.</p></div>
          </div>
        ) : (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-4">
            {items.map((video) => (
              <PipelineVideoCard key={video.video_id} video={video} stage={video.pipeline_stage} onPrimary={primary} onDetails={primary} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
