export type Status = "pending" | "in_progress" | "done" | "no_boom";
export type PipelineStage = "triage" | "sam3" | "review" | "completed" | "discarded";

export interface StageProgress {
  expected_frames: number;
  reviewed_frames: number;
  edited_frames: number;
  artifacts_valid: boolean;
  inconsistencies: string[];
}

export interface PipelineProjectionSnapshot extends StageProgress {
  pipeline_stage: PipelineStage;
  stage_status: string;
  validation_status: "manifest" | "invalid" | "audit_required" | "not_applicable";
  complete: boolean;
}

export interface FlagOption {
  id: string;
  label: string;
}

export interface FlagGroup {
  id: string;
  label: string;
  multi: boolean;
  required: boolean;
  help: string;
  options: FlagOption[];
}

export interface ObjectInfo {
  object_id: string;
  display_name: string;
  /** Rótulo gravado em cada bbox e no prompt.json. */
  label: string;
  videos_root: string;
  output_root: string;
  gcs_uri: string | null;
  exclude_patterns: ExcludePattern[];
  suggest_rules: string | null;
  created_at: string;
  created_by: string | null;
  archived: boolean;
  gcs_ready: boolean;
}

export interface UserInfo {
  user_id: string;
  display_name: string;
  color: string;
  created_at: string;
  last_seen_at: string | null;
}

export interface LockInfo {
  user: string;
  since: string;
  /** Segredo efêmero desta aquisição; só é devolvido ao dono da trava. */
  token?: string;
}

/** `null` = ainda não revisado; "ok" = conferido; "edited" = corrigido à mão. */
export type ReviewStatus = "ok" | "edited" | null;

export interface ReviewFrame {
  frame: number;
  boxes: BBox[];
  status: ReviewStatus;
}

export interface ReviewSegment {
  video_id: string;
  name: string;
  segment: string;
  segments: string[];
  classes: string[];
  label: string;
  export_version: string;
  prompt: {
    image_width?: number;
    image_height?: number;
    source_start_frame?: number;
    frame_idx?: number;
    flags?: FlagValues;
    label?: string;
    objects?: BBox[];
  };
  frame_count: number;
  reviewed: number;
  edited: number;
  with_objects: number;
  complete: boolean;
  reviewed_by: string | null;
  frames: ReviewFrame[];
}

export interface ReviewProgress {
  video_id: string;
  name: string;
  frame_count: number;
  reviewed: number;
  edited: number;
  complete: boolean;
  classes: string[];
  segments: {
    segment: string;
    frame_count: number;
    reviewed: number;
    edited: number;
    complete: boolean;
  }[];
}

export interface DatasetPreview {
  segments: number;
  videos: number;
  frames: number;
  frames_with_objects: number;
  frames_reviewed: number;
  classes: string[];
  by_flag: Record<string, Record<string, number>>;
  task: "detection" | "segmentation";
  export_allowed: boolean;
  blocking_reasons: string[];
}

export interface DatasetInfo {
  name: string;
  path: string;
  format: "yolo" | "coco";
  task: "detection" | "segmentation";
  generated_at: string;
  counts: Record<string, unknown>;
  filters: Record<string, unknown>;
  scope?: "global" | "object";
  object_id?: string | null;
  classes?: unknown[];
}

export interface GlobalClassInfo {
  id: number;
  object_id: string;
  name: string;
}

export interface GlobalDatasetPreview {
  segments: number;
  videos: number;
  frames: number;
  frames_with_objects: number;
  frames_reviewed: number;
  classes: GlobalClassInfo[];
  by_class: { object_id: string; name: string; videos: number; frames: number }[];
  by_flag: Record<string, Record<string, number>>;
  task: "detection" | "segmentation";
  completed_only: true;
  export_allowed: boolean;
  blocking_reasons: string[];
}

export interface SelectedVideo {
  object_id: string;
  video_id: string;
}

export interface MaskReviewInstance {
  obj_id: number;
  label: string;
  mask_url: string;
  bbox: [number, number, number, number] | null;
  area_pixels: number;
  sha256: string;
}

export interface MaskReviewFrame {
  frame: number;
  revision: number;
  status: "ok" | "edited" | null;
  reviewed_by?: string | null;
  instances: MaskReviewInstance[];
}

export interface MaskReviewDraft {
  status: "edited";
  instances: { obj_id: number; label: string; png_base64: string }[];
  retain_obj_ids: number[];
}

export interface MaskReviewBatchFrame {
  frame: number;
  expected_revision: number;
  status: "ok" | "edited";
  instances: { obj_id: number; label: string; png_base64: string }[];
  retain_obj_ids: number[];
}

export interface MaskReviewBatchResult {
  frames: { frame: number; revision: number; status: "ok" | "edited" }[];
  reviewed: number;
  frame_count: number;
  complete: boolean;
  video: {
    frame_count: number;
    reviewed: number;
    edited: number;
    complete: boolean;
    segments: {
      segment: string;
      frame_count: number;
      reviewed: number;
      edited: number;
      complete: boolean;
    }[];
  };
  pipeline: PipelineProjectionSnapshot | null;
  completion_error: string | null;
  projection_pending: boolean;
  projection_event_seq: number | null;
  sync_job_id: string | null;
  sync_pending: boolean;
}

export interface Sam3Preview {
  seq: number;
  mask_url: string | null;
  bbox: [number, number, number, number] | null;
  area_frac: number | null;
  error: string | null;
  at: string;
}

export interface Sam3Session {
  session_id?: string;
  segment?: string;
  frame_idx?: number;
  /** "none" quando não há sessão aberta para este segmento. */
  state: "none" | "opening" | "ready" | "busy" | "closed" | "error";
  error?: string | null;
  expires_in?: number;
  preview?: Sam3Preview | null;
}

export type Sam3State =
  | "queued"
  | "leased"
  | "running"
  | "done"
  | "error"
  | "cancelled";

export interface Sam3Info {
  state: Sam3State;
  progress: {
    segments_done?: number;
    segments_total?: number;
    frames_done?: number;
    current?: string;
  };
  attempts: number;
  error: string | null;
  segments: number;
  finished_at: string | null;
}

export interface AppConfig {
  app_version: string;
  workspace_root: string | null;
  workspace_ready: boolean;
  objects: ObjectInfo[];
  last_object_id: string | null;
  user: UserInfo | null;
  users: UserInfo[];
  flag_groups: FlagGroup[];
  flag_groups_version: number;
  suggest_rules: string[];
  proxy_max_frames: number;
  cache_limit_gb: number;
  video_exts: string[];
  can_browse_fs: boolean;
  ffmpeg: {
    ok: boolean;
    version: string | null;
    path: string | null;
    source?: string;
    error: string | null;
  };
  gcloud: {
    ok: boolean;
    path: string | null;
    source: string | null;
    error: string | null;
    credentials: string | null;
    credentials_ok: boolean;
    default_bucket: string | null;
    chunk: number;
  };
}

export interface ExcludePattern {
  kind: "substring" | "regex";
  value: string;
  case_sensitive?: boolean;
}

export interface ExcludedRecord {
  relpath: string;
  reason: "pattern" | "manual" | "corrupt";
  pattern: ExcludePattern | null;
  excluded_at: string;
  excluded_by: string | null;
  moved_to: string | null;
  size_bytes: number | null;
  note: string;
}

export interface ExclusionsInfo {
  exclude_patterns: ExcludePattern[];
  excluded: Record<string, ExcludedRecord>;
  trash_dir: string;
}

export interface RemoteFile {
  name: string;
  uri: string;
  size_bytes: number | null;
  generation: string | null;
  updated: string | null;
  pattern?: ExcludePattern;
  reason?: string;
}

/** Baldes da prévia de download. A ordem é parte do contrato — ver o servidor. */
export type GcsBucket =
  | "downloaded"
  | "missing"
  | "excluded"
  | "matches_pattern"
  | "skipped"
  | "new";

export interface GcsListing {
  gcs_uri: string;
  listed_at: string;
  total: number;
  counts: Record<GcsBucket, number>;
  buckets: Record<GcsBucket, RemoteFile[]>;
}

export interface VideoMedia {
  duration_sec: number | null;
  width: number;
  height: number;
  coded_width: number;
  coded_height: number;
  rotation: number;
  codec: string;
  pix_fmt: string | null;
  avg_frame_rate: string | null;
  r_frame_rate: string | null;
  fps: number | null;
  start_time_sec: number;
  container_nb_frames: number | null;
  frame_count: number | null;
  frame_count_source: string;
  frame_count_exact: boolean;
  is_vfr_suspect: boolean;
  browser_playable: boolean;
  probed_at?: string;
}

export interface VideoListItem {
  video_id: string;
  relpath: string;
  name: string;
  size_bytes: number;
  file_mtime: string;
  status: Status;
  interval_count: number;
  missing: boolean;
  probed: boolean;
  duration_sec: number | null;
  width: number | null;
  height: number | null;
  frame_count: number | null;
  browser_playable: boolean | null;
  thumb_url: string;
  updated_by?: string | null;
  /** Preenchido quando alguém está com o vídeo em triagem. */
  lock: LockInfo | null;
  /** Estado na fila de anotação automática; null se nunca foi enfileirado. */
  sam3: Sam3Info | null;
  /** Etapa derivada dos artefatos reais; "completed" exige todas as mascaras revisadas. */
  pipeline_stage: PipelineStage;
  stage_status: string;
  stage_progress: StageProgress;
  projection_status?: "current" | "stale" | "missing" | "pending";
  projected_at?: string | null;
}

export interface VideoListResponse {
  object_id: string;
  label: string;
  videos_root: string;
  scanned_at: string | null;
  total: number;
  counts: Record<string, number>;
  pipeline_counts: Partial<Record<PipelineStage, number>>;
  pipeline_status_counts: Record<string, number>;
  videos: VideoListItem[];
}

export interface VideoMeta {
  video_id: string;
  object_id: string;
  label: string;
  relpath: string;
  name: string;
  size_bytes: number;
  file_mtime: string;
  media: VideoMedia;
  stream_url: string;
}

export interface BBox {
  obj_id: number;
  label: string;
  /** Fonte da verdade: [x1, y1, x2, y2] em 0-1, ordem exata do SAM3. */
  normalized: [number, number, number, number];
  pixel?: [number, number, number, number];
}

export type FlagValues = Record<string, string | string[] | null>;

export interface Interval {
  index: number;
  segment: string;
  start_frame: number;
  end_frame: number;
  frame_count: number;
  start_time_sec: number | null;
  end_time_sec: number | null;
  prompt_frame: number;
  bboxes: BBox[];
  flags: FlagValues;
  notes: string;
}

export interface ExportInfo {
  root: string;
  segments: string[];
  total_frames: number;
  jpeg_qscale: number;
  frame_naming: string;
  ffmpeg_version: string;
}

export interface VideoEntry {
  video_id: string;
  relpath: string;
  name: string;
  status: Status;
  notes: string;
  intervals: Interval[];
  exported_at: string | null;
  export: ExportInfo | null;
  /** Versão otimista usada para impedir sobrescrita por uma aba antiga. */
  annotation_revision: number;
  media?: VideoMedia;
  /** Flags inferidas do nome do arquivo, para pré-marcar intervalos novos. */
  suggested_flags?: FlagValues;
  lock?: LockInfo | null;
}

export type JobKind = "proxy_full" | "proxy_window" | "export" | "gcs_download" | "dataset_export" | "dataset_export_global" | "object_purge";
export type JobState = "queued" | "running" | "done" | "cancelled" | "error";

export interface JobInfo {
  job_id: string;
  kind: JobKind;
  object_id: string;
  video_id: string;
  state: JobState;
  current: number;
  total: number;
  progress: number;
  message: string;
  error: string | null;
  result: Record<string, unknown>;
  /** Posição na fila do semáforo pesado — compartilhado entre usuários. */
  queue_pos: number;
  user: string | null;
}

export interface ProxyStatus {
  mode: "full" | "window";
  complete: boolean;
  frame_count: number | null;
  available_ranges: [number, number][];
  generation?: string | null;
  jobs: JobInfo[];
}

export interface DirListing {
  path: string;
  parent: string | null;
  dirs: { name: string; path: string }[];
  video_count: number;
}
