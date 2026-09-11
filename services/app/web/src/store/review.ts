import { create } from "zustand";
import { api } from "../api/client";
import type { BBox, ReviewFrame, ReviewSegment } from "../api/types";

/**
 * Estado da revisão de um segmento.
 *
 * O segmento inteiro é carregado de uma vez (poucos KB mesmo com 799 frames):
 * navegar frame a frame precisa ser instantâneo, e uma requisição por frame
 * tornaria a revisão de 9722 frames insuportável.
 *
 * A gravação é o inverso: um PUT por frame tocado, com debounce. Perder horas
 * de revisão por causa de um "salvar" esquecido não é aceitável, e o volume de
 * escrita é baixo (uma pessoa não edita mais de um frame por segundo).
 */

const SAVE_DEBOUNCE_MS = 400;

interface ReviewState {
  videoId: string | null;
  segment: string | null;
  segments: string[];
  name: string;
  label: string;
  exportVersion: string;
  frameCount: number;
  frames: ReviewFrame[];
  current: number;
  selectedBox: number;
  loading: boolean;
  saving: boolean;
  error: string | null;
  imageWidth: number;
  imageHeight: number;
  zoom: number;
  panX: number;
  panY: number;

  open: (videoId: string, segment: string) => Promise<void>;
  close: () => void;
  goto: (frame: number) => void;
  /** Avança confirmando o frame atual — o atalho que torna 9722 frames viável. */
  advance: (delta: number) => void;
  setBoxes: (boxes: BBox[]) => void;
  selectBox: (index: number) => void;
  clearFrame: () => void;
  resetFrame: () => Promise<void>;
  confirmRest: () => Promise<void>;
  setZoom: (zoom: number, focus?: { x: number; y: number }) => void;
  nudgePan: (dx: number, dy: number) => void;
  resetView: () => void;
  flush: () => Promise<void>;
}

const ZOOM_MIN = 1;
const ZOOM_MAX = 8;

/** Pendências de escrita, fora da store: são efeito, não estado renderizável. */
let saveTimer: ReturnType<typeof setTimeout> | null = null;
const pending = new Map<number, ReviewFrame>();
let openGeneration = 0;

export const useReview = create<ReviewState>((set, get) => {
  async function flushNow(): Promise<void> {
    if (saveTimer !== null) {
      clearTimeout(saveTimer);
      saveTimer = null;
    }
    const { videoId, segment, exportVersion } = get();
    if (!videoId || !segment || !exportVersion || pending.size === 0) return;

    const batch = [...pending.entries()];
    pending.clear();
    set({ saving: true });
    try {
      for (const [frame, entry] of batch) {
        await api.reviewFrame(videoId, segment, frame, {
          export_version: exportVersion,
          status: entry.status === "edited" ? "edited" : "ok",
          boxes: entry.boxes,
        });
      }
      set({ saving: false, error: null });
    } catch (error) {
      // Devolve para a fila: o próximo debounce tenta de novo, e o `flush` do
      // unmount tenta uma última vez. Descartar seria perder trabalho humano.
      for (const [frame, entry] of batch) pending.set(frame, entry);
      set({ saving: false, error: (error as Error).message });
    }
  }

  function queue(frame: number, entry: ReviewFrame): void {
    pending.set(frame, entry);
    if (saveTimer !== null) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => void flushNow(), SAVE_DEBOUNCE_MS);
  }

  function markCurrent(status: "ok" | "edited", boxes?: BBox[]): void {
    const { frames, current } = get();
    const existing = frames[current];
    if (!existing) return;
    // Confirmar não pode rebaixar uma edição para "ok": passar de novo por um
    // frame já corrigido tem de preservar a correção.
    const nextStatus = status === "ok" && existing.status === "edited" ? "edited" : status;
    const entry: ReviewFrame = {
      frame: current,
      status: nextStatus,
      boxes: boxes ?? existing.boxes,
    };
    const next = [...frames];
    next[current] = entry;
    set({ frames: next });
    queue(current, entry);
  }

  return {
    videoId: null,
    segment: null,
    segments: [],
    name: "",
    label: "",
    exportVersion: "",
    frameCount: 0,
    frames: [],
    current: 0,
    selectedBox: -1,
    loading: false,
    saving: false,
    error: null,
    imageWidth: 0,
    imageHeight: 0,
    zoom: 1,
    panX: 0,
    panY: 0,

    open: async (videoId, segment) => {
      const generation = ++openGeneration;
      await flushNow();
      if (generation !== openGeneration) return;
      set({ loading: true, error: null, videoId, segment, frames: [], current: 0 });
      try {
        const data: ReviewSegment = await api.segmentReview(videoId, segment);
        if (
          generation !== openGeneration
          || get().videoId !== videoId
          || get().segment !== segment
        ) return;
        // Primeiro frame ainda não revisado: retomar onde parou é o que torna
        // um segmento de 799 frames possível em mais de uma sessão.
        const resume = data.frames.findIndex((f) => !f.status);
        set({
          segments: data.segments,
          name: data.name,
          label: data.label,
          exportVersion: data.export_version,
          frameCount: data.frame_count,
          frames: data.frames,
          current: resume === -1 ? 0 : resume,
          selectedBox: -1,
          imageWidth: data.prompt?.image_width ?? 0,
          imageHeight: data.prompt?.image_height ?? 0,
          loading: false,
          zoom: 1,
          panX: 0,
          panY: 0,
        });
      } catch (error) {
        if (generation === openGeneration) {
          set({ loading: false, error: (error as Error).message });
        }
      }
    },

    close: () => {
      openGeneration += 1;
      void flushNow();
      set({ videoId: null, segment: null, exportVersion: "", frames: [], current: 0 });
    },

    goto: (frame) => {
      const { frameCount } = get();
      set({
        current: Math.max(0, Math.min(frame, frameCount - 1)),
        selectedBox: -1,
      });
    },

    advance: (delta) => {
      const { current, frameCount } = get();
      // Só avançar confirma. Voltar para conferir não pode marcar nada — senão
      // revisitar um trecho marcaria como conferido o que você ainda não viu.
      if (delta > 0) markCurrent("ok");
      const next = Math.max(0, Math.min(current + delta, frameCount - 1));
      set({ current: next, selectedBox: -1 });
    },

    setBoxes: (boxes) => {
      markCurrent("edited", boxes);
    },

    selectBox: (selectedBox) => set({ selectedBox }),

    clearFrame: () => {
      markCurrent("edited", []);
      set({ selectedBox: -1 });
    },

    resetFrame: async () => {
      const { videoId, segment, current, frames, exportVersion } = get();
      if (!videoId || !segment || !exportVersion) return;
      await flushNow();
      try {
        await api.reviewReset(videoId, segment, current, exportVersion);
        const data: ReviewSegment = await api.segmentReview(videoId, segment);
        set({
          frames: data.frames,
          exportVersion: data.export_version,
          selectedBox: -1,
        });
      } catch (error) {
        set({ error: (error as Error).message });
        set({ frames });
      }
    },

    confirmRest: async () => {
      const { videoId, segment, current, frameCount, exportVersion } = get();
      if (!videoId || !segment || !exportVersion) return;
      await flushNow();
      try {
        await api.reviewConfirm(
          videoId,
          segment,
          current,
          frameCount - 1,
          exportVersion,
        );
        const data: ReviewSegment = await api.segmentReview(videoId, segment);
        set({
          frames: data.frames,
          exportVersion: data.export_version,
          current: frameCount - 1,
        });
      } catch (error) {
        set({ error: (error as Error).message });
        throw error;
      }
    },

    setZoom: (zoom, focus) =>
      set((state) => {
        const next = Math.max(ZOOM_MIN, Math.min(zoom, ZOOM_MAX));
        if (next === 1) return { zoom: 1, panX: 0, panY: 0 };
        const target = focus ?? { x: 0.5, y: 0.5 };
        const ratio = 1 - state.zoom / next;
        const panX = state.panX + (target.x - 0.5 - state.panX) * ratio;
        const panY = state.panY + (target.y - 0.5 - state.panY) * ratio;
        const bound = 0.5 - 0.5 / next;
        return {
          zoom: next,
          panX: Math.max(-bound, Math.min(panX, bound)),
          panY: Math.max(-bound, Math.min(panY, bound)),
        };
      }),

    nudgePan: (dx, dy) =>
      set((state) => {
        const bound = 0.5 - 0.5 / state.zoom;
        return {
          panX: Math.max(-bound, Math.min(state.panX + dx, bound)),
          panY: Math.max(-bound, Math.min(state.panY + dy, bound)),
        };
      }),

    resetView: () => set({ zoom: 1, panX: 0, panY: 0 }),

    flush: flushNow,
  };
});

/** Contagens da barra de progresso. Puro e fora da store — ver library.ts. */
export function reviewCounts(frames: ReviewFrame[]): {
  reviewed: number;
  edited: number;
  empty: number;
} {
  let reviewed = 0;
  let edited = 0;
  let empty = 0;
  for (const frame of frames) {
    if (frame.status) reviewed += 1;
    if (frame.status === "edited") edited += 1;
    if (!frame.boxes.length) empty += 1;
  }
  return { reviewed, edited, empty };
}

export { ZOOM_MAX, ZOOM_MIN };
