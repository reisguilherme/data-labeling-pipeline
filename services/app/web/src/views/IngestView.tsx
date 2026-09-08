import { useCallback, useEffect, useMemo, useState } from "react";
import { api, watchJob } from "../api/client";
import type { ExcludePattern, GcsBucket, GcsListing, JobInfo } from "../api/types";
import { Button, Panel, Spinner } from "../components/ui";
import { formatBytes } from "../lib/format";
import { useSession } from "../store/session";

/** Ordem e texto dos baldes da prévia. Espelha a classificação do servidor. */
const BUCKETS: { id: GcsBucket; label: string; hint: string; action: boolean }[] = [
  { id: "new", label: "novos", hint: "serão baixados", action: true },
  { id: "missing", label: "sumiram do disco", hint: "no manifesto, mas o arquivo não está lá", action: true },
  { id: "downloaded", label: "já baixados", hint: "pular", action: false },
  { id: "excluded", label: "descartados antes", hint: "pular", action: false },
  { id: "matches_pattern", label: "casam uma regra", hint: "pular sem baixar", action: false },
  { id: "skipped", label: "ignorados", hint: "não são vídeo", action: false },
];

/** Puro e fora da store — seletor que devolve array novo derruba o React 19. */
function selectable(listing: GcsListing | null): string[] {
  if (!listing) return [];
  return [...listing.buckets.new, ...listing.buckets.missing].map((f) => f.name);
}

export function IngestView({ onBack }: { onBack: () => void }) {
  const activeObject = useSession((s) => s.activeObject);
  const config = useSession((s) => s.config);

  const [listing, setListing] = useState<GcsListing | null>(null);
  const [patterns, setPatterns] = useState<ExcludePattern[]>([]);
  const [draft, setDraft] = useState("");
  const [listingBusy, setListingBusy] = useState(false);
  const [job, setJob] = useState<JobInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [applyReport, setApplyReport] = useState<string | null>(null);

  const names = useMemo(() => selectable(listing), [listing]);
  const gcloudOk = config?.gcloud.ok && config.gcloud.credentials_ok;

  useEffect(() => {
    api
      .exclusions()
      .then((info) => setPatterns(info.exclude_patterns))
      .catch(() => undefined);
  }, []);

  const list = useCallback(async () => {
    setListingBusy(true);
    setError(null);
    try {
      setListing(await api.gcsList());
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setListingBusy(false);
    }
  }, []);

  const download = async () => {
    setError(null);
    try {
      const { job_id } = await api.gcsDownload(names);
      const stop = watchJob(job_id, (info) => {
        setJob(info);
        if (info.state === "done") {
          stop();
          void list();
        }
      });
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const savePatterns = async (next: ExcludePattern[]) => {
    setError(null);
    try {
      await api.setPatterns(next);
      setPatterns(next);
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const addPattern = () => {
    const value = draft.trim();
    if (!value) return;
    void savePatterns([...patterns, { kind: "substring", value, case_sensitive: false }]);
    setDraft("");
  };

  const dryRun = async (apply: boolean) => {
    setError(null);
    setApplyReport(null);
    try {
      const result = await api.applyExclusions(apply);
      setApplyReport(
        apply
          ? `${result.moved.length} vídeos movidos para a lixeira; ${result.blocked.length} bloqueados por já estarem triados.`
          : `${result.matches.length} seriam movidos, ${result.blocked.length} bloqueados por já estarem triados (precisam de confirmação explícita).`,
      );
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  return (
    <div className="mx-auto flex h-full w-full max-w-4xl flex-col gap-4 overflow-y-auto p-6">
      <header className="flex items-center gap-3">
        <Button variant="ghost" onClick={onBack}>
          ← biblioteca
        </Button>
        <h1 className="text-sm font-medium text-zinc-100">
          Baixar vídeos · {activeObject?.display_name}
        </h1>
      </header>

      {!gcloudOk && (
        <div className="rounded-md border border-amber-900/60 bg-amber-950/40 px-3 py-2 text-xs text-amber-300">
          {config?.gcloud.error ??
            "Configure GOOGLE_APPLICATION_CREDENTIALS no arquivo .env para baixar do bucket."}
        </div>
      )}

      <Panel title="Origem">
        <div className="flex items-center gap-3">
          <code className="flex-1 truncate rounded bg-zinc-950 px-2 py-1.5 text-xs text-zinc-400">
            {activeObject?.gcs_uri ?? "nenhum bucket configurado para este objeto"}
          </code>
          <Button onClick={list} disabled={!activeObject?.gcs_uri || listingBusy}>
            {listingBusy && <Spinner />} listar bucket
          </Button>
        </div>
      </Panel>

      <Panel
        title="Regras de descarte"
        right={
          <span className="text-[11px] text-zinc-600">
            aplicadas antes de baixar
          </span>
        }
      >
        <div className="flex flex-wrap gap-1.5">
          {patterns.map((pattern, index) => (
            <span
              key={`${pattern.value}-${index}`}
              className="inline-flex items-center gap-1.5 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-300"
            >
              {pattern.kind === "regex" && (
                <span className="text-[10px] text-zinc-500">re</span>
              )}
              {pattern.value}
              <button
                onClick={() =>
                  void savePatterns(patterns.filter((_, i) => i !== index))
                }
                className="text-zinc-500 hover:text-red-400"
              >
                ×
              </button>
            </span>
          ))}
          {patterns.length === 0 && (
            <span className="text-xs text-zinc-600">nenhuma regra</span>
          )}
        </div>

        <div className="mt-3 flex gap-2">
          <input
            aria-label="Novo padrão de descarte"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => event.key === "Enter" && addPattern()}
            placeholder="ex.: SOMBRA"
            className="flex-1 rounded-md border border-zinc-800 bg-zinc-950 px-3 py-1.5 text-sm text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
          />
          <Button onClick={addPattern} disabled={!draft.trim()}>
            adicionar
          </Button>
        </div>

        <div className="mt-3 flex items-center gap-2">
          <Button onClick={() => dryRun(false)} disabled={patterns.length === 0}>
            simular na biblioteca
          </Button>
          <Button
            variant="danger"
            onClick={() => dryRun(true)}
            disabled={patterns.length === 0}
          >
            aplicar e mover para a lixeira
          </Button>
        </div>
        {applyReport && (
          <p className="mt-2 text-xs text-zinc-400">{applyReport}</p>
        )}
      </Panel>

      {listing && (
        <Panel
          title="Prévia"
          right={
            <span className="tnum text-[11px] text-zinc-500">
              {listing.total} arquivos no bucket
            </span>
          }
        >
          <ul className="space-y-1">
            {BUCKETS.map((bucket) => {
              const count = listing.counts[bucket.id] ?? 0;
              return (
                <li
                  key={bucket.id}
                  className="flex items-baseline gap-3 border-b border-zinc-800/60 py-1.5 last:border-0"
                >
                  <span
                    className={
                      bucket.action
                        ? "tnum w-10 text-right text-sm font-medium text-emerald-400"
                        : "tnum w-10 text-right text-sm text-zinc-500"
                    }
                  >
                    {count}
                  </span>
                  <span className="text-sm text-zinc-300">{bucket.label}</span>
                  <span className="text-[11px] text-zinc-600">{bucket.hint}</span>
                </li>
              );
            })}
          </ul>

          <div className="mt-4 flex items-center gap-3">
            <Button
              variant="primary"
              onClick={download}
              disabled={names.length === 0 || job?.state === "running"}
            >
              baixar {names.length} arquivos
            </Button>
            {job && (
              <span className="tnum text-xs text-zinc-400">
                {job.state === "running" && <Spinner className="mr-1 inline" />}
                {job.message} · {job.current}/{job.total}
              </span>
            )}
          </div>

          {listing.buckets.new.length > 0 && (
            <ul className="mt-3 max-h-48 space-y-0.5 overflow-y-auto">
              {listing.buckets.new.map((file) => (
                <li
                  key={file.name}
                  className="flex items-baseline justify-between gap-3 text-[11px]"
                >
                  <span className="truncate text-zinc-400">{file.name}</span>
                  <span className="tnum shrink-0 text-zinc-600">
                    {file.size_bytes ? formatBytes(file.size_bytes) : ""}
                  </span>
                </li>
              ))}
            </ul>
          )}
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
