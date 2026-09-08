import { useEffect, useMemo } from "react";
import type { VideoListItem } from "../api/types";
import { StageNav } from "../components/StageNav";
import { VideoCard } from "../components/VideoCard";
import { Button, Chip, Spinner } from "../components/ui";
import { filterVideos, useLibrary } from "../store/library";
import { useSession } from "../store/session";

const FILTERS = [
  { id: "all", label: "Todos", tone: "zinc" },
  { id: "pending", label: "Pendentes", tone: "zinc" },
  { id: "in_progress", label: "Em andamento", tone: "amber" },
  { id: "done", label: "Concluídos", tone: "emerald" },
  { id: "no_boom", label: "Sem o objeto", tone: "zinc" },
] as const;

export function LibraryView({
  onOpen,
  onReview,
  onSam3,
  onChangeObject,
  onIngest,
  onDataset,
  active,
}: {
  onOpen: (video: VideoListItem) => void;
  onReview: (video: VideoListItem) => void;
  onSam3: (video: VideoListItem) => void;
  onChangeObject: () => void;
  onIngest: () => void;
  onDataset: () => void;
  active: VideoListItem | null;
}) {
  const activeObject = useSession((s) => s.activeObject);
  const user = useSession((s) => s.user);
  const {
    counts,
    loading,
    scanning,
    error,
    search,
    statusFilter,
    sort,
    setSearch,
    setStatusFilter,
    setSort,
    refresh,
    rescan,
    nextPending,
  } = useLibrary();

  const videos = useLibrary((s) => s.videos);
  const items = useMemo(
    () => filterVideos(videos, search, statusFilter),
    [videos, search, statusFilter],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Acompanha a fila do SAM3 enquanto houver job em voo, e só então. Parar
  // quando não há nada é o que impede um poll eterno numa biblioteca parada.
  const refreshSam3 = useLibrary((s) => s.refreshSam3);
  const inFlight = useMemo(
    () => videos.some((video) => video.sam3 && ["queued", "leased", "running"].includes(video.sam3.state)),
    [videos],
  );
  useEffect(() => {
    if (!inFlight) return;
    const timer = setInterval(() => void refreshSam3(), 5000);
    return () => clearInterval(timer);
  }, [inFlight, refreshSam3]);

  const done = (counts.done ?? 0) + (counts.no_boom ?? 0);
  const total = counts.total ?? 0;
  const progress = total ? (done / total) * 100 : 0;

  return (
    <div className="flex h-full flex-col">
      <header className="flex shrink-0 items-center gap-4 border-b border-zinc-800 px-4 py-2.5">
        <button
          onClick={onChangeObject}
          className="text-sm font-medium text-zinc-100 hover:text-emerald-400"
          title="trocar de objeto"
        >
          {activeObject?.display_name ?? "Triagem de vídeos"}
          <span className="ml-1.5 text-xs text-zinc-600">▾</span>
        </button>

        <div className="flex items-center gap-2">
          <div className="h-1.5 w-32 overflow-hidden rounded-full bg-zinc-800">
            <div
              className="h-full rounded-full bg-emerald-600 transition-[width] duration-300"
              style={{ width: `${progress}%` }}
            />
          </div>
          <span className="tnum text-xs text-zinc-500">
            {done}/{total} concluídos
          </span>
        </div>

        <StageNav
          active="library"
          video={active}
          onGo={(stage) => {
            if (stage === "sam3" && active) onSam3(active);
            else if (stage === "review" && active) onReview(active);
            else if (stage === "dataset") onDataset();
          }}
        />

        <div className="flex-1" />

        <span
          className="max-w-sm truncate text-xs text-zinc-600"
          title={`${activeObject?.videos_root} → ${activeObject?.output_root}`}
        >
          {activeObject?.videos_root}
        </span>

        <Button variant="ghost" onClick={onIngest}>
          ↓ baixar vídeos
        </Button>

        <Button variant="ghost" onClick={() => void rescan()} disabled={scanning}>
          {scanning ? <Spinner /> : "↻"} revarrer
        </Button>

        {user && (
          <span
            className="flex items-center gap-1.5 text-xs text-zinc-500"
            title={`você está logado como ${user.display_name}`}
          >
            <span
              className="h-2 w-2 rounded-full"
              style={{ background: user.color }}
            />
            {user.display_name}
          </span>
        )}
      </header>

      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-zinc-800 px-4 py-2">
        <input
          aria-label="Buscar vídeo por nome"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="buscar por nome…"
          className="w-56 rounded-md border border-zinc-800 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-200 placeholder:text-zinc-600"
        />

        {FILTERS.map((filter) => (
          <Chip
            key={filter.id}
            tone={filter.tone}
            active={statusFilter === filter.id}
            onClick={() => setStatusFilter(filter.id)}
            count={filter.id === "all" ? counts.total : counts[filter.id]}
          >
            {filter.label}
          </Chip>
        ))}

        <div className="flex-1" />

        <select
          aria-label="Ordenar vídeos"
          value={sort}
          onChange={(e) => setSort(e.target.value as never)}
          className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1 text-xs text-zinc-300"
        >
          <option value="name">nome</option>
          <option value="mtime">mais recentes</option>
          <option value="size">tamanho</option>
          <option value="status">status</option>
        </select>

        <Button
          variant="primary"
          onClick={() => {
            const next = nextPending();
            if (next) onOpen(next);
          }}
          disabled={!counts.pending && !counts.in_progress}
        >
          Próximo pendente
        </Button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {error && (
          <p className="mb-4 rounded-md border border-red-900/60 bg-red-950/40 p-3 text-sm text-red-300">
            {error}
          </p>
        )}

        {loading && items.length === 0 ? (
          <p className="flex items-center gap-2 text-sm text-zinc-500">
            <Spinner /> carregando…
          </p>
        ) : items.length === 0 ? (
          <p className="text-sm text-zinc-600">
            {total === 0
              ? "nenhum vídeo encontrado na pasta"
              : "nenhum vídeo bate com o filtro"}
          </p>
        ) : (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-3">
            {items.map((video) => (
              <VideoCard
                key={video.video_id}
                video={video}
                onOpen={onOpen}
                onReview={onReview}
                onSam3={onSam3}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
