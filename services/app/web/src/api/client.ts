import { clientId, objectPath } from "./scope";
import type {
  AppConfig,
  BBox,
  DatasetInfo,
  MaskReviewFrame,
  DatasetPreview,
  DirListing,
  ExclusionsInfo,
  GcsListing,
  GlobalDatasetPreview,
  Interval,
  JobInfo,
  LockInfo,
  ObjectInfo,
  ProxyStatus,
  ReviewProgress,
  ReviewSegment,
  Sam3Info,
  Sam3Session,
  Status,
  UserInfo,
  VideoEntry,
  VideoListResponse,
  VideoMeta,
  SelectedVideo,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body?: unknown,
  ) {
    super(message);
  }

  /** Trava de outro usuário, quando o 409 veio de um vídeo em triagem. */
  get lock(): LockInfo | null {
    const detail = (this.body as { detail?: { lock?: LockInfo } } | undefined)?.detail;
    return detail && typeof detail === "object" ? (detail.lock ?? null) : null;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-MST-Client": clientId(),
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    let detail: string = response.statusText;
    let body: unknown;
    try {
      body = await response.json();
      const raw = (body as { detail?: unknown }).detail;
      if (typeof raw === "string") detail = raw;
      else if (raw && typeof raw === "object" && "errors" in raw) {
        detail = (raw as { errors: string[] }).errors.join("\n");
      } else if (raw && typeof raw === "object" && "detail" in raw) {
        detail = String((raw as { detail: unknown }).detail);
      }
    } catch {
      /* corpo não-JSON: fica com o statusText */
    }
    throw new ApiError(detail, response.status, body);
  }
  return response.json() as Promise<T>;
}

export interface IntervalPayload {
  start_frame: number;
  end_frame: number;
  prompt_frame?: number;
  bboxes: { obj_id?: number; label: string; normalized: number[] }[];
  flags: Record<string, unknown>;
  notes: string;
}

/**
 * Prefixo do objeto ativo. Todas as assinaturas de `api.*` continuam recebendo
 * só o videoId — o escopo é resolvido aqui, então nenhum componente ou store
 * precisou mudar quando o app deixou de ser de um objeto só.
 */
const O = () => objectPath();

export const api = {
  // -- global --------------------------------------------------------------

  config: () => request<AppConfig>("/api/config"),

  setWorkspace: (workspace_root: string) =>
    request<AppConfig>("/api/config/workspace", {
      method: "POST",
      body: JSON.stringify({ workspace_root }),
    }),

  listDirs: (path?: string) =>
    request<DirListing>(
      `/api/fs/list${path ? `?path=${encodeURIComponent(path)}` : ""}`,
    ),

  // -- objetos -------------------------------------------------------------

  objects: (includeArchived = false) =>
    request<{ objects: ObjectInfo[] }>(
      `/api/objects${includeArchived ? "?include_archived=true" : ""}`,
    ),

  createObject: (payload: {
    display_name: string;
    object_id?: string;
    label?: string;
    gcs_uri?: string;
  }) =>
    request<ObjectInfo>("/api/objects", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  updateObject: (
    objectId: string,
    payload: Partial<{
      display_name: string;
      label: string;
      gcs_uri: string;
      archived: boolean;
    }>,
  ) =>
    request<ObjectInfo>(`/api/objects/${encodeURIComponent(objectId)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),

  archiveObject: (objectId: string) =>
    request<ObjectInfo>(`/api/objects/${encodeURIComponent(objectId)}/archive`, {
      method: "POST",
    }),

  restoreObject: (objectId: string) =>
    request<ObjectInfo>(`/api/objects/${encodeURIComponent(objectId)}/restore`, {
      method: "POST",
    }),

  purgeObject: (objectId: string, confirmation: string) =>
    request<{ job_id: string; managed_paths: string[]; skipped_paths: string[] }>(
      `/api/objects/${encodeURIComponent(objectId)}/purge`,
      { method: "POST", body: JSON.stringify({ confirmation }) },
    ),

  // -- usuários ------------------------------------------------------------

  users: () => request<{ users: UserInfo[] }>("/api/users"),

  createUser: (display_name: string) =>
    request<UserInfo>("/api/users", {
      method: "POST",
      body: JSON.stringify({ display_name }),
    }),

  login: (user_id: string) =>
    request<UserInfo>("/api/session/login", {
      method: "POST",
      body: JSON.stringify({ user_id }),
    }),

  logout: () => request<{ ok: boolean }>("/api/session/logout", { method: "POST" }),

  // -- biblioteca ----------------------------------------------------------

  videos: (params: { search?: string; status?: string; sort?: string } = {}) => {
    const query = new URLSearchParams();
    if (params.search) query.set("search", params.search);
    if (params.status && params.status !== "all") query.set("status", params.status);
    if (params.sort) query.set("sort", params.sort);
    const suffix = query.toString();
    return request<VideoListResponse>(`${O()}/videos${suffix ? `?${suffix}` : ""}`);
  },

  videosForObject: (objectId: string) =>
    request<VideoListResponse>(`/api/objects/${encodeURIComponent(objectId)}/videos`),

  rescan: () => request<VideoListResponse>(`${O()}/videos/rescan`, { method: "POST" }),

  meta: (videoId: string, refresh = false) =>
    request<VideoMeta>(`${O()}/videos/${videoId}/meta${refresh ? "?refresh=1" : ""}`),

  // -- proxy ---------------------------------------------------------------

  startProxy: (videoId: string, force = false) =>
    request<{
      mode: "full" | "window";
      job_id: string | null;
      total_frames: number | null;
      already_complete: boolean;
    }>(`${O()}/videos/${videoId}/proxy`, {
      method: "POST",
      body: JSON.stringify({ force }),
    }),

  startWindow: (videoId: string, center: number, radius?: number, force = false) =>
    request<{ job_id: string | null; start: number; end: number; already_available: boolean }>(
      `${O()}/videos/${videoId}/window`,
      {
        method: "POST",
        body: JSON.stringify({ center, ...(radius ? { radius } : {}), force }),
      },
    ),

  proxyStatus: (videoId: string) =>
    request<ProxyStatus>(`${O()}/videos/${videoId}/proxy/status`),

  /** `small` = filmstrip e reprodução; `full` = imagem do palco em resolução original. */
  frameUrl: (videoId: string, frame: number, tier: "small" | "full" = "small") =>
    `${O()}/videos/${videoId}/frames/${frame}.jpg?tier=${tier}`,

  streamUrl: (videoId: string) => `${O()}/videos/${videoId}/stream`,

  // -- jobs ----------------------------------------------------------------

  job: (jobId: string) => request<JobInfo>(`/api/jobs/${jobId}`),

  cancelJob: (jobId: string) =>
    request<{ cancelled: boolean }>(`/api/jobs/${jobId}`, { method: "DELETE" }),

  /** Troca de vídeo: cancela as extrações desta aba e renova a trava. */
  setActiveVideo: (videoId: string | null) =>
    request<{ cancelled: number; lock: LockInfo | null }>(`${O()}/session/active-video`, {
      method: "POST",
      body: JSON.stringify({ video_id: videoId }),
    }),

  cacheInfo: () =>
    request<{ bytes: number; gb: number; limit_gb: number }>(`${O()}/cache`),

  clearCache: () => request<{ ok: boolean }>(`${O()}/cache`, { method: "DELETE" }),

  // -- travas --------------------------------------------------------------

  acquireLock: (videoId: string, force = false) =>
    request<{ lock: LockInfo; heartbeat_seconds: number; ttl_seconds: number }>(
      `${O()}/lock`,
      { method: "POST", body: JSON.stringify({ video_id: videoId, force }) },
    ),

  releaseLock: (videoId: string) =>
    request<{ released: boolean }>(`${O()}/lock/release`, {
      method: "POST",
      body: JSON.stringify({ video_id: videoId }),
    }),

  /**
   * Libera no fechamento da aba. sendBeacon sobrevive ao unload, ao contrário de
   * um fetch normal — sem isso o vídeo ficaria preso até o TTL de 90 s.
   */
  releaseLockBeacon: (videoId: string) => {
    try {
      const blob = new Blob(
        [JSON.stringify({ video_id: videoId, client_id: clientId() })],
        { type: "application/json" },
      );
      navigator.sendBeacon(`${O()}/lock/release?client=${clientId()}`, blob);
    } catch {
      /* melhor esforço: o TTL cobre */
    }
  },

  // -- anotações -----------------------------------------------------------

  annotation: (videoId: string) => request<VideoEntry>(`${O()}/annotations/${videoId}`),

  saveAnnotation: (
    videoId: string,
    payload: { status: Status; intervals: IntervalPayload[]; notes: string },
    force = false,
  ) =>
    request<VideoEntry & { intervals: Interval[] }>(
      `${O()}/annotations/${videoId}${force ? "?force=1" : ""}`,
      { method: "PUT", body: JSON.stringify(payload) },
    ),

  markNoObject: (videoId: string, notes = "", deleteExported = true) =>
    request<VideoEntry>(`${O()}/annotations/${videoId}/no-object`, {
      method: "POST",
      body: JSON.stringify({ notes, delete_exported: deleteExported }),
    }),

  exportVideo: (videoId: string) =>
    request<{ job_id: string; total: number }>(`${O()}/videos/${videoId}/export`, {
      method: "POST",
    }),

  // -- entrada de vídeos ---------------------------------------------------

  exclusions: () => request<ExclusionsInfo>(`${O()}/exclusions`),

  setPatterns: (exclude_patterns: ExclusionsInfo["exclude_patterns"]) =>
    request<{ exclude_patterns: ExclusionsInfo["exclude_patterns"] }>(
      `${O()}/exclusions/patterns`,
      { method: "PUT", body: JSON.stringify({ exclude_patterns }) },
    ),

  applyExclusions: (apply = false, force = false) =>
    request<{
      applied: boolean;
      matches: { video_id: string; name: string; status: Status }[];
      blocked: { video_id: string; name: string; status: Status }[];
      moved: { name: string }[];
    }>(`${O()}/exclusions/apply?apply=${apply ? 1 : 0}&force=${force ? 1 : 0}`, {
      method: "POST",
    }),

  discardVideo: (videoId: string, note = "", force = false) =>
    request<{ discarded: string }>(`${O()}/exclusions/discard?force=${force ? 1 : 0}`, {
      method: "POST",
      body: JSON.stringify({ video_id: videoId, note }),
    }),

  restoreVideo: (name: string) =>
    request<{ restored: string }>(`${O()}/exclusions/restore`, {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  gcsList: (gcsUri?: string) =>
    request<GcsListing>(`${O()}/gcs/list`, {
      method: "POST",
      body: JSON.stringify({ gcs_uri: gcsUri ?? null }),
    }),

  gcsDownload: (names: string[]) =>
    request<{ job_id: string; total: number }>(`${O()}/gcs/download`, {
      method: "POST",
      body: JSON.stringify({ names }),
    }),

  // -- anotação automática (SAM3) ------------------------------------------

  sam3Queue: () =>
    request<{
      object_id: string;
      counts: Record<string, number>;
      active: boolean;
      videos: Record<string, Sam3Info>;
      worker: {
        state: "loading" | "ready" | "busy" | "error" | "unavailable";
        message: string | null;
        worker_id: string | null;
        age_seconds: number | null;
      };
    }>(`${O()}/sam3`),

  sam3Enqueue: (videoId: string, force = false) =>
    request<Sam3Info>(`${O()}/videos/${videoId}/sam3`, {
      method: "POST",
      body: JSON.stringify({ force }),
    }),

  sam3Cancel: (videoId: string) =>
    request<Sam3Info>(`${O()}/videos/${videoId}/sam3`, { method: "DELETE" }),

  // -- sessão interativa do SAM3 -------------------------------------------

  sam3OpenSession: (videoId: string, segment: string, frameIdx = 0) =>
    request<Sam3Session>(`${O()}/videos/${videoId}/sam3/session`, {
      method: "POST",
      body: JSON.stringify({ segment, frame_idx: frameIdx }),
    }),

  /** Com `wait`, a resposta só volta quando o estado muda — evita polling. */
  sam3Session: (videoId: string, segment: string, wait = 0) =>
    request<Sam3Session>(
      `${O()}/videos/${videoId}/sam3/session?segment=${encodeURIComponent(segment)}&wait=${wait}`,
    ),

  sam3Preview: (videoId: string, sessionId: string, boxes: BBox[]) =>
    request<Sam3Session>(
      `${O()}/videos/${videoId}/sam3/session/${sessionId}/preview`,
      { method: "POST", body: JSON.stringify({ boxes }) },
    ),

  sam3CloseSession: (videoId: string, sessionId: string) =>
    request<{ closed: boolean }>(
      `${O()}/videos/${videoId}/sam3/session/${sessionId}`,
      { method: "DELETE" },
    ),

  sam3SaveOverride: (videoId: string, segment: string, boxes: BBox[]) =>
    request<{ objects: unknown[] }>(`${O()}/videos/${videoId}/sam3/override`, {
      method: "PUT",
      body: JSON.stringify({ segment, boxes }),
    }),

  sam3ClearOverride: (videoId: string, segment: string) =>
    request<{ cleared: boolean }>(
      `${O()}/videos/${videoId}/sam3/override?segment=${encodeURIComponent(segment)}`,
      { method: "DELETE" },
    ),

  // -- revisão --------------------------------------------------------------

  videoReview: (videoId: string) =>
    request<ReviewProgress>(`${O()}/videos/${videoId}/review`),

  segmentReview: (videoId: string, segment: string) =>
    request<ReviewSegment>(`${O()}/videos/${videoId}/segments/${segment}/review`),

  /** Frame do EXPORT — não do cache de proxy, que é evictável. */
  segmentFrameUrl: (videoId: string, segment: string, frame: number) =>
    `${O()}/videos/${videoId}/segments/${segment}/frames/${frame}.jpg`,

  reviewFrame: (
    videoId: string,
    segment: string,
    frame: number,
    payload: { status: "ok" | "edited"; boxes: BBox[] },
  ) =>
    request<{ frame: number; status: string }>(
      `${O()}/videos/${videoId}/segments/${segment}/review/${frame}`,
      { method: "PUT", body: JSON.stringify(payload) },
    ),

  reviewConfirm: (videoId: string, segment: string, start: number, end: number) =>
    request<{ confirmed: number; reviewed: number; complete: boolean }>(
      `${O()}/videos/${videoId}/segments/${segment}/review/confirm`,
      { method: "POST", body: JSON.stringify({ start, end }) },
    ),

  reviewReset: (videoId: string, segment: string, frame: number) =>
    request<{ frame: number }>(
      `${O()}/videos/${videoId}/segments/${segment}/review/${frame}`,
      { method: "DELETE" },
    ),

  maskReviewFrame: (videoId: string, segment: string, frame: number) =>
    request<MaskReviewFrame>(
      `${O()}/videos/${videoId}/segments/${segment}/mask-review/${frame}`,
    ),

  saveMaskReviewFrame: (
    videoId: string,
    segment: string,
    frame: number,
    payload: {
      expected_revision: number;
      status: "ok" | "edited";
      instances: { obj_id: number; label: string; png_base64: string }[];
      retain_obj_ids?: number[];
    },
  ) =>
    request<MaskReviewFrame>(
      `${O()}/videos/${videoId}/segments/${segment}/mask-review/${frame}`,
      { method: "PUT", body: JSON.stringify(payload) },
    ),

  saveMaskReviewBatch: (
    videoId: string,
    segment: string,
    frames: import("./types").MaskReviewBatchFrame[],
  ) =>
    request<import("./types").MaskReviewBatchResult>(
      `${O()}/videos/${videoId}/segments/${segment}/mask-review`,
      { method: "PUT", body: JSON.stringify({ frames }) },
    ),

  // -- dataset --------------------------------------------------------------

  datasetPreview: (filters: {
    flags?: Record<string, string[]>;
    video_ids?: string[];
    reviewed_only?: boolean;
    include_empty?: boolean;
    task?: "detection" | "segmentation";
  }) =>
    request<DatasetPreview>(`${O()}/dataset/preview`, {
      method: "POST",
      body: JSON.stringify(filters),
    }),

  datasetExport: (payload: {
    format: "yolo" | "coco";
    task: "detection" | "segmentation";
    name?: string;
    val_fraction: number;
    test_fraction: number;
    filters: Record<string, unknown>;
  }) =>
    request<{ job_id: string; name: string; out_dir: string }>(`${O()}/dataset/export`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  datasets: () => request<{ datasets: DatasetInfo[] }>(`${O()}/dataset/list`),

  deleteDataset: (name: string) =>
    request<{ deleted: string }>(`${O()}/dataset/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),

  globalDatasetPreview: (payload: {
    object_ids: string[];
    filters: { flags: Record<string, string[]>; videos: SelectedVideo[]; include_empty: boolean };
    task: "detection" | "segmentation";
  }) =>
    request<GlobalDatasetPreview>("/api/datasets/preview", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  globalDatasetExport: (payload: {
    object_ids: string[];
    filters: { flags: Record<string, string[]>; videos: SelectedVideo[]; include_empty: boolean };
    format: "yolo" | "coco";
    task: "detection" | "segmentation";
    name?: string;
    val_fraction: number;
    test_fraction: number;
  }) =>
    request<{ job_id: string; name: string; out_dir: string }>("/api/datasets/export", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  globalDatasets: () => request<{ datasets: DatasetInfo[] }>("/api/datasets"),
};

/** Acompanha um job por SSE até o estado terminal. */
export function watchJob(
  jobId: string,
  onUpdate: (job: JobInfo) => void,
  onFailure?: (error: Error) => void,
): () => void {
  const source = new EventSource(`/api/jobs/${jobId}/events`);
  let stopped = false;
  let polling = false;
  let pollTimer: ReturnType<typeof setInterval> | null = null;

  const stop = () => {
    if (stopped) return;
    stopped = true;
    source.close();
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  };

  const fail = (error: unknown) => {
    if (stopped) return;
    const reason = error instanceof Error ? error : new Error(String(error));
    stop();
    try {
      onFailure?.(reason);
    } catch {
      // O watcher já foi encerrado; uma falha do consumidor não pode reabrir
      // polling nem virar uma rejeição global sem dono.
    }
  };

  const publish = (job: JobInfo) => {
    if (stopped) return;
    try {
      onUpdate(job);
    } catch (error) {
      fail(error);
      return;
    }
    if (job.state === "done" || job.state === "cancelled" || job.state === "error") {
      stop();
    }
  };

  const pollOnce = async () => {
    if (stopped || polling) return;
    polling = true;
    try {
      publish(await api.job(jobId));
    } catch (error) {
      fail(new Error(`Falha ao acompanhar o job ${jobId}: ${(error as Error).message}`));
    } finally {
      polling = false;
    }
  };

  source.onmessage = (event) => {
    try {
      publish(JSON.parse(event.data) as JobInfo);
    } catch (error) {
      fail(new Error(`Resposta inválida ao acompanhar o job ${jobId}: ${(error as Error).message}`));
    }
  };

  // Se o SSE cair (proxy de dev, sleep da máquina), o polling assume.
  source.onerror = () => {
    if (stopped || pollTimer !== null) return;
    source.close();
    void pollOnce();
    pollTimer = setInterval(() => void pollOnce(), 1000);
  };

  return stop;
}
