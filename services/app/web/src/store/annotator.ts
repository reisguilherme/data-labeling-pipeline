import { create } from "zustand";
import { ApiError, api, watchJob, type IntervalPayload } from "../api/client";
import type {
  BBox,
  FlagValues,
  Interval,
  JobInfo,
  LockInfo,
  ProxyStatus,
  Status,
  VideoMeta,
} from "../api/types";

export type Phase = "scan" | "refine" | "bbox";

/**
 * Intervalo em edição.
 *
 * `startProvisional`/`endProvisional` marcam bordas que vieram da estimativa do
 * player de vídeo (currentTime × fps) e ainda NÃO foram confirmadas no filmstrip.
 * Salvar com qualquer borda provisória é bloqueado: só o filmstrip indexa um JPEG
 * realmente extraído pelo mesmo contador que o export usa.
 */
export interface DraftInterval {
  key: string;
  start: number | null;
  end: number | null;
  startProvisional: boolean;
  endProvisional: boolean;
  bboxes: BBox[];
  flags: FlagValues;
  notes: string;
}

let keySeed = 0;
const nextKey = () => `iv${++keySeed}`;

/** Ajuste de imagem — o material é log/flat, e reflexo em cena lavada some. */
export interface ImageAdjust {
  brightness: number;
  contrast: number;
  saturate: number;
}

export const NEUTRAL_ADJUST: ImageAdjust = { brightness: 1, contrast: 1, saturate: 1 };
export const BOOST_ADJUST: ImageAdjust = { brightness: 1.08, contrast: 1.45, saturate: 1.25 };

export const ZOOM_MIN = 1;
export const ZOOM_MAX = 8;

/**
 * O que ainda falta para poder exportar.
 *
 * Pura e fora da store pelo mesmo motivo de `filterVideos`: seletor que devolve
 * array novo a cada chamada quebra o `useSyncExternalStore` do React.
 */
export function computeBlockers(intervals: DraftInterval[]): string[] {
  const problems: string[] = [];

  if (intervals.length === 0) problems.push("nenhum intervalo marcado");

  intervals.forEach((interval, index) => {
    const tag = `intervalo ${index + 1}`;
    if (interval.start === null) problems.push(`${tag}: falta marcar o início`);
    if (interval.end === null) problems.push(`${tag}: falta marcar o fim`);
    if (interval.startProvisional)
      problems.push(`${tag}: início ainda é estimativa — confirme no filmstrip`);
    if (interval.endProvisional)
      problems.push(`${tag}: fim ainda é estimativa — confirme no filmstrip`);
    if (interval.bboxes.length === 0) problems.push(`${tag}: falta desenhar o bbox`);
    if (!interval.flags.dificuldade) problems.push(`${tag}: falta a dificuldade`);
  });

  return problems;
}

export function emptyInterval(start: number | null = null): DraftInterval {
  return {
    key: nextKey(),
    start,
    end: null,
    startProvisional: false,
    endProvisional: false,
    bboxes: [],
    flags: {},
    notes: "",
  };
}

/** Um intervalo já marcado de ponta a ponta — usado para sinalizar no palco. */
export function isMarked(interval: DraftInterval): boolean {
  return interval.start !== null && interval.end !== null;
}

export function frameInsideMarked(intervals: DraftInterval[], frame: number): boolean {
  return intervals.some(
    (interval) => isMarked(interval) && frame >= interval.start! && frame <= interval.end!,
  );
}

function fromServer(interval: Interval): DraftInterval {
  return {
    key: nextKey(),
    start: interval.start_frame,
    end: interval.end_frame,
    startProvisional: false,
    endProvisional: false,
    bboxes: interval.bboxes,
    flags: interval.flags,
    notes: interval.notes,
  };
}

interface AnnotatorState {
  videoId: string | null;
  meta: VideoMeta | null;
  proxy: ProxyStatus | null;
  job: JobInfo | null;
  loading: boolean;
  error: string | null;
  saving: boolean;
  dirty: boolean;

  phase: Phase;
  currentFrame: number;
  /** true enquanto o frame vem do player e não de um JPEG extraído. */
  frameProvisional: boolean;
  playing: boolean;
  playbackRate: number;

  intervals: DraftInterval[];
  selected: number;
  selectedBbox: number;
  status: Status;
  videoNotes: string;

  filmstripStride: number;
  showHelp: boolean;

  suggestedFlags: FlagValues;
  /** Rótulo do objeto aberto — vai em cada bbox salvo. */
  label: string;
  /** Preenchido quando o vídeo está com outra pessoa; a tela vira leitura. */
  lock: LockInfo | null;
  readOnly: boolean;
  zoom: number;
  panX: number;
  panY: number;
  adjust: ImageAdjust;

  open: (videoId: string) => Promise<void>;
  close: () => void;
  setPhase: (phase: Phase) => void;
  setFrame: (frame: number, provisional?: boolean) => void;
  stepFrame: (delta: number) => void;
  setPlaying: (playing: boolean) => void;
  setPlaybackRate: (rate: number) => void;
  setStride: (stride: number) => void;
  toggleHelp: (show?: boolean) => void;

  setZoom: (zoom: number, focus?: { x: number; y: number }) => void;
  nudgePan: (dx: number, dy: number) => void;
  resetView: () => void;
  setAdjust: (adjust: Partial<ImageAdjust>) => void;
  toggleBoost: () => void;

  addInterval: (start?: number) => void;
  removeInterval: (at: number) => void;
  selectInterval: (at: number) => void;
  setIn: (frame: number, provisional: boolean) => void;
  setOut: (frame: number, provisional: boolean) => void;
  patchInterval: (at: number, patch: Partial<DraftInterval>) => void;
  setFlag: (groupId: string, value: string | string[] | null) => void;

  setBboxes: (bboxes: BBox[]) => void;
  selectBbox: (at: number) => void;

  setStatus: (status: Status) => void;
  setVideoNotes: (notes: string) => void;

  frameCount: () => number;
  blockers: () => string[];
  save: () => Promise<boolean>;
  queueExport: () => Promise<{ job_id: string; total: number } | null>;
  markNoBoom: () => Promise<boolean>;
  ensureFrameAvailable: (frame: number) => Promise<FrameRecoveryOutcome>;
}

export interface FrameRecoveryOutcome {
  proxy: ProxyStatus;
  jobId: string | null;
}

function toPayload(intervals: DraftInterval[], label: string): IntervalPayload[] {
  return intervals
    .filter((interval) => interval.start !== null && interval.end !== null)
    .map((interval) => ({
      start_frame: interval.start!,
      end_frame: interval.end!,
      prompt_frame: interval.start!,
      bboxes: interval.bboxes.map((box, position) => ({
        obj_id: box.obj_id || position + 1,
        // O rótulo é do OBJETO aberto, não uma constante. O servidor reescreve
        // com o label dele de qualquer forma — isto só mantém o payload honesto.
        label: box.label || label,
        normalized: box.normalized,
      })),
      flags: interval.flags,
      notes: interval.notes,
    }));
}

/**
 * Renovação da trava.
 *
 * Vai junto com `setActiveVideo`, que a aba já chamaria de qualquer forma — uma
 * requisição a cada 30 s em vez de duas. O TTL no servidor é 90 s, ou seja, três
 * batidas perdidas antes de liberar: uma aba que travou não segura o vídeo de
 * ninguém por mais que isso.
 */
const HEARTBEAT_MS = 30_000;
let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
let beaconBound: string | null = null;
let openGeneration = 0;
let stopProxyWatcher: (() => void) | null = null;
let lifecycleTransition: Promise<void> = Promise.resolve();

function sequenceLifecycle<T>(operation: () => Promise<T>): Promise<T> {
  const result = lifecycleTransition.then(operation, operation);
  lifecycleTransition = result.then(
    () => undefined,
    () => undefined,
  );
  return result;
}

class FrameRecoveryCancelled extends Error {
  constructor() {
    super("recuperação de frame cancelada");
  }
}

interface FrameAvailabilityFlight {
  promise: Promise<FrameRecoveryOutcome>;
  cancel: () => void;
}

const frameAvailabilityFlights = new Map<string, FrameAvailabilityFlight>();

function cancelFrameAvailabilityFlights(): void {
  for (const flight of frameAvailabilityFlights.values()) flight.cancel();
  frameAvailabilityFlights.clear();
}

function stopHeartbeat(): void {
  if (heartbeatTimer !== null) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
  if (beaconBound) {
    window.removeEventListener("pagehide", releaseOnUnload);
    beaconBound = null;
  }
}

function releaseOnUnload(): void {
  if (beaconBound) api.releaseLockBeacon(beaconBound);
}

function startHeartbeat(videoId: string): void {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    void sequenceLifecycle(() => api.setActiveVideo(videoId)).catch(() => undefined);
  }, HEARTBEAT_MS);
  // `pagehide` em vez de `beforeunload`: dispara também quando a aba vai para o
  // cache de navegação do browser, e é o evento em que o sendBeacon ainda vale.
  beaconBound = videoId;
  window.addEventListener("pagehide", releaseOnUnload);
}

export const useAnnotator = create<AnnotatorState>((set, get) => ({
  videoId: null,
  meta: null,
  proxy: null,
  job: null,
  loading: false,
  error: null,
  saving: false,
  dirty: false,

  phase: "scan",
  currentFrame: 0,
  frameProvisional: false,
  playing: false,
  playbackRate: 1,

  intervals: [],
  selected: -1,
  selectedBbox: -1,
  status: "in_progress",
  videoNotes: "",

  filmstripStride: 1,
  showHelp: false,

  suggestedFlags: {},
  label: "",
  lock: null,
  readOnly: false,
  zoom: 1,
  panX: 0,
  panY: 0,
  // Ajuste de imagem sobrevive à troca de vídeo: é preferência de quem opera,
  // não propriedade do arquivo.
  adjust: { ...NEUTRAL_ADJUST },

  open: async (videoId) => {
    const generation = ++openGeneration;
    const isActive = () => openGeneration === generation && get().videoId === videoId;
    cancelFrameAvailabilityFlights();
    stopProxyWatcher?.();
    stopProxyWatcher = null;
    stopHeartbeat();
    set({
      videoId,
      loading: true,
      error: null,
      saving: false,
      meta: null,
      proxy: null,
      job: null,
      phase: "scan",
      currentFrame: 0,
      frameProvisional: false,
      playing: false,
      playbackRate: 1,
      intervals: [],
      selected: -1,
      selectedBbox: -1,
      dirty: false,
      filmstripStride: 1,
      suggestedFlags: {},
      lock: null,
      readOnly: false,
      zoom: 1,
      panX: 0,
      panY: 0,
    });

    try {
      // Cancela extrações do vídeo anterior antes de disputar o semáforo pesado.
      await sequenceLifecycle(() => api.setActiveVideo(videoId));
      if (!isActive()) return;

      // A trava é pedida ANTES de carregar: se outra pessoa está com o vídeo, a
      // tela abre em leitura desde o primeiro render, em vez de deixar alguém
      // marcar dez intervalos para só então descobrir que não pode salvar.
      try {
        await sequenceLifecycle(() => api.acquireLock(videoId));
        if (!isActive()) {
          return;
        }
        startHeartbeat(videoId);
      } catch (error) {
        const lock = error instanceof ApiError ? error.lock : null;
        if (!isActive()) return;
        if (lock) set({ lock, readOnly: true });
        else throw error;
      }

      const [meta, entry] = await Promise.all([
        api.meta(videoId),
        api.annotation(videoId),
      ]);

      const intervals = (entry.intervals ?? []).map(fromServer);
      if (!isActive()) return;
      set({
        meta,
        label: meta.label,
        intervals,
        selected: intervals.length ? 0 : -1,
        status: entry.status === "pending" ? "in_progress" : entry.status,
        videoNotes: entry.notes ?? "",
        suggestedFlags: entry.suggested_flags ?? {},
      });

      const started = await api.startProxy(videoId);
      if (!isActive()) return;
      const status = await api.proxyStatus(videoId);
      if (!isActive()) return;
      set({ proxy: status, loading: false });

      if (
        status.mode === "window" &&
        !status.available_ranges.some(([start, end]) => start <= 0 && 0 <= end)
      ) {
        void get().ensureFrameAvailable(0).catch(() => undefined);
      }

      if (started.job_id) {
        stopProxyWatcher = watchJob(
          started.job_id,
          (job) => {
            if (!isActive()) return;
            set({ job: job.state === "running" || job.state === "queued" ? job : null });

            if (job.state === "done") {
              void api
                .proxyStatus(videoId)
                .then((nextStatus) => {
                  if (isActive()) set({ proxy: nextStatus });
                })
                .catch((error) => {
                  if (isActive()) set({ error: (error as Error).message, job: null });
                });
            }
          },
          (error) => {
            if (isActive()) set({ error: error.message, job: null });
          },
        );
      }
    } catch (error) {
      if (isActive()) set({ error: (error as Error).message, loading: false });
    }
  },

  close: () => {
    openGeneration += 1;
    cancelFrameAvailabilityFlights();
    stopProxyWatcher?.();
    stopProxyWatcher = null;
    const { videoId, readOnly } = get();
    stopHeartbeat();
    void sequenceLifecycle(async () => {
      try {
        await api.setActiveVideo(null);
      } finally {
        // A liberação faz parte da mesma transição: uma abertura seguinte só
        // ativa/adquire depois que o vídeo anterior terminou de fechar.
        if (videoId && !readOnly) await api.releaseLock(videoId);
      }
    }).catch(() => undefined);
    set({
      videoId: null,
      meta: null,
      proxy: null,
      job: null,
      intervals: [],
      saving: false,
      dirty: false,
      lock: null,
      readOnly: false,
    });
  },

  setPhase: (phase) => set({ phase }),

  setFrame: (frame, provisional = false) => {
    const max = get().frameCount() - 1;
    set({ currentFrame: Math.max(0, Math.min(frame, Math.max(max, 0))), frameProvisional: provisional });
  },

  stepFrame: (delta) => {
    const { currentFrame, phase, meta } = get();
    // Só o <video> nativo produz índice estimado. Quando o codec não toca no
    // navegador, quem reproduz é a sequência de frames extraídos e o índice é
    // exato mesmo na fase de assistir — marcar como provisório ali bloquearia o
    // salvamento pedindo uma confirmação que não faz sentido.
    const fromNativePlayer = phase === "scan" && meta?.media.browser_playable !== false;
    get().setFrame(currentFrame + delta, fromNativePlayer);
  },

  setPlaying: (playing) => set({ playing }),
  setPlaybackRate: (playbackRate) => set({ playbackRate }),
  setStride: (filmstripStride) => set({ filmstripStride }),
  toggleHelp: (show) => set((state) => ({ showHelp: show ?? !state.showHelp })),

  /**
   * Zoom com foco opcional (0-1 no frame), para ampliar sob o cursor.
   *
   * O pan é guardado em fração do frame e clampeado à área que sobra fora da
   * vista, para nunca sair da imagem — em zoom alto, perder a imagem de vista é
   * fácil e desorienta.
   */
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

  setAdjust: (adjust) => set((state) => ({ adjust: { ...state.adjust, ...adjust } })),

  toggleBoost: () =>
    set((state) => ({
      adjust:
        state.adjust.contrast === NEUTRAL_ADJUST.contrast &&
        state.adjust.brightness === NEUTRAL_ADJUST.brightness
          ? { ...BOOST_ADJUST }
          : { ...NEUTRAL_ADJUST },
    })),

  addInterval: (start) => {
    const interval = emptyInterval(start ?? get().currentFrame);
    interval.startProvisional = start !== undefined ? get().frameProvisional : false;
    interval.flags = { ...get().suggestedFlags };
    set((state) => ({
      intervals: [...state.intervals, interval],
      selected: state.intervals.length,
      selectedBbox: -1,
      dirty: true,
    }));
  },

  removeInterval: (at) =>
    set((state) => {
      const intervals = state.intervals.filter((_, index) => index !== at);
      return {
        intervals,
        selected: Math.min(state.selected, intervals.length - 1),
        selectedBbox: -1,
        dirty: true,
      };
    }),

  selectInterval: (at) => set({ selected: at, selectedBbox: -1 }),

  /**
   * `I` abre um intervalo NOVO começando no frame atual.
   *
   * Exceção deliberada: se o intervalo selecionado ainda está aberto (tem início
   * mas não tem fim), `I` reposiciona o início dele em vez de criar outro. É o que
   * sustenta o fluxo de dois estágios — marcar grosso no player e reconfirmar
   * exato no filmstrip não pode gerar dois intervalos.
   */
  setIn: (frame, provisional) =>
    set((state) => {
      const current = state.selected >= 0 ? state.intervals[state.selected] : null;
      const stillOpen = current !== null && current.start !== null && current.end === null;

      if (stillOpen) {
        return {
          intervals: state.intervals.map((interval, index) =>
            index === state.selected
              ? { ...interval, start: frame, startProvisional: provisional }
              : interval,
          ),
          dirty: true,
        };
      }

      const created = emptyInterval(frame);
      created.startProvisional = provisional;
      created.flags = { ...state.suggestedFlags };
      return {
        intervals: [...state.intervals, created],
        selected: state.intervals.length,
        selectedBbox: -1,
        dirty: true,
      };
    }),

  /** `O` fecha o intervalo selecionado; sem seleção, fecha o último aberto. */
  setOut: (frame, provisional) =>
    set((state) => {
      let { selected, intervals } = state;

      if (selected < 0) {
        const open = intervals.findLastIndex((interval) => interval.end === null);
        if (open >= 0) {
          selected = open;
        } else {
          intervals = [...intervals, emptyInterval(null)];
          selected = intervals.length - 1;
        }
      }

      intervals = intervals.map((interval, index) =>
        index === selected
          ? {
              ...interval,
              end: frame,
              endProvisional: provisional,
              // Fim antes do início invalidaria o intervalo: descarta o início.
              start: interval.start !== null && interval.start > frame ? null : interval.start,
            }
          : interval,
      );
      return { intervals, selected, dirty: true };
    }),

  patchInterval: (at, patch) =>
    set((state) => ({
      intervals: state.intervals.map((interval, index) =>
        index === at ? { ...interval, ...patch } : interval,
      ),
      dirty: true,
    })),

  setFlag: (groupId, value) => {
    const { selected } = get();
    if (selected < 0) return;
    const interval = get().intervals[selected];
    get().patchInterval(selected, { flags: { ...interval.flags, [groupId]: value } });
  },

  setBboxes: (bboxes) => {
    const { selected } = get();
    if (selected < 0) return;
    // obj_id sempre 1..n na ordem de criação, como o notebook do SAM3 espera.
    get().patchInterval(selected, {
      bboxes: bboxes.map((box, index) => ({ ...box, obj_id: index + 1 })),
    });
  },

  selectBbox: (selectedBbox) => set({ selectedBbox }),

  setStatus: (status) => set({ status, dirty: true }),
  setVideoNotes: (videoNotes) => set({ videoNotes, dirty: true }),

  frameCount: () => {
    const { proxy, meta } = get();
    return proxy?.frame_count ?? meta?.media.frame_count ?? 0;
  },

  blockers: () => computeBlockers(get().intervals),

  save: async () => {
    const { videoId, intervals, status, videoNotes, label, readOnly } = get();
    if (!videoId || readOnly) return false;
    const generation = openGeneration;
    const isActive = () => openGeneration === generation && get().videoId === videoId;
    set({ saving: true, error: null });
    try {
      const saved = await api.saveAnnotation(videoId, {
        status,
        intervals: toPayload(intervals, label),
        notes: videoNotes,
      });
      if (!isActive()) return false;
      set({ intervals: saved.intervals.map(fromServer), dirty: false, saving: false });
      return true;
    } catch (error) {
      if (!isActive()) return false;
      // 409 com trava: outra pessoa assumiu o vídeo enquanto esta aba editava.
      const lock = error instanceof ApiError ? error.lock : null;
      set({
        error: (error as Error).message,
        saving: false,
        ...(lock ? { lock, readOnly: true } : {}),
      });
      return false;
    }
  },

  queueExport: async () => {
    const { videoId, readOnly } = get();
    if (!videoId || readOnly) return null;
    const generation = openGeneration;
    const isActive = () => openGeneration === generation && get().videoId === videoId;
    set({ saving: true, error: null });
    try {
      const queued = await api.exportVideo(videoId);
      if (!isActive()) return null;
      // Criar o job é a barreira de durabilidade. O FFmpeg continua no worker
      // CPU e não deve prender o operador nesta tela durante centenas de frames.
      set({ saving: false, dirty: false });
      return queued;
    } catch (error) {
      if (!isActive()) return null;
      set({ error: (error as Error).message, saving: false });
      return null;
    }
  },

  markNoBoom: async () => {
    const { videoId, videoNotes, readOnly } = get();
    if (!videoId || readOnly) return false;
    const generation = openGeneration;
    const isActive = () => openGeneration === generation && get().videoId === videoId;
    set({ saving: true, error: null });
    try {
      await api.markNoObject(videoId, videoNotes);
      if (!isActive()) return false;
      set({ status: "no_boom", intervals: [], selected: -1, dirty: false, saving: false });
      return true;
    } catch (error) {
      if (!isActive()) return false;
      set({ error: (error as Error).message, saving: false });
      return false;
    }
  },

  ensureFrameAvailable: async (frame) => {
    const { videoId, proxy } = get();
    if (!videoId || !proxy) throw new Error("vídeo ou proxy indisponível");

    const generation = openGeneration;
    const key = `${generation}:${videoId}:${frame}`;
    const existing = frameAvailabilityFlights.get(key);
    if (existing) return existing.promise;

    let cancelled = false;
    let stopWatcher: (() => void) | null = null;
    let rejectCancellation: (error: Error) => void = () => undefined;
    const cancellation = new Promise<never>((_, reject) => {
      rejectCancellation = reject;
    });
    const isActive = () =>
      !cancelled && openGeneration === generation && get().videoId === videoId;
    const assertActive = () => {
      if (!isActive()) throw new FrameRecoveryCancelled();
    };
    const hasFrame = (status: ProxyStatus) =>
      status.complete || status.available_ranges.some(([start, end]) => start <= frame && frame <= end);
    const fingerprint = (status: ProxyStatus) =>
      JSON.stringify([status.mode, status.complete, status.frame_count, status.available_ranges]);

    const run = async (): Promise<FrameRecoveryOutcome> => {
      let jobId: string | null = null;

      if (proxy.complete) {
        // Falhar nos dois tiers contradiz o marcador de completude: reconstrua
        // uma vez, sem permitir uma tempestade de requests do <img>.
        jobId = (await api.startProxy(videoId, true)).job_id;
      } else if (proxy.mode === "window") {
        const covered = proxy.available_ranges.some(
          ([start, end]) => start <= frame && frame <= end,
        );
        jobId = (await api.startWindow(videoId, frame, undefined, covered)).job_id;
      } else {
        const active = proxy.jobs.find(
          (job) => job.kind === "proxy_full" && ["queued", "running"].includes(job.state),
        );
        jobId = active?.job_id ?? (await api.startProxy(videoId, true)).job_id;
      }
      assertActive();

      if (jobId) {
        await new Promise<void>((resolve, reject) => {
          stopWatcher = watchJob(
            jobId!,
            (job) => {
              if (!isActive()) {
                stopWatcher?.();
                reject(new FrameRecoveryCancelled());
                return;
              }
              if (["running", "queued"].includes(job.state)) {
                set({ job });
                return;
              }
              stopWatcher?.();
              if (job.state === "done") {
                resolve();
                return;
              }
              reject(
                new Error(
                  job.state === "cancelled"
                    ? "Reparo de frame cancelado. Tente novamente."
                    : job.error || "Falha ao extrair o frame.",
                ),
              );
            },
            reject,
          );
        });
      }

      assertActive();
      const nextStatus = await api.proxyStatus(videoId);
      assertActive();
      if (!jobId && fingerprint(nextStatus) === fingerprint(proxy)) {
        throw new Error("O reparo não iniciou. Tente novamente.");
      }
      if (!hasFrame(nextStatus)) {
        throw new Error("O reparo terminou sem disponibilizar este frame. Tente novamente.");
      }
      set({ proxy: nextStatus, job: null });
      return { proxy: nextStatus, jobId };
    };

    const record: FrameAvailabilityFlight = {
      promise: Promise.resolve({ proxy, jobId: null }),
      cancel: () => {
        if (cancelled) return;
        cancelled = true;
        stopWatcher?.();
        rejectCancellation(new FrameRecoveryCancelled());
      },
    };
    const promise = Promise.race([run(), cancellation])
      .catch((error) => {
        if (!(error instanceof FrameRecoveryCancelled) && isActive()) {
          set({ error: (error as Error).message, job: null });
        }
        throw error;
      })
      .finally(() => {
        stopWatcher?.();
        if (frameAvailabilityFlights.get(key) === record) {
          frameAvailabilityFlights.delete(key);
        }
      });
    record.promise = promise;
    frameAvailabilityFlights.set(key, record);
    return promise;
  },
}));
