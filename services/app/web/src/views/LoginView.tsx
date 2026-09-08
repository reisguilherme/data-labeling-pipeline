import { useState } from "react";
import { useSession } from "../store/session";
import { Button, Spinner } from "../components/ui";

/**
 * Login sem senha.
 *
 * Isto é atribuição, não autorização: serve para carimbar quem triou o quê e
 * para a trava saber de quem é o vídeo. Qualquer pessoa que alcança o servidor
 * pode escolher qualquer nome — a proteção real é a rede, não esta tela.
 */
export function LoginView() {
  const users = useSession((s) => s.users);
  const login = useSession((s) => s.login);
  const addUser = useSession((s) => s.addUser);

  const [name, setName] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const pick = async (userId: string) => {
    setBusy(userId);
    setError(null);
    try {
      await login(userId);
    } catch (exc) {
      setError((exc as Error).message);
      setBusy(null);
    }
  };

  const create = async () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    setBusy("__new__");
    setError(null);
    try {
      const user = await addUser(trimmed);
      await login(user.user_id);
    } catch (exc) {
      setError((exc as Error).message);
      setBusy(null);
    }
  };

  return (
    <div className="grid h-full place-items-center p-8">
      <div className="w-full max-w-md">
        <h1 className="text-lg font-medium text-zinc-100">Quem está triando?</h1>
        <p className="mt-1 text-xs text-zinc-500">
          Sem senha — o nome serve para registrar autoria e evitar que duas
          pessoas peguem o mesmo vídeo.
        </p>

        {users.length > 0 && (
          <ul className="mt-5 space-y-1.5">
            {users.map((user) => (
              <li key={user.user_id}>
                <button
                  onClick={() => pick(user.user_id)}
                  disabled={busy !== null}
                  className="flex w-full items-center gap-3 rounded-md border border-zinc-800 bg-zinc-900/60 px-3 py-2.5 text-left transition-colors hover:border-zinc-700 hover:bg-zinc-900 disabled:opacity-40"
                >
                  <span
                    className="h-2.5 w-2.5 shrink-0 rounded-full"
                    style={{ background: user.color }}
                  />
                  <span className="flex-1 text-sm text-zinc-200">
                    {user.display_name}
                  </span>
                  {busy === user.user_id && <Spinner className="text-zinc-500" />}
                </button>
              </li>
            ))}
          </ul>
        )}

        <div className="mt-5 border-t border-zinc-800 pt-5">
          <label className="text-xs text-zinc-500">Novo contribuidor</label>
          <div className="mt-1.5 flex gap-2">
            <input
              aria-label="Nome do novo contribuidor"
              value={name}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && create()}
              placeholder="seu nome"
              className="flex-1 rounded-md border border-zinc-800 bg-zinc-950 px-3 py-1.5 text-sm text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
            />
            <Button variant="primary" onClick={create} disabled={!name.trim() || busy !== null}>
              entrar
            </Button>
          </div>
        </div>

        {error && (
          <p className="mt-3 rounded-md border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
            {error}
          </p>
        )}
      </div>
    </div>
  );
}
