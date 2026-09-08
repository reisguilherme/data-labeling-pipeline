/**
 * Escopo das chamadas: qual objeto está aberto e qual aba está falando.
 *
 * O objeto vai no CAMINHO da URL, não num header, por duas razões do browser:
 *
 *  1. `<img src>`, `<video src>` e `EventSource` não mandam header customizado —
 *     e o app carrega frames por <img> o tempo todo. Contornar isso exigiria
 *     buscar cada frame por fetch+blob, o que mataria o prefetch de ±30 frames.
 *  2. O cache do browser é chaveado por URL e os frames vão com
 *     `immutable, max-age=1 ano`. Como `video_id = sha1(relpath)` não é salgado
 *     por objeto, dois objetos com o mesmo relpath geram o MESMO id — com
 *     escopo por header, o browser serviria o frame do objeto errado.
 */

const CLIENT_KEY = "mst_client_id";
const OBJECT_KEY = "mst_object_id";

let objectId: string | null = null;

export function setObject(id: string | null): void {
  objectId = id;
  try {
    if (id) localStorage.setItem(OBJECT_KEY, id);
    else localStorage.removeItem(OBJECT_KEY);
  } catch {
    /* modo privado: o escopo em memória basta para a sessão */
  }
}

export function getObject(): string | null {
  if (objectId) return objectId;
  try {
    objectId = localStorage.getItem(OBJECT_KEY);
  } catch {
    objectId = null;
  }
  return objectId;
}

export function requireObject(): string {
  const id = getObject();
  if (!id) throw new Error("nenhum objeto selecionado");
  return id;
}

/** Prefixo das rotas escopadas. */
export function objectPath(): string {
  return `/api/objects/${encodeURIComponent(requireObject())}`;
}

/**
 * Identidade da ABA, não da pessoa.
 *
 * Em sessionStorage de propósito: sobrevive ao F5 (a trava do vídeo continua
 * sendo minha) mas não é compartilhado entre abas — duas abas do mesmo usuário
 * são dois clientes, o que é o que permite cancelar as extrações de uma sem
 * matar as da outra.
 */
export function clientId(): string {
  try {
    let id = sessionStorage.getItem(CLIENT_KEY);
    if (!id) {
      id = crypto.randomUUID();
      sessionStorage.setItem(CLIENT_KEY, id);
    }
    return id;
  } catch {
    return "anon";
  }
}
