import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api, watchJob } from "../api/client";
import type { DatasetInfo, GlobalDatasetPreview, JobInfo, SelectedVideo, VideoListItem } from "../api/types";
import { ObjectClassSelector } from "../components/ObjectClassSelector";
import { VideoMultiSelector } from "../components/VideoMultiSelector";
import { Spinner } from "../components/ui";
import { useSession } from "../store/session";

export function GlobalDatasetView() {
  const config = useSession((state) => state.config);
  const sessionObjects = useSession((state) => state.objects);
  const objects = useMemo(() => sessionObjects.filter((item) => !item.archived), [sessionObjects]);
  const [objectIds, setObjectIds] = useState<string[]>(() => objects.map((item) => item.object_id));
  const [task, setTask] = useState<"detection" | "segmentation">("segmentation");
  const [format, setFormat] = useState<"yolo" | "coco">("yolo");
  const [flags, setFlags] = useState<Record<string, string[]>>({});
  const [videos, setVideos] = useState<SelectedVideo[]>([]);
  const [availableVideos, setAvailableVideos] = useState<{ object_id: string; object_name: string; video: VideoListItem }[]>([]);
  const [includeEmpty, setIncludeEmpty] = useState(false);
  const [valFraction, setValFraction] = useState(0.2);
  const [testFraction, setTestFraction] = useState(0);
  const [name, setName] = useState("");
  const [preview, setPreview] = useState<GlobalDatasetPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<JobInfo | null>(null);
  const [history, setHistory] = useState<DatasetInfo[]>([]);

  const payload = useMemo(() => ({
    object_ids: [...objectIds].sort(),
    filters: { flags, videos, include_empty: includeEmpty },
    task,
  }), [objectIds, flags, videos, includeEmpty, task]);

  useEffect(() => {
    let active = true;
    setPreview(null);
    setError(null);
    if (objectIds.length === 0) return;
    setPreviewing(true);
    const timer = window.setTimeout(() => {
      void api.globalDatasetPreview(payload)
        .then((result) => { if (active) setPreview(result); })
        .catch((exc) => { if (active) setError((exc as Error).message); })
        .finally(() => { if (active) setPreviewing(false); });
    }, 150);
    return () => { active = false; window.clearTimeout(timer); };
  }, [payload, objectIds.length]);

  useEffect(() => {
    let active = true;
    void Promise.all(objectIds.map(async (objectId) => {
      const object = objects.find((item) => item.object_id === objectId);
      const response = await api.videosForObject(objectId);
      return response.videos
        .filter((video) => video.pipeline_stage === "completed")
        .map((video) => ({ object_id: objectId, object_name: object?.display_name ?? objectId, video }));
    })).then((groups) => { if (active) setAvailableVideos(groups.flat()); }).catch(() => { if (active) setAvailableVideos([]); });
    return () => { active = false; };
  }, [objectIds.join("|"), objects]);

  const loadHistory = () => api.globalDatasets().then((result) => setHistory(result.datasets)).catch(() => undefined);
  useEffect(() => { void loadHistory(); }, []);

  const canExport = Boolean(preview?.export_allowed && preview.frames > 0 && !job?.state.match(/queued|running/));
  const startExport = async () => {
    if (!canExport) return;
    setError(null);
    try {
      const started = await api.globalDatasetExport({
        ...payload,
        format,
        name: name.trim() || undefined,
        val_fraction: valFraction,
        test_fraction: testFraction,
      });
      const initial = await api.job(started.job_id);
      setJob(initial);
      watchJob(started.job_id, (next) => {
        setJob(next);
        if (next.state === "done") void loadHistory();
      });
    } catch (exc) { setError((exc as Error).message); }
  };

  return (
    <div className="h-full overflow-y-auto p-5 lg:p-8">
      <div className="mx-auto max-w-6xl">
        <div className="flex flex-wrap items-end gap-4">
          <div><p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-emerald-500">Dataset global</p><h1 className="mt-1 text-3xl font-semibold tracking-tight text-zinc-50">Exportar datasets</h1><p className="mt-2 text-sm text-zinc-400">Combine um ou vários objetos em um único dataset multiclasse.</p></div>
          <div className="flex-1" />
          <span className="rounded-full border border-emerald-900 bg-emerald-950/30 px-3 py-1.5 text-xs text-emerald-300">✓ Somente concluídos</span>
        </div>

        <div className="mt-7 grid gap-5 lg:grid-cols-[minmax(0,1.4fr)_minmax(320px,.6fr)]">
          <div className="space-y-5">
            <Panel title="1. Objetos e classes" description="Os IDs seguem a ordem estável de object_id, não a ordem dos cliques."><ObjectClassSelector objects={objects} selected={objectIds} onChange={(ids) => { setObjectIds(ids); setVideos((items) => items.filter((item) => ids.includes(item.object_id))); }} /></Panel>
            <Panel title="2. Tags e vídeos" description="OR dentro do grupo e AND entre grupos.">
              <div className="grid gap-5 md:grid-cols-2">
                <div className="space-y-3">{config?.flag_groups.map((group) => <fieldset key={group.id}><legend className="text-xs font-medium text-zinc-300">{group.label}</legend><div className="mt-2 flex flex-wrap gap-2">{group.options.map((option) => { const checked = (flags[group.id] ?? []).includes(option.id); return <label key={option.id} className={`cursor-pointer rounded-full border px-2.5 py-1 text-xs ${checked ? "border-violet-700 bg-violet-950 text-violet-300" : "border-zinc-800 text-zinc-400"}`}><input name={`dataset_flag_${group.id}_${option.id}`} type="checkbox" className="sr-only" checked={checked} onChange={() => setFlags((current) => ({ ...current, [group.id]: checked ? (current[group.id] ?? []).filter((id) => id !== option.id) : [...(current[group.id] ?? []), option.id] }))} />{option.label}</label>; })}</div></fieldset>)}</div>
                <VideoMultiSelector videos={availableVideos} selected={videos} onChange={setVideos} />
              </div>
            </Panel>
            <Panel title="3. Formato e divisão" description="O split é determinístico e sempre agrupado por objeto + vídeo.">
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <Choice name="dataset_task" label="Tarefa" value={task} options={[{ id: "detection", label: "Detecção" }, { id: "segmentation", label: "Segmentação" }]} onChange={(value) => setTask(value as typeof task)} />
                <Choice name="dataset_format" label="Formato" value={format} options={[{ id: "yolo", label: "YOLO" }, { id: "coco", label: "COCO" }]} onChange={(value) => setFormat(value as typeof format)} />
                <NumberField name="dataset_validation_fraction" label="Validação" value={valFraction} onChange={setValFraction} />
                <NumberField name="dataset_test_fraction" label="Teste" value={testFraction} onChange={setTestFraction} />
              </div>
              <label className="mt-4 flex items-center gap-2 text-xs text-zinc-400"><input name="dataset_include_empty" type="checkbox" checked={includeEmpty} onChange={(event) => setIncludeEmpty(event.target.checked)} className="accent-emerald-500" />Incluir frames vazios explicitamente revisados</label>
              <label className="mt-4 block text-xs text-zinc-400">Nome do dataset (opcional)<input name="dataset_name" value={name} onChange={(event) => setName(event.target.value)} placeholder="ex.: objetos-v3" className="mt-1.5 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-200" /></label>
            </Panel>
          </div>

          <aside className="space-y-5">
            <section className="sticky top-0 rounded-xl border border-zinc-800 bg-zinc-900/55 p-5">
              <div className="flex items-center justify-between"><h2 className="text-sm font-semibold text-zinc-100">Preview obrigatório</h2>{previewing && <Spinner />}</div>
              {preview ? <><div className="mt-5 grid grid-cols-2 gap-3"><Metric label="Frames" value={preview.frames} /><Metric label="Vídeos" value={preview.videos} /><Metric label="Com objeto" value={preview.frames_with_objects} /><Metric label="Segmentos" value={preview.segments} /></div><div className="mt-4 space-y-1">{preview.classes.map((item) => <p key={item.object_id} className="flex justify-between rounded bg-zinc-950/60 px-2.5 py-1.5 text-xs"><span className="text-zinc-300">{item.id} · {item.name}</span><span className="text-zinc-400">{item.object_id}</span></p>)}</div>{preview.blocking_reasons.length > 0 && <div className="mt-4 rounded-lg border border-red-900/70 bg-red-950/30 p-3"><p className="text-xs font-medium text-red-300">Exportação bloqueada</p>{preview.blocking_reasons.map((reason) => <p key={reason} className="mt-1 text-xs text-red-400">{reason}</p>)}</div>}</> : <p className="mt-5 text-xs text-zinc-400">Selecione objetos para calcular o preview.</p>}
              {error && <p className="mt-4 rounded-lg bg-red-950/30 p-3 text-xs text-red-300">{error}</p>}
              {job && <div className="mt-4 rounded-lg bg-zinc-950 p-3 text-xs text-zinc-400"><p>{job.message || job.state}</p><div className="mt-2 h-1.5 overflow-hidden rounded bg-zinc-800"><div className="h-full bg-emerald-500" style={{ width: `${Math.round(job.progress * 100)}%` }} /></div>{job.error && <p className="mt-2 text-red-300">{job.error}</p>}</div>}
              <button type="button" disabled={!canExport} onClick={() => void startExport()} className="mt-5 w-full rounded-lg bg-emerald-500 px-4 py-2.5 text-sm font-semibold text-zinc-950 hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-40">Exportar dataset</button>
            </section>
            <section className="rounded-xl border border-zinc-800 p-4"><h2 className="text-sm font-medium text-zinc-200">Histórico</h2>{history.length === 0 ? <p className="mt-2 text-xs text-zinc-400">Nenhum dataset exportado ainda.</p> : <div className="mt-3 space-y-2">{history.slice(0, 8).map((item) => <div key={`${item.scope}:${item.object_id}:${item.name}`} className="rounded bg-zinc-900 p-2"><p className="truncate text-xs text-zinc-300">{item.name}</p><p className="mt-1 text-[10px] text-zinc-400">{item.scope === "global" ? "Global" : `Objeto · ${item.object_id}`} · {item.format}/{item.task}</p></div>)}</div>}</section>
          </aside>
        </div>
      </div>
    </div>
  );
}

function Panel({ title, description, children }: { title: string; description: string; children: ReactNode }) { return <section className="rounded-xl border border-zinc-800 bg-zinc-900/30 p-5"><h2 className="text-sm font-semibold text-zinc-100">{title}</h2><p className="mt-1 text-xs text-zinc-400">{description}</p><div className="mt-4">{children}</div></section>; }
function Metric({ label, value }: { label: string; value: number }) { return <div className="rounded-lg bg-zinc-950/70 p-3"><p className="text-[10px] uppercase tracking-wide text-zinc-400">{label}</p><p className="tnum mt-1 text-xl font-semibold text-zinc-100">{value}</p></div>; }
function Choice({ name, label, value, options, onChange }: { name: string; label: string; value: string; options: { id: string; label: string }[]; onChange: (value: string) => void }) { return <label className="text-xs text-zinc-400">{label}<select name={name} value={value} onChange={(event) => onChange(event.target.value)} className="mt-1.5 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-200">{options.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}</select></label>; }
function NumberField({ name, label, value, onChange }: { name: string; label: string; value: number; onChange: (value: number) => void }) { return <label className="text-xs text-zinc-400">{label}<input name={name} type="number" min="0" max="0.9" step="0.05" value={value} onChange={(event) => onChange(Number(event.target.value))} className="mt-1.5 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-200" /></label>; }
