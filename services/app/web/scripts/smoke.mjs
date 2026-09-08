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
  [/\/api\/objects\/[^/]+\/annotations\/[^/]+$/, TRIAGE_ANNOTATION],
  [/\/api\/objects\/[^/]+\/videos\/[^/]+\/meta$/, VIDEO_META],
  [/\/api\/objects\/[^/]+\/videos\/[^/]+\/proxy\/status$/, PROXY_STATUS],
  [/\/api\/objects\/[^/]+\/videos\/[^/]+\/proxy$/, { mode: "full", job_id: null, total_frames: 96, already_complete: true }],
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
const requests = [];
let maskFrameRequests = 0;
window.confirm = () => true;

window.fetch = async (input) => {
  const url = String(input);
  requests.push(url);
  if (scenario.action === "review-walk-save" && /\/mask-review\/\d+$/.test(url)) {
    maskFrameRequests += 1;
    if (maskFrameRequests === 2) await new Promise((resolve) => setTimeout(resolve, 120));
  }
  const match = routes.find(([pattern]) => pattern.test(url));
  return {
    ok: Boolean(match),
    status: match ? 200 : 404,
    statusText: match ? "OK" : "Not Found",
    json: async () => typeof match?.[1] === "function" ? match[1](url) : match?.[1] ?? { detail: "not found" },
  };
};

window.EventSource = class {
  close() {}
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
await new Promise((resolve) => setTimeout(resolve, 600));

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
  await new Promise((resolve) => setTimeout(resolve, 250));
}
if (scenario.action === "mark-no-object") {
  requests.length = 0;
  const action = [...window.document.querySelectorAll("button")].find(
    (element) => element.textContent?.toLowerCase().includes("sem boom") || element.textContent?.includes("Sem objeto"),
  );
  action?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 250));
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
