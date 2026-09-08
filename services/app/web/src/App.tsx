import { useEffect, useMemo, useState } from "react";
import { AppShell } from "./components/AppShell";
import { RootPicker } from "./components/RootPicker";
import { Spinner } from "./components/ui";
import { navigate, parseRoute, type AppRoute } from "./lib/routes";
import { useLibrary } from "./store/library";
import { useSession } from "./store/session";
import { AnnotatorView } from "./views/AnnotatorView";
import { GlobalDatasetView } from "./views/GlobalDatasetView";
import { IngestView } from "./views/IngestView";
import { LoginView } from "./views/LoginView";
import { ObjectsView } from "./views/ObjectsView";
import { OverviewView } from "./views/OverviewView";
import { OperationsView } from "./views/OperationsView";
import { ReviewView } from "./views/ReviewView";
import { Sam3View } from "./views/Sam3View";

function LoadingScreen({ message = "Carregando…" }: { message?: string }) {
  return (
    <div className="grid h-full place-items-center">
      <p className="flex items-center gap-2 text-sm text-zinc-500"><Spinner /> {message}</p>
    </div>
  );
}

export default function App() {
  const boot = useSession((state) => state.boot);
  const loading = useSession((state) => state.loading);
  const error = useSession((state) => state.error);
  const config = useSession((state) => state.config);
  const user = useSession((state) => state.user);
  const activeObject = useSession((state) => state.activeObject);
  const openObject = useSession((state) => state.openObject);
  const videos = useLibrary((state) => state.videos);
  const refresh = useLibrary((state) => state.refresh);
  const [route, setRoute] = useState<AppRoute>(() => parseRoute(window.location.pathname));

  useEffect(() => { void boot(); }, [boot]);
  useEffect(() => {
    const update = () => setRoute(parseRoute(window.location.pathname, useSession.getState().activeObject?.object_id));
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);

  useEffect(() => {
    if (!config || route.page === "objects" || route.page === "export") return;
    if (activeObject?.object_id !== route.objectId) openObject(route.objectId);
  }, [config, route, activeObject?.object_id, openObject]);

  useEffect(() => {
    if (!config || !activeObject || window.location.pathname !== "/") return;
    navigate({ page: "operations", objectId: activeObject.object_id, stage: "triage" }, true);
  }, [config, activeObject]);

  useEffect(() => {
    if (route.page !== "editor" || activeObject?.object_id !== route.objectId) return;
    if (!videos.some((video) => video.video_id === route.videoId)) void refresh();
  }, [route, activeObject?.object_id, videos, refresh]);

  const activeVideo = useMemo(
    () => route.page === "editor" ? videos.find((video) => video.video_id === route.videoId) ?? null : null,
    [route, videos],
  );

  if (error) {
    return (
      <div className="grid h-full place-items-center p-8">
        <div className="max-w-lg rounded-lg border border-red-900/60 bg-red-950/40 p-4">
          <p className="text-sm font-medium text-red-300">Não consegui falar com o backend</p>
          <p className="mt-1 text-xs text-red-400/80">{error}</p>
        </div>
      </div>
    );
  }
  if (loading || !config) return <LoadingScreen />;
  if (!config.workspace_ready) return <RootPicker onDone={() => void useSession.getState().boot()} />;
  if (!user) return <LoginView />;

  if (route.page === "objects" || !activeObject) {
    return (
      <ObjectsView
        onOpen={(objectId) => {
          openObject(objectId);
          navigate({ page: "operations", objectId, stage: "overview" });
        }}
      />
    );
  }

  if (route.page === "export") {
    return (
      <AppShell route={route}>
        <GlobalDatasetView />
      </AppShell>
    );
  }
  if (route.page === "ingest") {
    return (
      <AppShell route={route} editor>
        <IngestView onBack={() => navigate({ page: "operations", objectId: route.objectId, stage: "triage" })} />
      </AppShell>
    );
  }
  if (route.page === "operations") {
    return (
      <AppShell route={route}>
        {route.stage === "overview" ? <OverviewView /> : <OperationsView stage={route.stage} />}
      </AppShell>
    );
  }

  if (!activeVideo) {
    return <AppShell route={route} editor><LoadingScreen message="Carregando vídeo…" /></AppShell>;
  }

  const back = () => navigate({ page: "operations", objectId: route.objectId, stage: route.editor });
  if (route.editor === "triage") {
    return (
      <AppShell route={route} editor>
        <AnnotatorView
          video={activeVideo}
          onBack={back}
          onNavigate={(video) => navigate({ page: "editor", objectId: route.objectId, videoId: video.video_id, editor: "triage" })}
        />
      </AppShell>
    );
  }
  if (route.editor === "sam3") {
    return (
      <AppShell route={route} editor>
        <Sam3View
          video={activeVideo}
          onBack={back}
          onReview={(video) => navigate({ page: "editor", objectId: route.objectId, videoId: video.video_id, editor: "review" })}
        />
      </AppShell>
    );
  }
  return (
    <AppShell route={route} editor>
      <ReviewView video={activeVideo} onBack={back} />
    </AppShell>
  );
}
