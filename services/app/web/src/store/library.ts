import { create } from "zustand";
import { api } from "../api/client";
import { getObject } from "../api/scope";
import type { PipelineStage, Status, VideoListItem } from "../api/types";

type StatusFilter = Status | "all";
type Sort = "name" | "mtime" | "size" | "status";

/**
 * Filtro puro, fora da store.
 *
 * NÃO transforme isto num seletor de zustand: um seletor que devolve array novo a
 * cada chamada quebra o `useSyncExternalStore` do React ("getSnapshot should be
 * cached") e derruba a árvore inteira. Os componentes assinam os campos crus e
 * derivam com useMemo.
 */
export function filterVideos(
  videos: VideoListItem[],
  search: string,
  statusFilter: StatusFilter,
): VideoListItem[] {
  const needle = search.trim().toLowerCase();
  return videos.filter((video) => {
    if (statusFilter !== "all" && video.status !== statusFilter) return false;
    if (needle && !video.relpath.toLowerCase().includes(needle)) return false;
    return true;
  });
}

interface LibraryState {
  videos: VideoListItem[];
  counts: Record<string, number>;
  pipelineCounts: Partial<Record<PipelineStage, number>>;
  pipelineStatusCounts: Record<string, number>;
  label: string;
  loading: boolean;
  scanning: boolean;
  error: string | null;

  search: string;
  statusFilter: StatusFilter;
  sort: Sort;

  refresh: () => Promise<void>;
  refreshSam3: () => Promise<boolean>;
  rescan: () => Promise<void>;
  reset: () => void;
  setSearch: (value: string) => void;
  setStatusFilter: (value: StatusFilter) => void;
  setSort: (value: Sort) => void;

  visible: () => VideoListItem[];
  nextPending: (afterVideoId?: string) => VideoListItem | null;
  neighbours: (videoId: string) => { prev: VideoListItem | null; next: VideoListItem | null };
}

let scopeGeneration = 0;
let listingRequest = 0;

function requestFence(): { generation: number; objectId: string | null } {
  return { generation: scopeGeneration, objectId: getObject() };
}

function fenceIsCurrent(fence: { generation: number; objectId: string | null }): boolean {
  return fence.generation === scopeGeneration && fence.objectId === getObject();
}

const EMPTY_LIBRARY = {
  videos: [],
  counts: {},
  pipelineCounts: {},
  pipelineStatusCounts: {},
  label: "",
  loading: false,
  scanning: false,
  error: null,
} satisfies Partial<LibraryState>;

export const useLibrary = create<LibraryState>((set, get) => ({
  videos: [],
  counts: {},
  pipelineCounts: {},
  pipelineStatusCounts: {},
  label: "",
  loading: false,
  scanning: false,
  error: null,

  search: "",
  statusFilter: "all",
  sort: "name",

  refresh: async () => {
    const fence = requestFence();
    const request = ++listingRequest;
    set({ loading: true, error: null });
    try {
      const data = await api.videos({ sort: get().sort });
      if (!fenceIsCurrent(fence) || request !== listingRequest) return;
      set({
        videos: data.videos,
        counts: data.counts,
        pipelineCounts: data.pipeline_counts,
        pipelineStatusCounts: data.pipeline_status_counts,
        label: data.label,
      });
    } catch (error) {
      if (!fenceIsCurrent(fence) || request !== listingRequest) return;
      set({ error: (error as Error).message });
    } finally {
      if (fenceIsCurrent(fence) && request === listingRequest) set({ loading: false });
    }
  },

  /**
   * Atualiza SÓ o estado do SAM3, sem recarregar a lista inteira.
   *
   * Devolve `true` enquanto houver job em voo, que é o que o componente usa
   * para decidir se continua consultando. Um segundo canal SSE seria mais
   * maquinaria do que o problema pede: a fila muda a cada segundos, não a cada
   * frame.
   */
  refreshSam3: async () => {
    const fence = requestFence();
    try {
      const wasActive = get().videos.some(
        (video) => video.sam3 && ["queued", "leased", "running"].includes(video.sam3.state),
      );
      const data = await api.sam3Queue();
      if (!fenceIsCurrent(fence)) return false;
      set((state) => ({
        videos: state.videos.map((video) =>
          video.sam3 === (data.videos[video.video_id] ?? null)
            ? video
            : { ...video, sam3: data.videos[video.video_id] ?? null },
        ),
      }));
      if (wasActive && !data.active) await get().refresh();
      return data.active;
    } catch {
      return false;
    }
  },

  rescan: async () => {
    const fence = requestFence();
    const request = ++listingRequest;
    set({ scanning: true, error: null });
    try {
      const data = await api.rescan();
      if (!fenceIsCurrent(fence) || request !== listingRequest) return;
      set({
        videos: data.videos,
        counts: data.counts,
        pipelineCounts: data.pipeline_counts,
        pipelineStatusCounts: data.pipeline_status_counts,
        label: data.label,
      });
    } catch (error) {
      if (!fenceIsCurrent(fence) || request !== listingRequest) return;
      set({ error: (error as Error).message });
    } finally {
      if (fenceIsCurrent(fence) && request === listingRequest) set({ scanning: false });
    }
  },

  reset: () => {
    scopeGeneration += 1;
    listingRequest += 1;
    set({ ...EMPTY_LIBRARY, search: "", statusFilter: "all", sort: "name" });
  },

  setSearch: (search) => set({ search }),
  setStatusFilter: (statusFilter) => set({ statusFilter }),
  setSort: (sort) => {
    set({ sort });
    void get().refresh();
  },

  // Filtragem no cliente: com algumas centenas de itens é instantâneo e evita
  // um round-trip por tecla digitada na busca. Só para uso imperativo — na
  // renderização use `filterVideos` com useMemo.
  visible: () => {
    const { videos, search, statusFilter } = get();
    return filterVideos(videos, search, statusFilter);
  },

  nextPending: (afterVideoId) => {
    const list = get().videos;
    const start = afterVideoId ? list.findIndex((v) => v.video_id === afterVideoId) + 1 : 0;
    const ordered = [...list.slice(start), ...list.slice(0, start)];
    const pending = (video: VideoListItem) =>
      video.video_id !== afterVideoId &&
      (video.status === "pending" || video.status === "in_progress");
    // Pular o que já está com outra pessoa é o que impede duas pessoas caírem no
    // mesmo vídeo apertando o mesmo botão — o caso mais provável de colisão, já
    // que todo mundo entra pelo "próximo pendente".
    return (
      ordered.find((video) => pending(video) && !video.lock) ??
      // Só se não sobrou nada livre: melhor abrir em leitura que dizer "acabou".
      ordered.find(pending) ??
      null
    );
  },

  neighbours: (videoId) => {
    const list = get().visible();
    const at = list.findIndex((v) => v.video_id === videoId);
    if (at === -1) return { prev: null, next: null };
    return {
      prev: at > 0 ? list[at - 1] : null,
      next: at < list.length - 1 ? list[at + 1] : null,
    };
  },
}));
