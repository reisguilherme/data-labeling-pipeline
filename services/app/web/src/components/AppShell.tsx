import { useEffect, useMemo, type ReactNode } from "react";
import type { AppRoute } from "../lib/routes";
import { navigate } from "../lib/routes";
import { useLibrary } from "../store/library";
import { useSession } from "../store/session";
import { PipelineSidebar } from "./PipelineSidebar";

export function AppShell({
  route,
  children,
  editor = false,
}: {
  route: AppRoute;
  children: ReactNode;
  editor?: boolean;
}) {
  const activeObject = useSession((state) => state.activeObject);
  const objects = useSession((state) => state.objects);
  const user = useSession((state) => state.user);
  const openObject = useSession((state) => state.openObject);
  const pipelineCounts = useLibrary((state) => state.pipelineCounts);
  const videos = useLibrary((state) => state.videos);
  const refresh = useLibrary((state) => state.refresh);
  const refreshSam3 = useLibrary((state) => state.refreshSam3);
  const inFlight = useMemo(
    () => videos.some((video) => video.sam3 && ["queued", "leased", "running"].includes(video.sam3.state)),
    [videos],
  );

  useEffect(() => {
    if (activeObject) void refresh();
  }, [activeObject?.object_id, refresh]);

  useEffect(() => {
    if (!inFlight) return;
    const timer = window.setInterval(() => void refreshSam3(), 5000);
    return () => window.clearInterval(timer);
  }, [inFlight, refreshSam3]);

  if (!activeObject) return <>{children}</>;
  return (
    <div className="flex h-full flex-col overflow-hidden bg-zinc-950">
      <header className="flex h-14 shrink-0 items-center gap-3 border-b border-zinc-800 bg-zinc-950/95 px-4">
        <button
          type="button"
          onClick={() => navigate({ page: "operations", objectId: activeObject.object_id, stage: "overview" })}
          className="flex items-center gap-2 text-sm font-semibold tracking-tight text-zinc-100"
        >
          <span className="grid h-7 w-7 place-items-center rounded-lg bg-emerald-500 text-xs font-black text-zinc-950">B</span>
          <span className="hidden sm:inline">Boom Pipeline</span>
        </button>

        <span className="h-5 w-px bg-zinc-800" />
        <label className="sr-only" htmlFor="object-switcher">Objeto ativo</label>
        <select
          id="object-switcher"
          value={activeObject.object_id}
          onChange={(event) => {
            const objectId = event.target.value;
            const stage = route.page === "operations" ? route.stage : "overview";
            if (navigate({ page: "operations", objectId, stage })) openObject(objectId);
          }}
          className="max-w-52 rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200"
        >
          {objects.filter((item) => !item.archived).map((item) => (
            <option key={item.object_id} value={item.object_id}>{item.display_name}</option>
          ))}
        </select>

        <div className="flex-1" />
        {user && (
          <span className="flex items-center gap-2 text-xs text-zinc-400">
            <span className="h-2 w-2 rounded-full" style={{ backgroundColor: user.color }} />
            <span className="hidden sm:inline">{user.display_name}</span>
          </span>
        )}
      </header>
      <div className="flex min-h-0 flex-1">
        <PipelineSidebar
          objectId={activeObject.object_id}
          route={route}
          collapsed={editor}
          counts={pipelineCounts}
        />
        <main className="min-w-0 flex-1 overflow-hidden">{children}</main>
      </div>
    </div>
  );
}
