import { useEffect, useMemo, useRef, useState } from "react";
import { api, watchJob } from "../api/client";
import type { ObjectInfo } from "../api/types";
import { Button, Spinner } from "../components/ui";
import { CONFIRMED_NAVIGATION, confirmNavigation, navigate } from "../lib/routes";
import { useSession } from "../store/session";

type Tab = "active" | "archived";

export function ObjectsView({ onOpen }: { onOpen: (objectId: string) => void }) {
  const user = useSession((state) => state.user);
  const logout = useSession((state) => state.logout);
  const addObject = useSession((state) => state.addObject);
  const refreshConfig = useSession((state) => state.refreshConfig);
  const activeObject = useSession((state) => state.activeObject);
  const sessionObjects = useSession((state) => state.objects);
  const [objects, setObjects] = useState<ObjectInfo[]>(sessionObjects);
  const [tab, setTab] = useState<Tab>("active");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadingObjects, setLoadingObjects] = useState(true);
  const [loggingOut, setLoggingOut] = useState(false);
  const [purging, setPurging] = useState<ObjectInfo | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const purgeDialogRef = useRef<HTMLDivElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const mountedRef = useRef(true);
  const loadGenerationRef = useRef(0);

  const load = async () => {
    const generation = ++loadGenerationRef.current;
    setLoadingObjects(true);
    setError(null);
    try {
      const data = await api.objects(true);
      if (!mountedRef.current || generation !== loadGenerationRef.current) return;
      setObjects(data.objects);
      setSelectedId((current) => current ?? data.objects.find((item) => !item.archived)?.object_id ?? data.objects[0]?.object_id ?? null);
    } catch (exc) {
      if (mountedRef.current && generation === loadGenerationRef.current) {
        setError((exc as Error).message);
      }
    } finally {
      if (mountedRef.current && generation === loadGenerationRef.current) {
        setLoadingObjects(false);
      }
    }
  };
  useEffect(() => {
    mountedRef.current = true;
    void load();
    return () => {
      mountedRef.current = false;
      loadGenerationRef.current += 1;
    };
  }, []);
  useEffect(() => {
    if (!purging) return;
    const dialog = purgeDialogRef.current;
    const previous = previousFocusRef.current;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setPurging(null);
        return;
      }
      if (event.key !== "Tab" || !dialog) return;
      const focusable = [...dialog.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])")];
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previous?.focus();
    };
  }, [purging]);

  const visible = useMemo(
    () => objects.filter((item) => tab === "archived" ? item.archived : !item.archived),
    [objects, tab],
  );
  const selected = objects.find((item) => item.object_id === selectedId) ?? null;

  const replace = (updated: ObjectInfo) => {
    setObjects((items) => items.map((item) => item.object_id === updated.object_id ? updated : item));
    void refreshConfig();
  };

  return (
    <div className="flex h-full flex-col overflow-hidden bg-zinc-950">
      <header className="flex h-16 shrink-0 items-center gap-4 border-b border-zinc-800 px-5 lg:px-8">
        <button type="button" onClick={() => activeObject && navigate({ page: "operations", objectId: activeObject.object_id, stage: "overview" })} className="grid h-8 w-8 place-items-center rounded-lg bg-emerald-500 text-xs font-black text-zinc-950">B</button>
        <div>
          <h1 className="text-lg font-semibold text-zinc-100">Objetos e classes</h1>
          <p className="text-xs text-zinc-400">O que vamos anotar? Edite nomes sem alterar o identificador histórico.</p>
        </div>
        <div className="flex-1" />
        {user && (
          <button
            type="button"
            disabled={loggingOut}
            onClick={async () => {
              if (!confirmNavigation()) return;
              setLoggingOut(true);
              setError(null);
              try {
                await logout();
                navigate({ page: "objects" }, true, CONFIRMED_NAVIGATION);
              } catch (exc) {
                if (mountedRef.current) setError((exc as Error).message);
              } finally {
                if (mountedRef.current) setLoggingOut(false);
              }
            }}
            className="text-xs text-zinc-400 hover:text-zinc-200 disabled:cursor-wait disabled:opacity-50"
          >
            {user.display_name} · {loggingOut ? "saindo…" : "sair"}
          </button>
        )}
      </header>

      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        <aside className="w-full shrink-0 border-b border-zinc-800 bg-zinc-950/50 p-4 md:w-72 md:border-r md:border-b-0">
          <div className="flex rounded-lg bg-zinc-900 p-1">
            <TabButton active={tab === "active"} onClick={() => setTab("active")}>Ativos</TabButton>
            <TabButton active={tab === "archived"} onClick={() => setTab("archived")}>Arquivados</TabButton>
          </div>
          <div className="mt-4 space-y-2 overflow-y-auto">
            {loadingObjects && objects.length === 0 && (
              <p className="flex items-center gap-2 p-3 text-xs text-zinc-400"><Spinner /> Carregando objetos…</p>
            )}
            {visible.map((object) => (
              <button
                type="button"
                key={object.object_id}
                onClick={() => { setSelectedId(object.object_id); setCreating(false); }}
                className={`w-full rounded-lg border p-3 text-left ${selectedId === object.object_id ? "border-emerald-800 bg-emerald-950/25" : "border-zinc-800 bg-zinc-900/40 hover:border-zinc-700"}`}
              >
                <span className="block truncate text-sm font-medium text-zinc-200">{object.display_name}</span>
                <span className="mt-1 block truncate text-[11px] text-zinc-400">{object.object_id} · {object.label}</span>
              </button>
            ))}
            {!loadingObjects && visible.length === 0 && <p className="p-3 text-xs text-zinc-400">Nenhum objeto nesta lista.</p>}
          </div>
          <button type="button" onClick={() => { setCreating(true); setSelectedId(null); }} className="mt-4 w-full rounded-lg border border-dashed border-zinc-700 px-3 py-2 text-sm text-zinc-400 hover:border-zinc-500 hover:text-zinc-200">+ novo objeto</button>
        </aside>

        <main className="min-w-0 flex-1 overflow-y-auto p-5 lg:p-8">
          <div className="mx-auto max-w-3xl">
            {error && (
              <div className="mb-4 flex items-center gap-3 rounded-lg border border-red-900 bg-red-950/30 p-3 text-sm text-red-300">
                <span className="flex-1">{error}</span>
                <Button variant="ghost" disabled={loadingObjects} onClick={() => void load()}>
                  {loadingObjects ? <Spinner /> : null} tentar novamente
                </Button>
              </div>
            )}
            {creating ? (
              <CreateObjectForm
                busy={busy}
                onCancel={() => setCreating(false)}
                onCreate={async (payload) => {
                  setBusy(true); setError(null);
                  try {
                    const created = await addObject(payload);
                    await load();
                    setCreating(false);
                    setSelectedId(created.object_id);
                  } catch (exc) { setError((exc as Error).message); }
                  finally { setBusy(false); }
                }}
              />
            ) : selected ? (
              <ObjectEditor
                key={selected.object_id}
                object={selected}
                busy={busy}
                onOpen={() => onOpen(selected.object_id)}
                onSave={async (changes) => {
                  setBusy(true); setError(null);
                  try { replace(await api.updateObject(selected.object_id, changes)); }
                  catch (exc) { setError((exc as Error).message); }
                  finally { setBusy(false); }
                }}
                onArchive={async () => {
                  setBusy(true); setError(null);
                  try { replace(await api.archiveObject(selected.object_id)); setTab("archived"); }
                  catch (exc) { setError((exc as Error).message); }
                  finally { setBusy(false); }
                }}
                onRestore={async () => {
                  setBusy(true); setError(null);
                  try { replace(await api.restoreObject(selected.object_id)); setTab("active"); }
                  catch (exc) { setError((exc as Error).message); }
                  finally { setBusy(false); }
                }}
                onPurge={() => {
                  previousFocusRef.current = document.activeElement as HTMLElement | null;
                  setPurging(selected);
                  setConfirmation("");
                }}
              />
            ) : <p className="text-sm text-zinc-400">Selecione ou crie um objeto.</p>}
          </div>
        </main>
      </div>

      {purging && (
        <div role="dialog" aria-modal="true" aria-labelledby="purge-title" className="fixed inset-0 z-50 grid place-items-center bg-black/75 p-4">
          <div ref={purgeDialogRef} className="w-full max-w-lg rounded-xl border border-red-900/70 bg-zinc-950 p-5 shadow-2xl">
            <h2 id="purge-title" className="text-lg font-semibold text-red-300">Excluir objeto permanentemente</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-400">Um inventário com checksums será criado antes da remoção. Datasets já exportados serão preservados. Esta operação não pode ser desfeita pela interface.</p>
            <label className="mt-4 block text-xs text-zinc-400">
              Digite <strong className="font-mono text-zinc-200">EXCLUIR {purging.object_id}</strong>
              <input autoFocus value={confirmation} onChange={(event) => setConfirmation(event.target.value)} className="mt-2 w-full rounded-lg border border-red-900 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-100" />
            </label>
            <div className="mt-5 flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setPurging(null)}>Cancelar</Button>
              <button
                type="button"
                disabled={confirmation !== `EXCLUIR ${purging.object_id}` || busy}
                onClick={async () => {
                  setBusy(true); setError(null);
                  try {
                    const started = await api.purgeObject(purging.object_id, confirmation);
                    setPurging(null);
                    watchJob(started.job_id, (job) => {
                      if (job.state === "done") void load();
                      if (job.state === "error") setError(job.error ?? "exclusão falhou");
                    });
                  } catch (exc) { setError((exc as Error).message); }
                  finally { setBusy(false); }
                }}
                className="rounded-lg bg-red-600 px-4 py-2 text-sm font-semibold text-white hover:bg-red-500 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Excluir agora
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function TabButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: string }) {
  return <button type="button" onClick={onClick} className={`flex-1 rounded-md px-3 py-1.5 text-xs ${active ? "bg-zinc-700 text-zinc-100" : "text-zinc-400"}`}>{children}</button>;
}

function ObjectEditor({ object, busy, onOpen, onSave, onArchive, onRestore, onPurge }: {
  object: ObjectInfo; busy: boolean; onOpen: () => void;
  onSave: (changes: { display_name: string; label: string; gcs_uri: string }) => Promise<void>;
  onArchive: () => Promise<void>; onRestore: () => Promise<void>; onPurge: () => void;
}) {
  const [name, setName] = useState(object.display_name);
  const [label, setLabel] = useState(object.label);
  const [gcs, setGcs] = useState(object.gcs_uri ?? "");
  return (
    <section>
      <div className="flex flex-wrap items-start gap-4">
        <div><p className="text-xs text-zinc-400">Identificador imutável</p><h2 className="mt-1 font-mono text-lg text-zinc-200">{object.object_id}</h2></div>
        <div className="flex-1" />
        {!object.archived && <Button variant="ghost" onClick={onOpen}>Abrir operação</Button>}
        <span className={`rounded-full px-2 py-1 text-xs ${object.archived ? "bg-zinc-800 text-zinc-400" : "bg-emerald-950 text-emerald-300"}`}>{object.archived ? "Arquivado" : "Ativo"}</span>
      </div>
      <div className="mt-6 grid gap-4 rounded-xl border border-zinc-800 bg-zinc-900/35 p-5">
        <Field label="Nome de exibição" value={name} onChange={setName} />
        <Field label="Classe no dataset" value={label} onChange={setLabel} help="Usada em novos exports. O object_id e os manifestos antigos não mudam." />
        <Field label="Bucket GCS" value={gcs} onChange={setGcs} placeholder="gs://bucket/prefixo/" />
        <div><p className="text-xs text-zinc-400">Raízes locais (somente leitura)</p><p className="mt-1 truncate font-mono text-xs text-zinc-400">{object.videos_root}</p><p className="truncate font-mono text-xs text-zinc-400">{object.output_root}</p></div>
        {!object.archived && <Button variant="primary" disabled={busy || !name.trim() || !label.trim()} onClick={() => onSave({ display_name: name.trim(), label: label.trim(), gcs_uri: gcs.trim() })}>{busy && <Spinner />} Salvar alterações</Button>}
      </div>
      <div className="mt-6 rounded-xl border border-zinc-800 p-5">
        <h3 className="text-sm font-medium text-zinc-200">Ciclo de vida</h3>
        {object.archived ? (
          <div className="mt-3 flex flex-wrap gap-2"><Button variant="primary" onClick={onRestore} disabled={busy}>Restaurar</Button><button type="button" onClick={onPurge} className="rounded-md border border-red-900 px-3 py-1.5 text-xs text-red-400 hover:bg-red-950/30">Excluir permanentemente</button></div>
        ) : (
          <div className="mt-3"><button type="button" onClick={onArchive} disabled={busy} className="rounded-md border border-red-900 bg-red-950/20 px-3 py-1.5 text-xs text-red-300 hover:bg-red-950/40 disabled:cursor-not-allowed disabled:opacity-50">Arquivar objeto</button><p className="mt-2 text-xs text-zinc-400">Arquivar interrompe novos trabalhos e preserva todos os dados.</p></div>
        )}
      </div>
    </section>
  );
}

function CreateObjectForm({ busy, onCancel, onCreate }: { busy: boolean; onCancel: () => void; onCreate: (payload: { display_name: string; label?: string; gcs_uri?: string }) => Promise<void> }) {
  const [name, setName] = useState(""); const [label, setLabel] = useState(""); const [gcs, setGcs] = useState("");
  return <section><h2 className="text-xl font-semibold text-zinc-100">Novo objeto</h2><div className="mt-5 grid gap-4 rounded-xl border border-zinc-800 p-5"><Field label="Nome de exibição" value={name} onChange={setName} placeholder="Microfone" /><Field label="Classe no dataset" value={label} onChange={setLabel} placeholder="microphone" /><Field label="Bucket GCS" value={gcs} onChange={setGcs} placeholder="gs://bucket/prefixo/" /><div className="flex gap-2"><Button variant="primary" disabled={busy || !name.trim()} onClick={() => onCreate({ display_name: name.trim(), label: label.trim() || undefined, gcs_uri: gcs.trim() || undefined })}>{busy && <Spinner />} Criar</Button><Button variant="ghost" onClick={onCancel}>Cancelar</Button></div></div></section>;
}

function Field({ label, value, onChange, placeholder, help }: { label: string; value: string; onChange: (value: string) => void; placeholder?: string; help?: string }) {
  return <label className="block"><span className="text-xs text-zinc-400">{label}</span><input value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} className="mt-1.5 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-200 placeholder:text-zinc-400" />{help && <span className="mt-1 block text-[11px] text-zinc-400">{help}</span>}</label>;
}
