/**
 * Smoke test: o bundle buildado monta de verdade?
 *
 * Existe porque um seletor de zustand devolvendo array novo a cada chamada faz o
 * React 19 desmontar a árvore inteira — e o sintoma é uma TELA PRETA, sem erro
 * visível nem falha de build. O tsc e o vite build passam felizes.
 *
 *     node scripts/smoke.mjs [cenario]
 *
 * Um CENÁRIO por processo: o bundle se auto-monta no #root ao ser importado, e o
 * cache de módulos ESM impede montar de novo com outro estado. Cada tela do app
 * é escolhida pelo que o /api/config devolve, então basta variar o mock.
 */

import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { JSDOM } from "jsdom";

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = join(here, "..", "..", "static");

const OBJECT = {
  object_id: "boom",
  display_name: "Boom",
  label: "boom",
  videos_root: "/videos",
  output_root: "/out",
  gcs_uri: "gs://bucket/boom/",
  exclude_patterns: [],
  suggest_rules: "boom",
  created_at: "2026-01-01T00:00:00-03:00",
  created_by: null,
  archived: false,
  gcs_ready: true,
};

const USER = {
  user_id: "guilherme",
  display_name: "Guilherme",
  color: "#f59e0b",
  created_at: "2026-01-01T00:00:00-03:00",
  last_seen_at: null,
};

const CONFIG = {
  app_version: "0.2.0",
  workspace_root: "/ws",
  workspace_ready: true,
  objects: [OBJECT],
  last_object_id: "boom",
  user: USER,
  users: [USER],
  flag_groups: [
    {
      id: "dificuldade",
      label: "Dificuldade",
      multi: false,
      required: true,
      help: "",
      options: [{ id: "facil", label: "Fácil" }],
    },
  ],
  flag_groups_version: 1,
  suggest_rules: ["boom"],
  proxy_max_frames: 4000,
  cache_limit_gb: 20,
  video_exts: [".mp4"],
  can_browse_fs: true,
  ffmpeg: { ok: true, version: "8.0", path: "ffmpeg", error: null },
  gcloud: {
    ok: true,
    path: "gcloud",
    source: "path",
    error: null,
    credentials: "/keys/sa.json",
    credentials_ok: true,
    default_bucket: "gs://bucket",
    chunk: 20,
  },
};

const video = (id, name, status, extra = {}) => ({
  video_id: id,
  relpath: `${name}.mp4`,
  name,
  size_bytes: 1,
  file_mtime: "2026-01-01T00:00:00-03:00",
  status,
  interval_count: status === "done" ? 2 : 0,
  missing: false,
  probed: true,
  duration_sec: 30,
  width: 640,
  height: 360,
  frame_count: 900,
  browser_playable: true,
  thumb_url: `/api/objects/boom/videos/${id}/thumb`,
  lock: null,
  sam3: null,
  pipeline_stage: status === "done" ? "sam3" : status === "no_boom" ? "discarded" : "triage",
  stage_status: status === "done" ? "ready" : status === "no_boom" ? "discarded" : status,
  stage_progress: {
    expected_frames: 0,
    reviewed_frames: 0,
    edited_frames: 0,
    artifacts_valid: false,
    inconsistencies: [],
  },
  ...extra,
});

const VIDEOS = {
  object_id: "boom",
  label: "boom",
  videos_root: "/videos",
  scanned_at: null,
  total: 3,
  counts: { total: 3, pending: 1, in_progress: 0, done: 1, no_boom: 1 },
  pipeline_counts: { triage: 2, sam3: 1 },
  pipeline_status_counts: { "triage:pending": 2, "sam3:ready": 1 },
  videos: [
    video("aaa", "a", "pending"),
    video("bbb", "b", "done"),
    // Terceiro card travado por outra pessoa: cobre o caminho do selo de trava,
    // que só aparece quando `lock` vem preenchido.
    video("ccc", "c", "pending", { lock: { user: "Ana", since: "2026-01-01T00:00:00-03:00" } }),
  ],
};

const sam3 = (state, extra = {}) => ({
  state,
  progress: { segments_done: 1, segments_total: 3, frames_done: 18 },
  attempts: 1,
  error: null,
  segments: 3,
  finished_at: null,
  ...extra,
});

// Mesma biblioteca, com os quatro estados visíveis da fila do SAM3.
const VIDEOS_SAM3 = {
  ...VIDEOS,
  pipeline_counts: { sam3: 4 },
  pipeline_status_counts: { "sam3:running": 1, "sam3:queued": 1, "sam3:ready": 1, "sam3:error": 1 },
  videos: [
    video("aaa", "a", "done", { sam3: sam3("running"), pipeline_stage: "sam3", stage_status: "running" }),
    video("bbb", "b", "done", { sam3: sam3("queued"), pipeline_stage: "sam3", stage_status: "queued" }),
    video("ccc", "c", "done", { sam3: sam3("done"), pipeline_stage: "sam3", stage_status: "ready" }),
    video("ddd", "d", "done", { sam3: sam3("error", { error: "cuda_oom", attempts: 2 }), pipeline_stage: "sam3", stage_status: "error" }),
  ],
};

const ARCHIVED_OBJECT = {
  ...OBJECT,
  object_id: "arquivo",
  display_name: "Objeto arquivado",
  label: "archive_class",
  archived: true,
  gcs_ready: false,
};
const MIC_OBJECT = {
  ...OBJECT,
  object_id: "microfone",
  display_name: "Microfone",
  label: "microphone",
};

const SAM3_QUEUE = {
  object_id: "boom",
  counts: { running: 1, queued: 1, done: 1, error: 1 },
  active: true,
  videos: {
    aaa: sam3("running"),
    bbb: sam3("queued"),
    ccc: sam3("done"),
    ddd: sam3("error", { error: "cuda_oom", attempts: 2 }),
  },
};

const SEGMENT_REVIEW = {
  video_id: "aaa",
  name: "a",
  segment: "seg_00",
  segments: ["seg_00"],
  classes: ["boom"],
  label: "boom",
  prompt: {
    image_width: 3840,
    image_height: 2160,
    frame_idx: 7,
    flags: {},
    objects: [{ obj_id: 8, label: "boom", normalized: [0.11, 0.22, 0.33, 0.44] }],
  },
  frame_count: 12,
  reviewed: 3,
  edited: 1,
  with_objects: 10,
  complete: false,
  reviewed_by: null,
  frames: Array.from({ length: 12 }, (_, i) => ({
    frame: i,
    boxes: i < 10 ? [{ obj_id: 1, label: "boom", normalized: [0.3, 0.1, 0.45, 0.28] }] : [],
    status: i < 3 ? (i === 1 ? "edited" : "ok") : null,
  })),
};

const MASK_REVIEW = {
  frame: 0,
  revision: 2,
  status: "edited",
  instances: [
    {
      obj_id: 1,
      label: "boom",
      mask_url: "/api/mask/1.png?revision=2",
      bbox: [0.3, 0.1, 0.45, 0.28],
      area_pixels: 900,
      sha256: "abc",
    },
  ],
};

const MASK_REVIEW_FLOW = {
  ...SEGMENT_REVIEW,
  frame_count: 2,
  reviewed: 0,
  edited: 0,
  with_objects: 2,
  frames: Array.from({ length: 2 }, (_, frame) => ({
    frame,
    boxes: [{ obj_id: 1, label: "boom", normalized: [0.3, 0.1, 0.45, 0.28] }],
    // Simula o review.json legado já aprovado. A resposta individual de
    // mask-review continua status=null e deve ser a autoridade para o PUT.
    status: "ok",
  })),
};

const EXCLUSIONS = { exclude_patterns: [], excluded: {}, trash_dir: "/videos/../_trash" };
const TRIAGE_VIDEOS = {
  ...VIDEOS,
  total: 2,
  counts: { total: 2, pending: 2, in_progress: 0, done: 0, no_boom: 0 },
  pipeline_counts: { triage: 2 },
  pipeline_status_counts: { "triage:pending": 2 },
  videos: [video("aaa", "primeiro", "pending"), video("eee", "proximo", "pending")],
};
const VIDEO_MEDIA = {
  duration_sec: 4,
  width: 640,
  height: 360,
  coded_width: 640,
  coded_height: 360,
  rotation: 0,
  codec: "h264",
  pix_fmt: "yuv420p",
  avg_frame_rate: "24/1",
  r_frame_rate: "24/1",
  fps: 24,
  start_time_sec: 0,
  container_nb_frames: 96,
  frame_count: 96,
  frame_count_source: "proxy_extraction",
  frame_count_exact: true,
  is_vfr_suspect: false,
  browser_playable: true,
};
const VIDEO_META = {
  video_id: "aaa",
  object_id: "boom",
  label: "boom",
  relpath: "primeiro.mp4",
  name: "primeiro",
  size_bytes: 1,
  file_mtime: "2026-01-01T00:00:00-03:00",
  media: VIDEO_MEDIA,
  stream_url: "/stream",
};
const TRIAGE_INTERVAL = {
  index: 0,
  segment: "seg_00",
  start_frame: 10,
  end_frame: 19,
  frame_count: 10,
  start_time_sec: 10 / 24,
  end_time_sec: 19 / 24,
  prompt_frame: 10,
  bboxes: [{ obj_id: 1, label: "boom", normalized: [0.1, 0.2, 0.3, 0.4] }],
  flags: { dificuldade: "facil" },
  notes: "",
};
const TRIAGE_ANNOTATION = {
  video_id: "aaa",
  relpath: "primeiro.mp4",
  name: "primeiro",
  status: "in_progress",
  notes: "",
  intervals: [TRIAGE_INTERVAL],
  exported_at: null,
  export: null,
  suggested_flags: {},
};
const PROXY_STATUS = {
  mode: "full",
  complete: true,
  frame_count: 96,
  available_ranges: [[0, 95]],
  jobs: [],
};
const WINDOW_PROXY_STATUS = {
  mode: "window",
  complete: false,
  frame_count: 96,
  available_ranges: [],
  jobs: [],
};

/**
 * Cada cenário = um estado de /api/config + o que se espera ver na tela.
 * A tela é consequência do config, então isto cobre o roteamento do App também.
 */
const SCENARIOS = {
  "global-export": {
    url: "/export",
    config: { ...CONFIG, objects: [OBJECT, MIC_OBJECT] },
    globalPreview: {
      segments: 8,
      videos: 3,
      frames: 42,
      frames_with_objects: 39,
      frames_reviewed: 42,
      classes: [
        { id: 0, object_id: "boom", name: "boom" },
        { id: 1, object_id: "microfone", name: "microphone" },
      ],
      by_class: [],
      by_flag: { dificuldade: { facil: 30, dificil: 12 } },
      task: "segmentation",
      completed_only: true,
      export_allowed: false,
      blocking_reasons: ["microfone: máscara inválida no frame 7"],
    },
    expect: [
      ["Exportar datasets", "construtor global abriu"],
      ["Somente concluídos", "elegibilidade concluída é fixa"],
      ["Boom", "primeiro objeto pode ser selecionado"],
      ["Microfone", "segundo objeto pode ser selecionado"],
      ["0 · boom", "classe zero é determinística"],
      ["1 · microphone", "classe um é determinística"],
      ["42", "preview mostra o total de frames"],
      ["Tags e vídeos", "filtros gerais estão disponíveis"],
      ["máscara inválida", "bloqueio do preview é explicado"],
    ],
    disabledButton: "Exportar dataset",
  },
  "object-management": {
    url: "/objects",
    config: { ...CONFIG, last_object_id: null },
    objects: { objects: [OBJECT, ARCHIVED_OBJECT] },
    action: "open-purge",
    expect: [
      ["Objetos e classes", "administração de objetos abriu"],
      ["Nome de exibição", "nome do objeto pode ser editado"],
      ["Classe no dataset", "classe do objeto pode ser renomeada"],
      ["Arquivados", "filtro de arquivados existe"],
      ["Restaurar", "objeto arquivado pode ser restaurado"],
      ["EXCLUIR arquivo", "confirmação destrutiva exige o object_id exato"],
    ],
    disabledButton: "Excluir agora",
  },
  "archive-button": {
    url: "/objects",
    config: { ...CONFIG, last_object_id: null },
    objects: { objects: [OBJECT] },
    expect: [["Arquivar objeto", "arquivo permanece uma acao visivel"]],
    html: [["border-red-900", "arquivo e destacado como acao destrutiva"]],
  },
  overview: {
    url: "/objects/boom/overview",
    config: CONFIG,
    videos: {
      ...VIDEOS,
      total: 23,
      pipeline_counts: { triage: 2, sam3: 3, review: 4, completed: 5, discarded: 9 },
      pipeline_status_counts: {},
    },
    expect: [
      ["Visão operacional", "visão geral abriu pela URL"],
      ["Triagem2", "card de triagem usa a contagem autoritativa"],
      ["SAM33", "card SAM3 usa a contagem autoritativa"],
      ["Revisão4", "card de revisão usa a contagem autoritativa"],
      ["Concluídos5", "descartados não são somados aos concluídos"],
      ["Continuar próximo trabalho", "atalho operacional está disponível"],
    ],
    reject: [["Concluídos14", "concluídos não incluem descartados"]],
  },
  operations: {
    url: "/objects/boom/review",
    config: CONFIG,
    videos: {
      ...VIDEOS,
      pipeline_counts: { review: 2 },
      videos: [
        video("rev-a", "aguardando", "done", {
          pipeline_stage: "review",
          stage_status: "waiting",
          stage_progress: { expected_frames: 12, reviewed_frames: 0, edited_frames: 0, artifacts_valid: true, inconsistencies: [] },
        }),
        video("rev-b", "em-progresso", "done", {
          pipeline_stage: "review",
          stage_status: "in_progress",
          stage_progress: { expected_frames: 12, reviewed_frames: 4, edited_frames: 1, artifacts_valid: true, inconsistencies: [] },
        }),
      ],
    },
    expect: [
      ["Revisão de máscaras", "board de revisão abriu pela URL"],
      ["Aguardando", "filtro de aguardando estÃ¡ visÃ­vel"],
      ["Em andamento", "filtro de trabalho iniciado estÃ¡ visÃ­vel"],
    ],
  },
  "completed-gate": {
    url: "/objects/boom/completed",
    config: CONFIG,
    videos: {
      ...VIDEOS,
      pipeline_counts: { sam3: 1, completed: 1 },
      videos: [
        video("triaged", "apenas-triado", "done", {
          pipeline_stage: "sam3",
          stage_status: "ready",
        }),
        video("validated", "validado-final", "done", {
          pipeline_stage: "completed",
          stage_status: "validated",
          stage_progress: { expected_frames: 7, reviewed_frames: 7, edited_frames: 1, artifacts_valid: true, inconsistencies: [] },
        }),
      ],
    },
    expect: [
      ["validado-final", "vÃ­deo integralmente revisado aparece em ConcluÃ­dos"],
    ],
    reject: [
      ["apenas-triado", "vÃ­deo apenas triado nÃ£o aparece em ConcluÃ­dos"],
    ],
  },
  library: {
    url: "/objects/boom/triage",
    config: CONFIG,
    expect: [
      ["Boom", "cabeçalho do objeto ativo renderizou"],
      ["Triagem de vídeos", "board de triagem renderizou"],
      ["Ana está triando", "card travado mostra quem está com o vídeo"],
      ["Guilherme", "usuário logado aparece no cabeçalho"],
    ],
  },
  "triage-submit-next": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    action: "submit-triage",
    expectPath: "/objects/boom/videos/eee/triage",
    expect: [["Salvar, enviar ao SAM3 e próximo", "ação descreve o fluxo real"]],
    expectedRequests: [
      [/\/api\/objects\/boom\/videos\/aaa\/export$/, "exportação foi enfileirada"],
    ],
  },
  "triage-window-bootstrap": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    videoMeta: { ...VIDEO_META, media: { ...VIDEO_MEDIA, browser_playable: false } },
    proxyStatus: (() => {
      let calls = 0;
      return () => (++calls === 1
        ? WINDOW_PROXY_STATUS
        : { ...WINDOW_PROXY_STATUS, available_ranges: [[0, 95]] });
    })(),
    proxyStart: { mode: "window", job_id: null, total_frames: 96, already_complete: false },
    windowStart: { job_id: "window-job-1", start: 0, end: 95, already_available: false },
    terminalJobs: { "window-job-1": "done" },
    action: "triage-window-bootstrap",
  },
  "triage-full-frame-fallback": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    videoMeta: { ...VIDEO_META, media: { ...VIDEO_MEDIA, browser_playable: false } },
    proxyStatus: PROXY_STATUS,
    proxyStart: { mode: "full", job_id: null, total_frames: 96, already_complete: true },
    repairStart: { mode: "full", job_id: "repair-job-1", total_frames: 96, already_complete: false },
    terminalJobs: { "repair-job-1": "done" },
    action: "triage-full-frame-fallback",
  },
  "triage-window-covered-missing": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    videoMeta: { ...VIDEO_META, media: { ...VIDEO_MEDIA, browser_playable: false } },
    proxyStatus: { ...WINDOW_PROXY_STATUS, available_ranges: [[0, 95]] },
    proxyStart: { mode: "window", job_id: null, total_frames: 96, already_complete: false },
    windowStart: { job_id: "window-repair-1", start: 0, end: 95, already_available: false },
    terminalJobs: { "window-repair-1": "done" },
    action: "triage-frame-recovery-success",
    expectedRepairKind: "window",
  },
  "triage-full-incomplete-missing": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    videoMeta: { ...VIDEO_META, media: { ...VIDEO_MEDIA, browser_playable: false } },
    proxyStatus: (() => {
      let calls = 0;
      return () => (++calls === 1
        ? { ...PROXY_STATUS, complete: false, available_ranges: [], jobs: [] }
        : PROXY_STATUS);
    })(),
    proxyStart: { mode: "full", job_id: null, total_frames: 96, already_complete: false },
    repairStart: { mode: "full", job_id: "full-repair-1", total_frames: 96, already_complete: false },
    terminalJobs: { "full-repair-1": "done" },
    action: "triage-frame-recovery-success",
    expectedRepairKind: "full",
  },
  "triage-repair-no-job": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    videoMeta: { ...VIDEO_META, media: { ...VIDEO_MEDIA, browser_playable: false } },
    proxyStatus: PROXY_STATUS,
    proxyStart: { mode: "full", job_id: null, total_frames: 96, already_complete: true },
    repairStart: { mode: "full", job_id: null, total_frames: 96, already_complete: true },
    action: "triage-frame-recovery-failure",
    expectedFailure: "reparo não iniciou",
  },
  "triage-repair-error": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    videoMeta: { ...VIDEO_META, media: { ...VIDEO_MEDIA, browser_playable: false } },
    proxyStatus: PROXY_STATUS,
    proxyStart: { mode: "full", job_id: null, total_frames: 96, already_complete: true },
    repairStart: { mode: "full", job_id: "repair-error-1", total_frames: 96, already_complete: false },
    terminalJobs: { "repair-error-1": "error" },
    terminalJobErrors: { "repair-error-1": "ffmpeg falhou" },
    action: "triage-frame-recovery-failure",
    expectedFailure: "ffmpeg falhou",
  },
  "triage-repair-cancelled": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    videoMeta: { ...VIDEO_META, media: { ...VIDEO_MEDIA, browser_playable: false } },
    proxyStatus: PROXY_STATUS,
    proxyStart: { mode: "full", job_id: null, total_frames: 96, already_complete: true },
    repairStart: { mode: "full", job_id: "repair-cancel-1", total_frames: 96, already_complete: false },
    terminalJobs: { "repair-cancel-1": "cancelled" },
    action: "triage-frame-recovery-failure",
    expectedFailure: "cancelado",
  },
  "triage-sse-poll-failure": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    videoMeta: { ...VIDEO_META, media: { ...VIDEO_MEDIA, browser_playable: false } },
    proxyStatus: PROXY_STATUS,
    proxyStart: { mode: "full", job_id: null, total_frames: 96, already_complete: true },
    repairStart: { mode: "full", job_id: "repair-poll-fail", total_frames: 96, already_complete: false },
    sseFailureJobs: ["repair-poll-fail"],
    failJobPoll: ["repair-poll-fail"],
    fastPoll: true,
    action: "triage-frame-recovery-failure",
    expectedFailure: "acompanhar",
  },
  "triage-sse-close": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    videoMeta: { ...VIDEO_META, media: { ...VIDEO_MEDIA, browser_playable: false } },
    proxyStatus: PROXY_STATUS,
    proxyStart: { mode: "full", job_id: null, total_frames: 96, already_complete: true },
    repairStart: { mode: "full", job_id: "repair-poll-running", total_frames: 96, already_complete: false },
    sseFailureJobs: ["repair-poll-running"],
    pollingJobs: { "repair-poll-running": "running" },
    fastPoll: true,
    action: "triage-sse-close",
  },
  "triage-close-reopen-flight": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    videoMeta: { ...VIDEO_META, media: { ...VIDEO_MEDIA, browser_playable: false } },
    proxyStatus: PROXY_STATUS,
    proxyStart: { mode: "full", job_id: null, total_frames: 96, already_complete: true },
    repairStart: (() => {
      let calls = 0;
      return () => ({ mode: "full", job_id: `repair-reopen-${++calls}`, total_frames: 96, already_complete: false });
    })(),
    holdFirstRepairStart: true,
    terminalJobs: { "repair-reopen-1": "done", "repair-reopen-2": "done" },
    action: "triage-close-reopen-flight",
  },
  "triage-keyboard-recovery-failure": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    videoMeta: { ...VIDEO_META, media: { ...VIDEO_MEDIA, browser_playable: false } },
    proxyStatus: { ...WINDOW_PROXY_STATUS, available_ranges: [[0, 95]] },
    proxyStart: { mode: "window", job_id: null, total_frames: 96, already_complete: false },
    windowStart: { job_id: "keyboard-repair-error", start: 0, end: 95, already_available: false },
    terminalJobs: { "keyboard-repair-error": "error" },
    terminalJobErrors: { "keyboard-repair-error": "falha no atalho" },
    action: "triage-keyboard-recovery-failure",
  },
  "triage-active-video-order": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    delayActiveVideoNull: true,
    action: "triage-active-video-order",
  },
  "triage-stale-open": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    initialWait: 80,
    delayMetaVideo: "aaa",
    annotation: (url) => url.includes("/eee")
      ? { ...TRIAGE_ANNOTATION, video_id: "eee", relpath: "proximo.mp4", name: "proximo", intervals: [] }
      : TRIAGE_ANNOTATION,
    action: "triage-stale-open",
    expectPath: "/objects/boom/videos/eee/triage",
    reject: [["10 → 19", "resposta atrasada do vídeo anterior não sobrescreve o novo"]],
  },
  "triage-submit-next-slow-refresh": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    delayAnnotationMutation: true,
    action: "submit-triage-slow-refresh",
    expectPath: "/objects/boom/videos/eee/triage",
  },
  "triage-submit-failure": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    failAnnotationSave: true,
    action: "submit-triage-failure",
    expectPath: "/objects/boom/videos/aaa/triage",
  },
  "triage-submit-last": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: { ...TRIAGE_VIDEOS, videos: [TRIAGE_VIDEOS.videos[0]] },
    action: "submit-triage",
    expectPath: "/objects/boom/triage",
    expect: [["Triagem de vídeos", "retornou ao painel quando não havia próximo"]],
    expectedRequests: [
      [/\/api\/objects\/boom\/videos\/aaa\/export$/, "exportação foi enfileirada"],
    ],
  },
  "triage-no-object-next": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    action: "mark-no-object",
    expectPath: "/objects/boom/videos/eee/triage",
    expect: [["Sem objeto", "ação usa o nome genérico"]],
    reject: [["sem boom", "texto específico do objeto foi removido"]],
    expectedRequests: [
      [/\/api\/objects\/boom\/annotations\/aaa\/no-object$/, "status sem objeto foi persistido"],
    ],
  },
  "triage-no-object-next-slow-refresh": {
    url: "/objects/boom/videos/aaa/triage",
    config: CONFIG,
    videos: TRIAGE_VIDEOS,
    delayAnnotationMutation: true,
    action: "mark-no-object-slow-refresh",
    expectPath: "/objects/boom/videos/eee/triage",
  },
  login: {
    config: { ...CONFIG, user: null },
    expect: [
      ["Quem está triando", "tela de login renderizou"],
      ["Guilherme", "usuário existente listado"],
      ["Novo contribuidor", "cadastro de usuário disponível"],
    ],
  },
  objects: {
    // Sem last_object_id e sem objeto lembrado: cai no seletor de objetos.
    config: { ...CONFIG, last_object_id: null },
    expect: [
      ["O que vamos anotar", "seletor de objetos renderizou"],
      ["Boom", "objeto listado"],
      ["novo objeto", "criação de objeto disponível"],
    ],
  },
  workspace: {
    config: { ...CONFIG, workspace_ready: false, workspace_root: null },
    expect: [
      ["Onde fica o workspace", "escolha do workspace renderizou"],
      ["Usar esta pasta", "botão de confirmação renderizou"],
    ],
  },
  sam3: {
    url: "/objects/boom/sam3",
    config: CONFIG,
    videos: VIDEOS_SAM3,
    expect: [
      ["Processando", "estado de processamento renderizou"],
      ["Na fila", "estado de espera renderizou"],
      ["Pronto para propagar", "estado pronto renderizou"],
      ["Com erro", "estado de erro renderizou"],
      ["18 frames", "progresso por frame renderiza no card"],
      ["tentativa 2", "contador de tentativas aparece no erro"],
      ["Configurar e propagar", "ação primária do SAM3 está sempre visível"],
    ],
  },
  "sam3-prompt": {
    url: "/objects/boom/videos/ccc/sam3",
    config: CONFIG,
    videos: VIDEOS_SAM3,
    expect: [
      ["#8", "bbox original do prompt foi carregada"],
      ["0.110, 0.220, 0.330, 0.440", "coordenadas do prompt aparecem na tela"],
    ],
    html: [["/frames/7.jpg", "frame local do prompt é exibido"]],
  },
  "sam3-propagation-refresh": {
    url: "/objects/boom/videos/ccc/sam3",
    config: CONFIG,
    videos: VIDEOS_SAM3,
    action: "propagate-sam3",
    expectedRequests: [
      [/\/api\/objects\/boom\/videos\?sort=name$/, "a biblioteca é atualizada logo após enfileirar"],
    ],
  },
  "mask-review": {
    url: "/objects/boom/videos/aaa/review",
    config: CONFIG,
    videos: VIDEOS_SAM3,
    segmentReview: MASK_REVIEW_FLOW,
    action: "review-walk-save",
    expect: [
      ["Pincel", "editor de mascara foi carregado"],
      ["Borracha", "ferramenta de borracha esta disponivel"],
      ["Mover", "ferramenta de navegacao esta disponivel"],
      ["Formato", "formato do pincel pode ser escolhido"],
      ["Circular", "pincel circular esta disponivel"],
      ["Ocultar máscara", "mascara pode ser escondida temporariamente"],
      ["Ajustar à tela", "viewport pode ser restaurado"],
      ["Salvar trecho", "acao global de salvamento esta sempre disponivel"],
      ["Sem objeto", "estado vazio explicito esta disponivel"],
      ["BBox derivada da máscara", "bbox da mascara e sempre exibida"],
    ],
    reject: [
      ["Aprovar original", "aprovacao manual por frame foi removida"],
      ["Salvar frame", "salvamento manual por frame foi removido"],
    ],
    expectedRequests: [
      [/\/segments\/seg_00\/mask-review$/, "o trecho foi persistido em lote"],
    ],
    assertReviewControlsOutsideFrame: true,
    html: [["opacity: 0.3", "mascara inicia com transparencia maior"]],
  },
};

const scenarioName = process.argv[2] ?? "library";
const scenario = SCENARIOS[scenarioName];
if (!scenario) {
  console.error(`cenário desconhecido: ${scenarioName}`);
  console.error(`disponíveis: ${Object.keys(SCENARIOS).join(", ")}`);
  process.exit(2);
}

const routes = [
  [/\/api\/config$/, scenario.config],
  [/\/api\/objects\?include_archived=true$/, scenario.objects ?? { objects: [OBJECT] }],
  [/\/api\/objects$/, scenario.objects ?? { objects: [OBJECT] }],
  [/\/api\/users$/, { users: [USER] }],
  // Antes da rota de vídeos: /sam3 é sufixo do mesmo prefixo e seria capturado
  // pelo padrão mais largo.
  [/\/api\/objects\/[^/]+\/sam3$/, SAM3_QUEUE],
  [/\/api\/objects\/[^/]+\/videos\/[^/]+\/sam3\/override$/, { ok: true }],
  [/\/api\/objects\/[^/]+\/videos\/[^/]+\/sam3$/, { state: "queued" }],
  [/\/api\/objects\/[^/]+\/session\/active-video$/, { cancelled: 0, lock: null }],
  [/\/api\/objects\/[^/]+\/lock\/release$/, { released: true }],
  [/\/api\/objects\/[^/]+\/lock$/, { lock: { user: "Guilherme", since: "2026-01-01T00:00:00-03:00" }, heartbeat_seconds: 30, ttl_seconds: 90 }],
  [/\/api\/objects\/[^/]+\/annotations\/[^/]+\/no-object$/, { ...TRIAGE_ANNOTATION, status: "no_boom", intervals: [] }],
  [/\/api\/objects\/[^/]+\/annotations\/[^/]+$/, scenario.annotation ?? TRIAGE_ANNOTATION],
  [/\/api\/objects\/[^/]+\/videos\/[^/]+\/meta$/, scenario.videoMeta ?? VIDEO_META],
  [/\/api\/objects\/[^/]+\/videos\/[^/]+\/proxy\/status$/, scenario.proxyStatus ?? PROXY_STATUS],
  [/\/api\/objects\/[^/]+\/videos\/[^/]+\/window$/, scenario.windowStart ?? { job_id: null, start: 0, end: 95, already_available: true }],
  [/\/api\/objects\/[^/]+\/videos\/[^/]+\/proxy$/, scenario.proxyStart ?? { mode: "full", job_id: null, total_frames: 96, already_complete: true }],
  [/\/api\/objects\/[^/]+\/videos\/[^/]+\/export$/, { job_id: "export-job-1", total: 10 }],
  [/\/api\/datasets\/preview$/, scenario.globalPreview ?? { export_allowed: false, blocking_reasons: [] }],
  [/\/api\/datasets$/, { datasets: [] }],
  [/\/api\/objects\/[^/]+\/videos\/[^/]+\/sam3\/session/, { state: "none" }],
  [/\/videos\/[^/]+\/review$/, { ...SEGMENT_REVIEW, complete: false, segments: [{ segment: "seg_00", complete: false }] }],
  [/\/segments\/[^/]+\/review$/, scenario.segmentReview ?? SEGMENT_REVIEW],
  [/\/segments\/[^/]+\/mask-review$/, { frames: [], reviewed: 2, frame_count: 2, complete: true }],
  [/\/segments\/[^/]+\/mask-review\/\d+$/, (url) => ({
    ...MASK_REVIEW,
    frame: Number(url.match(/mask-review\/(\d+)/)?.[1] ?? 0),
    revision: 0,
    status: null,
  })],
  [/\/api\/objects\/[^/]+\/videos(\?|$)/, scenario.videos ?? VIDEOS],
  [/\/api\/objects\/[^/]+\/exclusions$/, EXCLUSIONS],
  [/\/api\/fs\/list/, { path: "/ws", parent: "/", dirs: [], video_count: 0 }],
];

const dom = new JSDOM(
  `<!doctype html><html><body><div id="root"></div></body></html>`,
  { url: `http://localhost:8090${scenario.url ?? "/"}`, pretendToBeVisual: true, runScripts: "dangerously" },
);

const { window } = dom;
const nativeSetInterval = globalThis.setInterval.bind(globalThis);
const requests = [];
const requestLog = [];
const eventLog = [];
const jobPolls = new Map();
const eventSourceCloses = new Map();
const unhandledRejections = [];
let frameStageMounts = 0;
let maskFrameRequests = 0;
let videosRequests = 0;
let mutationAcked = false;
let navigatedBeforeMutationAck = false;
let recoveryOverlayBeforeLoad = false;
let recoveryOverlayDuringFallback = false;
let recoveryOverlayAfterLoad = false;
let pollsWhenClosed = 0;
let repairsWhileFirstPending = 0;
let releaseFirstRepairStart = () => undefined;
const firstRepairStartGate = new Promise((resolve) => {
  releaseFirstRepairStart = resolve;
});
window.confirm = () => true;
const originalPushState = window.history.pushState.bind(window.history);
window.history.pushState = (...args) => {
  eventLog.push(`navigate:${String(args[2] ?? "")}`);
  return originalPushState(...args);
};
process.on("unhandledRejection", (reason) => {
  unhandledRejections.push(String(reason instanceof Error ? reason.message : reason));
});

window.fetch = async (input, init = {}) => {
  const url = String(input);
  requests.push(url);
  requestLog.push({ url, method: init.method ?? "GET", body: init.body });
  if (init.method === "POST" && /\/session\/active-video$/.test(url)) {
    const activeVideo = JSON.parse(init.body ?? "{}").video_id ?? null;
    if (activeVideo === null && scenario.delayActiveVideoNull) {
      await new Promise((resolve) => setTimeout(resolve, 160));
    }
    eventLog.push(`active:${activeVideo ?? "null"}:ack`);
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => ({ cancelled: 0, lock: null }),
    };
  }
  if (init.method === "POST" && /\/lock\/release$/.test(url)) {
    eventLog.push("release:ack");
  } else if (init.method === "POST" && /\/lock$/.test(url)) {
    eventLog.push(`acquire:${JSON.parse(init.body ?? "{}").video_id}:ack`);
  }
  if (scenario.delayMetaVideo && url.includes(`/videos/${scenario.delayMetaVideo}/meta`)) {
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
  if (
    scenario.delayAnnotationMutation &&
    ["PUT", "POST"].includes(init.method) &&
    /\/api\/objects\/[^/]+\/annotations\/[^/]+(?:\/no-object)?$/.test(url)
  ) {
    await new Promise((resolve) => setTimeout(resolve, 120));
    mutationAcked = true;
  }
  if (/\/api\/objects\/[^/]+\/videos(\?|$)/.test(url)) {
    videosRequests += 1;
    if (videosRequests > 1 && scenario.action?.includes("slow-refresh")) {
      await new Promise((resolve) => setTimeout(resolve, 900));
    }
  }
  if (
    scenario.failAnnotationSave &&
    init.method === "PUT" &&
    /\/api\/objects\/[^/]+\/annotations\/[^/]+$/.test(url)
  ) {
    return {
      ok: false,
      status: 500,
      statusText: "save failed",
      json: async () => ({ detail: "falha controlada ao salvar" }),
    };
  }
  if (init.method === "POST" && /\/proxy$/.test(url) && JSON.parse(init.body ?? "{}").force) {
    const repairNumber = requestLog.filter(
      (request) => request.method === "POST" && /\/proxy$/.test(request.url),
    ).length;
    if (scenario.holdFirstRepairStart && repairNumber === 1) {
      await firstRepairStartGate;
    }
    const configured = scenario.repairStart ?? scenario.proxyStart;
    const result = typeof configured === "function" ? configured(url) : configured;
    return { ok: true, status: 200, statusText: "OK", json: async () => result };
  }
  const jobId = url.match(/\/api\/jobs\/([^/?]+)/)?.[1];
  if (jobId && (init.method ?? "GET") === "GET") {
    jobPolls.set(jobId, (jobPolls.get(jobId) ?? 0) + 1);
    if (scenario.failJobPoll?.includes(jobId)) {
      return {
        ok: false,
        status: 503,
        statusText: "Service Unavailable",
        json: async () => ({ detail: "falha ao acompanhar o job" }),
      };
    }
    const state = scenario.pollingJobs?.[jobId] ?? scenario.terminalJobs?.[jobId] ?? "running";
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => ({
        job_id: jobId,
        kind: "proxy_full",
        object_id: "boom",
        video_id: "aaa",
        state,
        current: state === "running" ? 1 : 96,
        total: 96,
        progress: state === "running" ? 0.01 : 1,
        message: "acompanhando",
        error: state === "error" ? (scenario.terminalJobErrors?.[jobId] ?? "job falhou") : null,
        result: {},
        queue_pos: 0,
        user: null,
      }),
    };
  }
  if (scenario.action === "review-walk-save" && /\/mask-review\/\d+$/.test(url)) {
    maskFrameRequests += 1;
    if (maskFrameRequests === 2) await new Promise((resolve) => setTimeout(resolve, 120));
  }
  const match = routes.find(([pattern]) => pattern.test(url));
  if (match && ["PUT", "POST"].includes(init.method) && /\/annotations\/aaa$/.test(url)) {
    eventLog.push("mutation:ack");
  }
  if (match && init.method === "POST" && /\/videos\/aaa\/export$/.test(url)) {
    eventLog.push("export:ack");
  }
  return {
    ok: Boolean(match),
    status: match ? 200 : 404,
    statusText: match ? "OK" : "Not Found",
    json: async () => typeof match?.[1] === "function" ? match[1](url) : match?.[1] ?? { detail: "not found" },
  };
};

window.EventSource = class {
  constructor(url) {
    this.url = String(url);
    this.jobId = this.url.match(/\/api\/jobs\/([^/]+)\/events/)?.[1];
    const jobId = this.jobId;
    if (scenario.sseFailureJobs?.includes(jobId)) {
      setTimeout(() => this.onerror?.(new window.Event("error")), 10);
      return;
    }
    const state = scenario.terminalJobs?.[jobId];
    if (state) {
      setTimeout(() => this.onmessage?.({ data: JSON.stringify({
        job_id: jobId,
        kind: jobId.startsWith("repair") ? "proxy_full" : "proxy_window",
        state,
        current: 96,
        total: 96,
        progress: 1,
        message: "pronto",
        error: state === "error" ? (scenario.terminalJobErrors?.[jobId] ?? "job falhou") : null,
        result: {},
        queue_pos: 0,
        user: null,
      }) }), 25);
    }
  }
  close() {
    eventSourceCloses.set(this.jobId, (eventSourceCloses.get(this.jobId) ?? 0) + 1);
  }
};
window.ResizeObserver = class {
  observe() {}
  disconnect() {}
};
window.HTMLElement.prototype.scrollTo = function scrollTo() {};
Object.defineProperties(window.HTMLImageElement.prototype, {
  naturalWidth: { configurable: true, get: () => 640 },
  naturalHeight: { configurable: true, get: () => 360 },
  clientWidth: { configurable: true, get: () => 640 },
  clientHeight: { configurable: true, get: () => 360 },
});
const stageObserver = new window.MutationObserver((records) => {
  for (const record of records) {
    for (const node of record.addedNodes) {
      if (!(node instanceof window.Element)) continue;
      if (node.matches?.('img[alt^="frame "]')) frameStageMounts += 1;
      frameStageMounts += node.querySelectorAll?.('img[alt^="frame "]').length ?? 0;
    }
  }
});
stageObserver.observe(window.document.getElementById("root"), { childList: true, subtree: true });

// O loop de render do React estoura como exceção não-capturada e mataria o
// processo antes das checagens; converte num relatório legível.
process.on("uncaughtException", (error) => {
  console.log(`[ FALHA] React quebrou na montagem — ${error.message.split("\n")[0]}`);
  if (/#185|#310|Maximum update depth|getSnapshot/.test(error.message)) {
    console.log(
      "         Sintoma: TELA PRETA. Causa provável: seletor de zustand devolvendo\n" +
        "         objeto/array novo a cada chamada. Assine o campo cru e derive com useMemo.",
    );
  }
  console.log("\nSMOKE FALHOU");
  process.exit(1);
});

const errors = [];
window.addEventListener("error", (event) => errors.push(String(event.error ?? event.message)));
const originalError = window.console.error;
window.console.error = (...args) => {
  errors.push(args.map(String).join(" "));
  originalError.apply(window.console, args);
};

// Expõe tudo o que o jsdom oferece: o bundle toca em várias APIs de DOM logo no
// preâmbulo do Vite (MutationObserver, entre outras), e listar uma a uma só gera
// falhas em cascata.
for (const key of Object.getOwnPropertyNames(window)) {
  if (globalThis[key] === undefined) {
    try {
      globalThis[key] = window[key];
    } catch {
      /* propriedades só-leitura do jsdom */
    }
  }
}
globalThis.window = window;
globalThis.document = window.document;
// O Node 24 já tem `fetch` global, então o bulk-copy acima não sobrescreve — sem
// isto o bundle tentaria a rede de verdade com URL relativa e falharia.
globalThis.fetch = window.fetch;
globalThis.EventSource = window.EventSource;
globalThis.ResizeObserver = window.ResizeObserver;
if (scenario.fastPoll) {
  const fastSetInterval = (callback, delay, ...args) =>
    nativeSetInterval(callback, Math.min(Number(delay) || 0, 20), ...args);
  globalThis.setInterval = fastSetInterval;
  window.setInterval = fastSetInterval;
}

const bundle = readdirSync(join(staticDir, "assets")).find(
  (file) => file.endsWith(".js"),
);
if (!bundle) {
  console.error("nenhum bundle em static/assets — rode `npm run build` antes");
  process.exit(2);
}

try {
  await import(pathToFileURL(join(staticDir, "assets", bundle)).href);
} catch (error) {
  // Sem isto o node despeja o bundle minificado inteiro no terminal.
  console.error(`[ FALHA] o bundle nem carregou — ${error.message}`);
  process.exit(1);
}
await new Promise((resolve) => setTimeout(resolve, scenario.initialWait ?? 600));

if (scenario.action === "open-sam3") {
  const action = [...window.document.querySelectorAll('[role="button"]')].find(
    (element) => element.textContent?.trim() === "SAM3",
  );
  action?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 600));
}
if (scenario.action === "open-review") {
  const action = [...window.document.querySelectorAll('[role="button"]')].find(
    (element) => element.textContent?.trim() === "revisar",
  );
  action?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 600));
  window.document.querySelector("img")?.dispatchEvent(new window.Event("load"));
  await new Promise((resolve) => setTimeout(resolve, 100));
}
if (scenario.action === "open-purge") {
  const archivedTab = [...window.document.querySelectorAll("button")].find(
    (element) => element.textContent?.trim() === "Arquivados",
  );
  archivedTab?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 50));
  const archivedObject = [...window.document.querySelectorAll("button")].find(
    (element) => element.textContent?.includes("Objeto arquivado"),
  );
  archivedObject?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 50));
  const action = [...window.document.querySelectorAll("button")].find(
    (element) => element.textContent?.trim() === "Excluir permanentemente",
  );
  action?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 100));
}
if (scenario.action === "propagate-sam3") {
  requests.length = 0;
  const action = [...window.document.querySelectorAll("button")].find(
    (element) => element.textContent?.trim() === "propagar para o trecho",
  );
  action?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 150));
}
if (scenario.action === "submit-triage") {
  requests.length = 0;
  const action = [...window.document.querySelectorAll("button")].find(
    (element) => element.textContent?.includes("salvar + exportar + próximo") || element.textContent?.includes("Salvar, enviar ao SAM3 e próximo"),
  );
  action?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 40));
  navigatedBeforeMutationAck = !mutationAcked && !window.location.pathname.includes("/aaa/");
  await new Promise((resolve) => setTimeout(resolve, 210));
}
if (scenario.action === "submit-triage-slow-refresh" || scenario.action === "submit-triage-failure") {
  requests.length = 0;
  requestLog.length = 0;
  eventLog.length = 0;
  const action = [...window.document.querySelectorAll("button")].find(
    (element) => element.textContent?.includes("Salvar, enviar ao SAM3"),
  );
  action?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 40));
  navigatedBeforeMutationAck = !mutationAcked && !window.location.pathname.includes("/aaa/");
  await new Promise((resolve) => setTimeout(resolve, 210));
}
if (scenario.action === "mark-no-object") {
  requests.length = 0;
  const action = [...window.document.querySelectorAll("button")].find(
    (element) => element.textContent?.toLowerCase().includes("sem boom") || element.textContent?.includes("Sem objeto"),
  );
  action?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 250));
}
if (scenario.action === "mark-no-object-slow-refresh") {
  requests.length = 0;
  requestLog.length = 0;
  const action = [...window.document.querySelectorAll("button")].find(
    (element) => element.textContent?.includes("Sem objeto"),
  );
  action?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 40));
  navigatedBeforeMutationAck = !mutationAcked && !window.location.pathname.includes("/aaa/");
  await new Promise((resolve) => setTimeout(resolve, 210));
}
if (scenario.action === "triage-full-frame-fallback") {
  requests.length = 0;
  requestLog.length = 0;
  const stage = window.document.querySelector('img[alt^="frame "]');
  stage?.dispatchEvent(new window.Event("error"));
  await new Promise((resolve) => setTimeout(resolve, 30));
  const fallback = window.document.querySelector('img[alt^="frame "]');
  if (!fallback?.getAttribute("src")?.includes("tier=small")) {
    errors.push("frame full ausente nao fez fallback para small");
  }
  fallback?.dispatchEvent(new window.Event("error"));
  fallback?.dispatchEvent(new window.Event("error"));
  await new Promise((resolve) => setTimeout(resolve, 150));
}
if (
  scenario.action === "triage-frame-recovery-success" ||
  scenario.action === "triage-frame-recovery-failure" ||
  scenario.action === "triage-sse-close" ||
  scenario.action === "triage-close-reopen-flight"
) {
  requests.length = 0;
  requestLog.length = 0;
  const failBothTiers = async () => {
    const full = window.document.querySelector('img[alt^="frame "]');
    full?.dispatchEvent(new window.Event("error"));
    await new Promise((resolve) => setTimeout(resolve, 5));
    const small = window.document.querySelector('img[alt^="frame "]');
    small?.dispatchEvent(new window.Event("error"));
  };
  await failBothTiers();

  if (scenario.action === "triage-frame-recovery-success") {
    await new Promise((resolve) => setTimeout(resolve, 100));
    recoveryOverlayBeforeLoad = (window.document.body.textContent ?? "").includes("extraindo os frames");
    window.document.querySelector('img[alt^="frame "]')?.dispatchEvent(new window.Event("error"));
    await new Promise((resolve) => setTimeout(resolve, 5));
    recoveryOverlayDuringFallback = (window.document.body.textContent ?? "").includes("extraindo os frames");
    window.document.querySelector('img[alt^="frame "]')?.dispatchEvent(new window.Event("load"));
    await new Promise((resolve) => setTimeout(resolve, 15));
    recoveryOverlayAfterLoad = (window.document.body.textContent ?? "").includes("extraindo os frames");
  } else if (scenario.action === "triage-frame-recovery-failure") {
    await new Promise((resolve) => setTimeout(resolve, scenario.fastPoll ? 120 : 100));
  } else if (scenario.action === "triage-sse-close") {
    await new Promise((resolve) => setTimeout(resolve, 80));
    const next = window.document.querySelector('button[title="próximo vídeo (PageDown)"]');
    next?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 50));
    pollsWhenClosed = jobPolls.get("repair-poll-running") ?? 0;
    await new Promise((resolve) => setTimeout(resolve, 100));
  } else {
    const next = window.document.querySelector('button[title="próximo vídeo (PageDown)"]');
    next?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 80));
    const previous = window.document.querySelector('button[title="vídeo anterior (PageUp)"]');
    previous?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 80));
    await failBothTiers();
    await new Promise((resolve) => setTimeout(resolve, 30));
    repairsWhileFirstPending = requestLog.filter(
      (request) => request.method === "POST" &&
        /\/videos\/aaa\/proxy$/.test(request.url) &&
        JSON.parse(request.body ?? "{}").force === true,
    ).length;
    releaseFirstRepairStart();
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}
if (scenario.action === "triage-keyboard-recovery-failure") {
  unhandledRejections.length = 0;
  window.dispatchEvent(new window.KeyboardEvent("keydown", { key: "i", bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 100));
}
if (scenario.action === "triage-active-video-order") {
  eventLog.length = 0;
  const next = window.document.querySelector('button[title="próximo vídeo (PageDown)"]');
  next?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 350));
}
if (scenario.action === "triage-stale-open") {
  requests.length = 0;
  requestLog.length = 0;
  const next = window.document.querySelector('button[title="próximo vídeo (PageDown)"]');
  next?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 900));
}
if (scenario.action === "review-walk-save") {
  requests.length = 0;
  window.dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 30));
  if (!(window.document.body.textContent ?? "").includes("Salvar trecho")) {
    errors.push("Salvar trecho desapareceu enquanto o proximo frame carregava");
  }
  await new Promise((resolve) => setTimeout(resolve, 140));
  const action = [...window.document.querySelectorAll("button")].find(
    (element) => element.textContent?.trim() === "Salvar trecho",
  );
  action?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 250));
}

const root = window.document.getElementById("root");
const html = root.innerHTML;
const text = root.textContent ?? "";

const fatal = errors.filter(
  (message) =>
    message.includes("getSnapshot") ||
    message.includes("Maximum update depth") ||
    message.includes("Minified React error") ||
    message.includes("infinite loop"),
);

let failed = false;
const check = (ok, label, detail = "") => {
  console.log(`[${ok ? "  ok  " : " FALHA"}] ${label}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failed = true;
};

console.log(`--- cenário: ${scenarioName} ---`);
check(html.length > 0, "#root não está vazio (tela preta)", `${html.length} bytes`);
check(fatal.length === 0, "sem loop de render / erro fatal do React", fatal[0] ?? "");
check(!text.includes("A interface quebrou"), "error boundary não disparou");
check(!text.includes("Não consegui falar"), "nenhuma chamada de API ficou sem mock");

const orphanedInputs = [...window.document.querySelectorAll("input, select, textarea")].filter((element) => {
  const labelledById = element.id && window.document.querySelector(`label[for="${element.id}"]`);
  const hasAria = element.getAttribute("aria-label") || element.getAttribute("aria-labelledby");
  return !labelledById && !hasAria && !element.closest("label");
});
const unnamedButtons = [...window.document.querySelectorAll("button")].filter(
  (element) => !(element.textContent ?? "").trim() && !element.getAttribute("aria-label") && !element.getAttribute("title"),
);
check(orphanedInputs.length === 0, "todos os campos têm nome acessível", orphanedInputs[0]?.outerHTML ?? "");
check(unnamedButtons.length === 0, "todos os botões têm nome acessível", unnamedButtons[0]?.outerHTML ?? "");

if (scenario.assertReviewControlsOutsideFrame) {
  const viewport = window.document.querySelector('[aria-label="Área do frame para revisão"]');
  const overlaidControl = viewport?.querySelector("button, input, select");
  check(Boolean(viewport), "área exclusiva do frame está identificada");
  check(
    !overlaidControl,
    "nenhum controle fica sobreposto ao frame",
    overlaidControl?.outerHTML ?? "",
  );
}

if (scenario.action === "triage-window-bootstrap") {
  const windowPosts = requestLog.filter(
    (request) => request.method === "POST" && /\/videos\/aaa\/window$/.test(request.url),
  );
  check(windowPosts.length === 1, "bootstrap de janela foi coalescido", `${windowPosts.length} POSTs`);
  check(frameStageMounts >= 2, "frame foi recarregado depois do job", `${frameStageMounts} montagens`);
}
if (scenario.action === "triage-full-frame-fallback") {
  const repairs = requestLog.filter(
    (request) => request.method === "POST" &&
      /\/videos\/aaa\/proxy$/.test(request.url) &&
      JSON.parse(request.body ?? "{}").force === true,
  );
  check(!errors.some((item) => item.includes("fallback para small")), "frame full cai para o tier small");
  check(repairs.length === 1, "falha dos dois tiers inicia um unico reparo", `${repairs.length} POSTs`);
}
if (scenario.action === "triage-frame-recovery-success") {
  const expectedPattern = scenario.expectedRepairKind === "window" ? /\/window$/ : /\/proxy$/;
  const repairRequests = requestLog.filter(
    (request) => request.method === "POST" && expectedPattern.test(request.url),
  );
  check(repairRequests.length === 1, "frame ausente inicia exatamente um reparo", `${repairRequests.length} POSTs`);
  if (scenario.expectedRepairKind === "window") {
    const body = JSON.parse(repairRequests[0]?.body ?? "{}");
    check(body.force === true, "janela anunciada mas ausente força reextração");
  }
  check(recoveryOverlayBeforeLoad, "reparo concluído mantém cobertura até o JPEG carregar");
  check(recoveryOverlayDuringFallback, "fallback após reparo não revela um palco preto");
  check(!recoveryOverlayAfterLoad, "onLoad remove a cobertura de recuperação");
}
if (scenario.action === "triage-frame-recovery-failure") {
  const currentText = window.document.body.textContent ?? "";
  check(currentText.includes(scenario.expectedFailure), "falha de recuperação é acionável", currentText.slice(-180));
  check(
    [...window.document.querySelectorAll("button")].some((button) => button.textContent?.includes("tentar novamente")),
    "falha oferece tentativa manual",
  );
}
if (scenario.action === "triage-sse-close") {
  const finalPolls = jobPolls.get("repair-poll-running") ?? 0;
  check(pollsWhenClosed > 0, "fallback por polling iniciou");
  check(finalPolls === pollsWhenClosed, "fechar a tela encerra polling do reparo", `${pollsWhenClosed} -> ${finalPolls}`);
}
if (scenario.action === "triage-close-reopen-flight") {
  const repairs = requestLog.filter(
    (request) => request.method === "POST" &&
      /\/videos\/aaa\/proxy$/.test(request.url) &&
      JSON.parse(request.body ?? "{}").force === true,
  );
  check(
    repairsWhileFirstPending === 2 && repairs.length === 2,
    "reabrir o mesmo vídeo não reutiliza flight da sessão fechada",
    `${repairsWhileFirstPending} pendentes; ${repairs.length} total`,
  );
}
if (scenario.action === "triage-keyboard-recovery-failure") {
  check(unhandledRejections.length === 0, "falha de recuperação pelo teclado é tratada", unhandledRejections[0] ?? "");
  check((window.document.body.textContent ?? "").includes("falha no atalho"), "erro do atalho permanece visível");
}
if (scenario.action === "triage-active-video-order") {
  const lifecycle = eventLog.filter((event) =>
    event.startsWith("active:") || event.startsWith("release:") || event.startsWith("acquire:"),
  );
  check(
    lifecycle[0] === "active:null:ack" &&
      lifecycle[1] === "release:ack" &&
      lifecycle[2] === "active:eee:ack" &&
      lifecycle[3] === "acquire:eee:ack",
    "desativação e release antigos terminam antes da nova ativação",
    lifecycle.join(" | "),
  );
}
if (scenario.action === "submit-triage-slow-refresh") {
  const saveAt = requestLog.findIndex(
    (request) => request.method === "PUT" && /\/annotations\/aaa$/.test(request.url),
  );
  const exportAt = requestLog.findIndex(
    (request) => request.method === "POST" && /\/videos\/aaa\/export$/.test(request.url),
  );
  check(saveAt >= 0 && exportAt > saveAt, "salvamento precede o enqueue da exportacao");
  check(
    eventLog[0] === "mutation:ack" &&
      eventLog[1] === "export:ack" &&
      eventLog[2]?.includes("/objects/boom/videos/eee/triage"),
    "ordem exata é ACK da mutação -> ACK do export -> navegação",
    eventLog.join(" | "),
  );
  check(!navigatedBeforeMutationAck, "navegação espera o ACK da mutação");
  check(window.location.pathname.includes("/eee/"), "navegacao nao espera refresh lento", window.location.pathname);
}
if (scenario.action === "mark-no-object-slow-refresh") {
  const mutationAt = requestLog.findIndex(
    (request) => request.method === "POST" && /\/annotations\/aaa\/no-object$/.test(request.url),
  );
  check(mutationAt >= 0, "sem objeto foi persistido antes da navegacao");
  check(!navigatedBeforeMutationAck, "sem objeto espera o ACK da mutação");
  check(window.location.pathname.includes("/eee/"), "sem objeto nao espera refresh lento", window.location.pathname);
}
if (scenario.action === "submit-triage-failure") {
  check(window.location.pathname.includes("/aaa/"), "falha de mutacao preserva o video atual", window.location.pathname);
  check(!requestLog.some((request) => /\/videos\/aaa\/export$/.test(request.url)), "falha de mutacao nao enfileira exportacao");
  check(text.includes("falha controlada ao salvar"), "erro de salvamento continua visivel");
}

for (const [needle, label] of scenario.expect ?? []) {
  check(text.includes(needle), label);
}
for (const [needle, label] of scenario.html ?? []) {
  check(html.includes(needle), label);
}
for (const [needle, label] of scenario.reject ?? []) {
  check(!text.includes(needle), label);
}
if (scenario.disabledButton) {
  const button = [...window.document.querySelectorAll("button")].find(
    (element) => element.textContent?.trim() === scenario.disabledButton,
  );
  check(Boolean(button?.disabled), `${scenario.disabledButton} fica desabilitado antes da confirmação`);
}
for (const [pattern, label] of scenario.expectedRequests ?? []) {
  check(requests.some((url) => pattern.test(url)), label);
}
if (scenario.expectPath) {
  check(window.location.pathname === scenario.expectPath, "abriu o próximo vídeo", window.location.pathname);
}

if (readFileSync(join(staticDir, "index.html"), "utf8").includes("/assets/")) {
  check(true, "index.html referencia os assets");
}

if (failed) {
  console.log("\n--- texto renderizado ---");
  console.log(text.slice(0, 400) || "(vazio)");
  if (errors.length) console.log("\n--- erros ---\n" + errors.slice(0, 5).join("\n"));
}

console.log(failed ? "\nSMOKE FALHOU" : "\nsmoke ok");
process.exit(failed ? 1 : 0);
