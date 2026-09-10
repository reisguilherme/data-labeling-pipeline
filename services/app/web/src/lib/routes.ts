import type { PipelineStage } from "../api/types";

export type OperationsStage = PipelineStage | "overview";
export type EditorKind = "triage" | "sam3" | "review";

export type AppRoute =
  | { page: "operations"; objectId: string; stage: OperationsStage }
  | { page: "editor"; objectId: string; videoId: string; editor: EditorKind }
  | { page: "ingest"; objectId: string }
  | { page: "export" }
  | { page: "objects" };

type NavigationGuard = () => boolean;
let navigationGuard: NavigationGuard | null = null;

/**
 * Token explicito para uma navegacao cuja confirmacao ja aconteceu.
 *
 * Fluxos assincronos consultam o guard, aguardam o ACK do backend e usam este
 * token para navegar sem apresentar uma segunda confirmacao.
 */
export const CONFIRMED_NAVIGATION = Symbol("confirmed-navigation");
export type NavigationBypass = typeof CONFIRMED_NAVIGATION;

const OPERATION_STAGES = new Set<OperationsStage>([
  "overview",
  "triage",
  "sam3",
  "review",
  "completed",
  "discarded",
]);
const EDITORS = new Set<EditorKind>(["triage", "sam3", "review"]);

export function parseRoute(pathname: string, fallbackObjectId?: string | null): AppRoute {
  const path = pathname.replace(/\/+$/, "") || "/";
  if (path === "/objects") return { page: "objects" };
  if (path === "/export") return { page: "export" };

  const editor = path.match(/^\/objects\/([^/]+)\/videos\/([^/]+)\/(triage|sam3|review)$/);
  if (editor && EDITORS.has(editor[3] as EditorKind)) {
    return {
      page: "editor",
      objectId: decodeURIComponent(editor[1]),
      videoId: decodeURIComponent(editor[2]),
      editor: editor[3] as EditorKind,
    };
  }

  const ingest = path.match(/^\/objects\/([^/]+)\/ingest$/);
  if (ingest) return { page: "ingest", objectId: decodeURIComponent(ingest[1]) };

  const operations = path.match(/^\/objects\/([^/]+)\/([^/]+)$/);
  if (operations && OPERATION_STAGES.has(operations[2] as OperationsStage)) {
    return {
      page: "operations",
      objectId: decodeURIComponent(operations[1]),
      stage: operations[2] as OperationsStage,
    };
  }

  return fallbackObjectId
    ? { page: "operations", objectId: fallbackObjectId, stage: "triage" }
    : { page: "objects" };
}

export function routePath(route: AppRoute): string {
  if (route.page === "objects") return "/objects";
  if (route.page === "export") return "/export";
  if (route.page === "ingest") return `/objects/${encodeURIComponent(route.objectId)}/ingest`;
  if (route.page === "editor") {
    return `/objects/${encodeURIComponent(route.objectId)}/videos/${encodeURIComponent(route.videoId)}/${route.editor}`;
  }
  return `/objects/${encodeURIComponent(route.objectId)}/${route.stage}`;
}

export function installNavigationGuard(guard: NavigationGuard): () => void {
  navigationGuard = guard;
  return () => {
    if (navigationGuard === guard) navigationGuard = null;
  };
}

export function confirmNavigation(): boolean {
  return !navigationGuard || navigationGuard();
}

export function navigate(
  route: AppRoute | string,
  replace = false,
  bypass?: NavigationBypass,
): boolean {
  if (bypass !== CONFIRMED_NAVIGATION && !confirmNavigation()) return false;
  const path = typeof route === "string" ? route : routePath(route);
  window.history[replace ? "replaceState" : "pushState"]({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
  return true;
}
