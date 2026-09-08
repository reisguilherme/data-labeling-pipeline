import { useCallback, useEffect, useMemo, useState } from "react";
import { api, watchJob } from "../api/client";
import type { DatasetInfo, DatasetPreview, JobInfo } from "../api/types";
import { Button, Chip, Panel, Spinner } from "../components/ui";
import { useLibrary } from "../store/library";
import { useSession } from "../store/session";

/** Puro e fora da store — seletor devolvendo objeto novo derruba o React 19. */
function selectedCount(flags: Record<string, string[]>): number {
  return Object.values(flags).reduce((total, values) => total + values.length, 0);
}

export function DatasetView({ onBack }: { onBack: () => void }) {
  const config = useSession((s) => s.config);
  const activeObject = useSession((s) => s.activeObject);
  const videos = useLibrary((s) => s.videos);

  const [flags, setFlags] = useState<Record<string, string[]>>({});
  const [videoIds, setVideoIds] = useState<string[]>([]);
  const [reviewedOnly, setReviewedOnly] = useState(false);
  const [includeEmpty, setIncludeEmpty] = useState(false);
  const [format, setFormat] = useState<"yolo" | "coco">("yolo");
  const [task, setTask] = useState<"detection" | "segmentation">("detection");
  const [valFraction, setValFraction] = useState(0.2);
  const [testFraction, setTestFraction] = useState(0);

  const [preview, setPreview] = useState<DatasetPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [job, setJob] = useState<JobInfo | null>(null);
  const [datasets, setDatasets] = useState<DatasetInfo[]>([]);
  const [error, setError] = useState<string | null>(null);

  const filters = useMemo(
    () => ({
      flags,
      video_ids: videoIds,
      reviewed_only: reviewedOnly,
      include_empty: includeEmpty,
    }),
    [flags, videoIds, reviewedOnly, includeEmpty],
  );

  const refreshPreview = useCallback(async () => {
    setPreviewing(true);
    setError(null);
    try {
      setPreview(await api.datasetPreview({ ...filters, task }));
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setPreviewing(false);
    }
  }, [filters, task]);

  // A prévia é o que evita exportar 9 mil frames e só então perceber que o
  // filtro estava errado — então acompanha cada mudança.
  useEffect(() => {
    void refreshPreview();
  }, [refreshPreview]);

  const loadDatasets = useCallback(async () => {
    try {
      setDatasets((await api.datasets()).datasets);
    } catch {
      /* lista é informativa */
    }
  }, []);

  useEffect(() => {
    void loadDatasets();
  }, [loadDatasets]);

  const toggleFlag = (group: string, option: string) => {
    setFlags((current) => {
      const values = current[group] ?? [];
      const next = values.includes(option)
        ? values.filter((v) => v !== option)
        : [...values, option];
      const copy = { ...current };
      if (next.length) copy[group] = next;
      else delete copy[group];
      return copy;
    });
  };

  const exportNow = async () => {
    setError(null);
    try {
      const { job_id } = await api.datasetExport({
        format,
        task,
        val_fraction: valFraction,
        test_fraction: testFraction,
        filters,
      });
      const stop = watchJob(job_id, (info) => {
        setJob(info);
        if (info.state === "done") {
          stop();
          void loadDatasets();
        }
      });
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const doneVideos = useMemo(() => videos.filter((v) => v.status === "done"), [videos]);
  const active = selectedCount(flags) > 0 || videoIds.length > 0;

  return (
    <div className="mx-auto flex h-full w-full max-w-4xl flex-col gap-4 overflow-y-auto p-6">
      <header className="flex items-center gap-3">
        <Button variant="ghost" onClick={onBack}>
          ← biblioteca
        </Button>
        <h1 className="text-sm font-medium text-zinc-100">
          Exportar dataset · {activeObject?.display_name}
        </h1>
      </header>

      <Panel title="Recorte por flags da triagem">
        <div className="space-y-3">
          {(config?.flag_groups ?? []).map((group) => (
            <div key={group.id}>
              <p className="mb-1.5 text-xs text-zinc-500">{group.label}</p>
              <div className="flex flex-wrap gap-1.5">
                {group.options.map((option) => (
                  <Chip
                    key={option.id}
                    active={(flags[group.id] ?? []).includes(option.id)}
                    onClick={() => toggleFlag(group.id, option.id)}
                    count={preview?.by_flag?.[group.id]?.[option.id]}
                  >
                    {option.label}
                  </Chip>
                ))}
              </div>
            </div>
          ))}
          <p className="text-[11px] text-zinc-600">
            Dentro de um grupo vale qualquer um dos marcados; entre grupos, todos
            precisam bater. Nada marcado = sem restrição.
          </p>
        </div>
      </Panel>

      <Panel
        title="Vídeos"
        right={
          videoIds.length > 0 ? (
            <button
              onClick={() => setVideoIds([])}
              className="text-[11px] text-zinc-500 hover:text-zinc-300"
            >
              limpar seleção
            </button>
          ) : (
            <span className="text-[11px] text-zinc-600">nenhum = todos</span>
          )
        }
      >
        <div className="max-h-44 space-y-0.5 overflow-y-auto">
          {doneVideos.map((video) => (
            <label
              key={video.video_id}
              className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-xs text-zinc-300 hover:bg-zinc-900"
            >
              <input
                type="checkbox"
                checked={videoIds.includes(video.video_id)}
                onChange={() =>
                  setVideoIds((current) =>
                    current.includes(video.video_id)
                      ? current.filter((id) => id !== video.video_id)
                      : [...current, video.video_id],
                  )
                }
              />
              <span className="flex-1 truncate">{video.name}</span>
              {video.sam3?.state === "done" && (
                <span className="text-[10px] text-violet-400">SAM3</span>
              )}
            </label>
          ))}
          {doneVideos.length === 0 && (
            <p className="text-xs text-zinc-600">nenhum vídeo exportado ainda</p>
          )}
        </div>
      </Panel>

      <Panel title="Formato e divisão">
        <div className="flex flex-wrap items-end gap-4">
          <label className="block">
            <span className="text-xs text-zinc-500">Tarefa</span>
            <select
              value={task}
              onChange={(event) =>
                setTask(event.target.value as "detection" | "segmentation")
              }
              className="mt-1 block rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-200"
            >
              <option value="detection">Detecao (bbox)</option>
              <option value="segmentation">Segmentacao (mascara)</option>
            </select>
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">Formato</span>
            <select
              value={format}
              onChange={(event) => setFormat(event.target.value as "yolo" | "coco")}
              className="mt-1 block rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-200"
            >
              <option value="yolo">YOLO (txt por imagem)</option>
              <option value="coco">COCO (instances_*.json)</option>
            </select>
          </label>

          <label className="block">
            <span className="text-xs text-zinc-500">Validação</span>
            <input
              type="number"
              min={0}
              max={0.9}
              step={0.05}
              value={valFraction}
              onChange={(event) => setValFraction(Number(event.target.value))}
              className="mt-1 block w-24 rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-200"
            />
          </label>

          <label className="block">
            <span className="text-xs text-zinc-500">Teste</span>
            <input
              type="number"
              min={0}
              max={0.9}
              step={0.05}
              value={testFraction}
              onChange={(event) => setTestFraction(Number(event.target.value))}
              className="mt-1 block w-24 rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-200"
            />
          </label>

          <label className="flex items-center gap-2 text-xs text-zinc-400">
            <input
              type="checkbox"
              checked={reviewedOnly}
              onChange={(event) => setReviewedOnly(event.target.checked)}
            />
            só revisados
          </label>

          <label className="flex items-center gap-2 text-xs text-zinc-400">
            <input
              type="checkbox"
              checked={includeEmpty}
              onChange={(event) => setIncludeEmpty(event.target.checked)}
            />
            incluir frames sem objeto
          </label>
        </div>
        <p className="mt-3 text-[11px] text-zinc-600">
          A divisão é por <strong>vídeo</strong>, nunca por frame: frames vizinhos
          são quase idênticos, e separá-los faria a validação medir memorização.
        </p>
      </Panel>

      <Panel
        title="Prévia"
        right={previewing ? <Spinner className="text-zinc-600" /> : undefined}
      >
        {preview ? (
          <div className="flex flex-wrap gap-6">
            <Stat label="vídeos" value={preview.videos} />
            <Stat label="segmentos" value={preview.segments} />
            <Stat label="frames" value={preview.frames} />
            <Stat label="com objeto" value={preview.frames_with_objects} />
            <Stat label="revisados" value={preview.frames_reviewed} />
          </div>
        ) : (
          <p className="text-xs text-zinc-600">calculando…</p>
        )}

        <div className="mt-4 flex items-center gap-3">
          <Button
            variant="primary"
            onClick={exportNow}
            disabled={!preview?.frames || !preview.export_allowed || job?.state === "running"}
          >
            exportar {format.toUpperCase()} {task === "segmentation" ? "seg" : "det"}
            {active ? " (com recorte)" : " (tudo)"}
          </Button>
          {job && (
            <span className="tnum text-xs text-zinc-400">
              {job.state === "running" && <Spinner className="mr-1 inline" />}
              {job.state === "error" ? (
                <span className="text-red-400">{job.error}</span>
              ) : (
                <>
                  {job.message} {job.total ? `· ${job.current}/${job.total}` : ""}
                </>
              )}
            </span>
          )}
        </div>
        {preview && !preview.export_allowed && (
          <div className="mt-3 rounded border border-amber-900/60 bg-amber-950/30 p-2 text-xs text-amber-300">
            Segmentacao bloqueada: {preview.blocking_reasons[0]}
            {preview.blocking_reasons.length > 1 &&
              ` (+${preview.blocking_reasons.length - 1} ocorrencias)`}
          </div>
        )}
      </Panel>

      {datasets.length > 0 && (
        <Panel title="Datasets exportados">
          <ul className="space-y-1">
            {datasets.map((dataset) => (
              <li
                key={dataset.name}
                className="flex items-center gap-3 border-b border-zinc-800/60 py-1.5 text-xs last:border-0"
              >
                <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-300 uppercase">
                  {dataset.format}-{dataset.task === "segmentation" ? "seg" : "det"}
                </span>
                <span className="flex-1 truncate text-zinc-300">{dataset.name}</span>
                <span className="tnum text-zinc-600">
                  {String((dataset.counts as { images?: number }).images ?? "")} imagens
                </span>
                <button
                  onClick={async () => {
                    await api.deleteDataset(dataset.name);
                    void loadDatasets();
                  }}
                  className="text-zinc-600 hover:text-red-400"
                  title="apagar"
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      {error && (
        <p className="rounded-md border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
          {error}
        </p>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <div className="tnum text-lg text-zinc-100">{value.toLocaleString("pt-BR")}</div>
      <div className="text-[11px] text-zinc-500">{label}</div>
    </div>
  );
}
