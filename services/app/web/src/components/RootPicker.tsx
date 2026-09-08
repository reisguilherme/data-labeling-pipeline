import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { DirListing } from "../api/types";
import { useSession } from "../store/session";
import { Button, Panel, Spinner } from "./ui";

/** Navegador de pastas server-side — o browser não entrega caminhos reais do disco. */
function DirBrowser({
  value,
  onPick,
}: {
  value: string | null;
  onPick: (path: string) => void;
}) {
  const [listing, setListing] = useState<DirListing | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async (path?: string) => {
    setLoading(true);
    setError(null);
    try {
      setListing(await api.listDirs(path));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load(value ?? undefined);
    // Carrega só na montagem: navegar depois é responsabilidade dos cliques.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="flex h-64 flex-col rounded-md border border-zinc-800 bg-zinc-950">
      <div className="flex items-center gap-2 border-b border-zinc-800 px-2 py-1.5">
        <Button
          variant="ghost"
          className="px-2 py-1 text-xs"
          disabled={!listing?.parent}
          onClick={() => listing?.parent && void load(listing.parent)}
        >
          ↑
        </Button>
        <span className="tnum flex-1 truncate text-xs text-zinc-400">
          {listing?.path ?? "…"}
        </span>
        {loading && <Spinner className="text-zinc-500" />}
        {listing && (
          <Button
            variant="primary"
            className="px-2 py-1 text-xs"
            onClick={() => onPick(listing.path)}
          >
            usar esta
          </Button>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-1">
        {error && <p className="p-2 text-xs text-red-400">{error}</p>}
        {listing?.video_count ? (
          <p className="px-2 py-1 text-[11px] text-emerald-500">
            {listing.video_count} vídeo(s) nesta pasta
          </p>
        ) : null}
        {listing?.dirs.map((dir) => (
          <button
            key={dir.path}
            onDoubleClick={() => void load(dir.path)}
            onClick={() => void load(dir.path)}
            className="block w-full truncate rounded px-2 py-1 text-left text-xs text-zinc-300 hover:bg-zinc-900"
          >
            <span className="mr-1.5 text-zinc-600">▸</span>
            {dir.name}
          </button>
        ))}
        {listing && listing.dirs.length === 0 && (
          <p className="p-2 text-xs text-zinc-600">nenhuma subpasta</p>
        )}
      </div>
    </div>
  );
}

/**
 * Escolha da raiz do workspace — a pasta que guarda os objetos, o registro de
 * usuários e as travas. Só aparece na primeira execução.
 *
 * Só funciona na máquina do servidor: o endpoint que lista pastas enumera o
 * disco inteiro do host, e com `--host 0.0.0.0` isso seria leitura da árvore de
 * diretórios para a rede toda. É um passo de setup, então é local.
 */
export function RootPicker({ onDone }: { onDone?: () => void }) {
  const config = useSession((s) => s.config);

  const [root, setRoot] = useState(config?.workspace_root ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setError(null);
    setBusy(true);
    try {
      await api.setWorkspace(root);
      onDone?.();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-2xl p-8">
      <h1 className="text-lg font-medium text-zinc-100">Onde fica o workspace?</h1>
      <p className="mt-1 mb-6 text-sm text-zinc-500">
        Uma pasta para todos os objetos. Cada objeto ganha{" "}
        <code className="text-zinc-400">&lt;objeto&gt;/raw</code> e{" "}
        <code className="text-zinc-400">&lt;objeto&gt;/dataset</code> aqui dentro.
      </p>

      {config && !config.ffmpeg.ok && (
        <div className="mb-6 rounded-md border border-red-900/60 bg-red-950/40 p-3">
          <p className="text-sm font-medium text-red-300">ffmpeg indisponível</p>
          <pre className="mt-1 text-xs whitespace-pre-wrap text-red-400/80">
            {config.ffmpeg.error}
          </pre>
        </div>
      )}

      {config && !config.can_browse_fs && (
        <div className="mb-6 rounded-md border border-amber-900/60 bg-amber-950/40 p-3 text-xs text-amber-300">
          A pasta do workspace só pode ser escolhida na própria máquina do
          servidor. Rode a ferramenta lá, ou passe <code>--workspace</code> na
          linha de comando.
        </div>
      )}

      <Panel title="Pasta do workspace">
        <input
          aria-label="Pasta do workspace"
          value={root}
          onChange={(e) => setRoot(e.target.value)}
          placeholder="D:\mst"
          className="mb-2 w-full rounded-md border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 placeholder:text-zinc-600"
        />
        {config?.can_browse_fs && (
          <DirBrowser value={root || null} onPick={setRoot} />
        )}
      </Panel>

      {error && <p className="mt-4 text-sm text-red-400">{error}</p>}

      <div className="mt-6">
        <Button variant="primary" disabled={!root || busy} onClick={() => void submit()}>
          {busy && <Spinner />}
          Usar esta pasta
        </Button>
      </div>
    </div>
  );
}
