import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "../api/client";
import type { MaskReviewDraft, MaskReviewFrame, VideoListItem } from "../api/types";
import { BboxCanvas } from "../components/BboxCanvas";
import { MaskEditor } from "../components/MaskEditor";
import { ReviewStrip } from "../components/ReviewStrip";
import { Button, Kbd, Spinner } from "../components/ui";
import { cx } from "../lib/format";
import { installNavigationGuard } from "../lib/routes";
import { useLibrary } from "../store/library";
import { useReview } from "../store/review";

// Frames 4K a ~250 KB: prefetch curto à frente (é para onde se anda) e mínimo
// atrás. Sem isto, cada seta espera um download.
const PREFETCH_AHEAD = 8;
const PREFETCH_BEHIND = 2;
const DISCARD_REVIEW_MESSAGE = "Há revisões ainda não salvas. Descartar?";

export function ReviewView({
  video,
  onBack,
}: {
  video: VideoListItem;
  onBack: () => void;
}) {
  const open = useReview((s) => s.open);
  const flush = useReview((s) => s.flush);
  const segment = useReview((s) => s.segment);
  const segments = useReview((s) => s.segments);
  const exportVersion = useReview((s) => s.exportVersion);
  const frames = useReview((s) => s.frames);
  const current = useReview((s) => s.current);
  const frameCount = useReview((s) => s.frameCount);
  const loading = useReview((s) => s.loading);
  const saving = useReview((s) => s.saving);
  const error = useReview((s) => s.error);
  const selectedBox = useReview((s) => s.selectedBox);
  const zoom = useReview((s) => s.zoom);
  const panX = useReview((s) => s.panX);
  const panY = useReview((s) => s.panY);
  const imageWidth = useReview((s) => s.imageWidth);
  const imageHeight = useReview((s) => s.imageHeight);
  const refreshPipeline = useLibrary((s) => s.refresh);
  const applyPipelineSnapshot = useLibrary((s) => s.applyPipelineSnapshot);

  const [rect, setRect] = useState<{ left: number; top: number; width: number; height: number } | null>(null);
  const [panning, setPanning] = useState(false);
  const [maskFrame, setMaskFrame] = useState<MaskReviewFrame | null>(null);
  const [maskLoading, setMaskLoading] = useState(false);
  const [maskUnavailable, setMaskUnavailable] = useState(false);
  const [maskLoadError, setMaskLoadError] = useState<string | null>(null);
  const [maskStates, setMaskStates] = useState<Record<number, MaskReviewFrame>>({});
  const [drafts, setDrafts] = useState<Record<number, MaskReviewDraft>>({});
  const [visited, setVisited] = useState<Set<number>>(() => new Set());
  const [committing, setCommitting] = useState(false);
  const [commitError, setCommitError] = useState<string | null>(null);
  const [toolControlsTarget, setToolControlsTarget] = useState<HTMLElement | null>(null);
  const [adjustmentControlsTarget, setAdjustmentControlsTarget] = useState<HTMLElement | null>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const pendingReviewRef = useRef(false);
  const navigationCommittedRef = useRef(false);

  const stagedReviewed = useMemo(() => {
    const reviewed = new Set(frames.filter((frame) => frame.status).map((frame) => frame.frame));
    for (const frame of visited) reviewed.add(frame);
    return reviewed.size;
  }, [frames, visited]);
  const stagedEdited = useMemo(() => {
    const edited = new Set(frames.filter((frame) => frame.status === "edited").map((frame) => frame.frame));
    for (const frame of Object.keys(drafts)) edited.add(Number(frame));
    return edited.size;
  }, [drafts, frames]);
  const entry = frames[current];
  const boxes = entry?.boxes ?? [];
  const maskBoxes = useMemo(
    () => (maskFrame?.instances ?? []).flatMap((instance) => (
      instance.bbox
        ? [{ obj_id: instance.obj_id, label: instance.label, normalized: instance.bbox }]
        : []
    )),
    [maskFrame],
  );

  useEffect(() => {
    void open(video.video_id, segments[0] ?? "seg_00");
    // Só na montagem: trocar de segmento é ação explícita.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [video.video_id]);

  // Descarrega o que estiver pendente ao sair — o autosave tem debounce, e sair
  // no meio dele perderia a última edição.
  useEffect(() => () => void flush(), [flush]);

  useEffect(() => {
    navigationCommittedRef.current = false;
    setMaskStates({});
    setDrafts({});
    setVisited(new Set());
    setCommitError(null);
    setMaskLoadError(null);
  }, [video.video_id, segment]);

  useEffect(() => {
    if (!segment || !exportVersion) return;
    let active = true;
    setMaskFrame(null);
    setMaskLoading(true);
    setMaskUnavailable(false);
    setMaskLoadError(null);
    void api
      .maskReviewFrame(video.video_id, segment, current, exportVersion)
      .then((data) => {
        if (!active) return;
        setMaskFrame(data);
        setMaskStates((states) => ({ ...states, [current]: data }));
        setVisited((framesSeen) => new Set(framesSeen).add(current));
        setMaskLoading(false);
      })
      .catch((exception: unknown) => {
        if (!active) return;
        setMaskFrame(null);
        if (exception instanceof ApiError && exception.code === "mask_run_unavailable") {
          setMaskUnavailable(true);
        } else {
          setMaskUnavailable(false);
          setMaskLoadError((exception as Error).message || "não foi possível carregar a máscara");
        }
        setMaskLoading(false);
      });
    return () => {
      active = false;
    };
  }, [video.video_id, segment, current, exportVersion]);

  const hasPendingReview = Object.keys(drafts).length > 0 || [...visited].some(
    (frame) => !maskStates[frame]?.status,
  );
  pendingReviewRef.current = hasPendingReview && !navigationCommittedRef.current;
  const canLeaveReview = useCallback(
    () => !hasPendingReview || window.confirm(DISCARD_REVIEW_MESSAGE),
    [hasPendingReview],
  );

  // Sidebar, logo, voltar e logout compartilham uma unica protecao. A ref
  // acompanha o render atual sem reinstalar o guard a cada frame visitado.
  useEffect(
    () => installNavigationGuard(
      () => !pendingReviewRef.current || window.confirm(DISCARD_REVIEW_MESSAGE),
    ),
    [],
  );

  useEffect(() => {
    if (!hasPendingReview) return;
    const warnBeforeClose = (event: BeforeUnloadEvent) => {
      if (navigationCommittedRef.current) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeClose);
    return () => window.removeEventListener("beforeunload", warnBeforeClose);
  }, [hasPendingReview]);

  const src = segment
    ? api.segmentFrameUrl(video.video_id, segment, current, exportVersion)
    : "";

  useEffect(() => {
    if (!segment) return;
    const images: HTMLImageElement[] = [];
    for (let offset = -PREFETCH_BEHIND; offset <= PREFETCH_AHEAD; offset += 1) {
      const target = current + offset;
      if (target < 0 || target >= frameCount || offset === 0) continue;
      const image = new Image();
      image.src = api.segmentFrameUrl(video.video_id, segment, target, exportVersion);
      images.push(image);
    }
    return () => images.forEach((image) => (image.src = ""));
  }, [video.video_id, segment, current, frameCount, exportVersion]);

  /** Retângulo ocupado pela imagem dentro do <img> com object-contain. */
  const measure = useCallback(() => {
    const image = imgRef.current;
    if (!image) return;
    const boxWidth = image.clientWidth;
    const boxHeight = image.clientHeight;
    const naturalWidth = image.naturalWidth;
    const naturalHeight = image.naturalHeight;
    if (!boxWidth || !boxHeight || !naturalWidth || !naturalHeight) return;
    const scale = Math.min(boxWidth / naturalWidth, boxHeight / naturalHeight);
    const width = naturalWidth * scale;
    const height = naturalHeight * scale;
    setRect({
      left: (boxWidth - width) / 2,
      top: (boxHeight - height) / 2,
      width,
      height,
    });
  }, []);

  useEffect(() => {
    const image = imgRef.current;
    if (!image) return;
    const observer = new ResizeObserver(measure);
    observer.observe(image);
    measure();
    return () => observer.disconnect();
  }, [measure]);

  // -- teclado -------------------------------------------------------------

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (target.tagName === "INPUT" || target.tagName === "TEXTAREA") return;
      const state = useReview.getState();

      switch (event.key) {
        case "ArrowRight":
          event.preventDefault();
          if (maskUnavailable) state.advance(1);
          else state.goto(current + 1);
          return;
        case "ArrowLeft":
          event.preventDefault();
          if (maskUnavailable) state.advance(-1);
          else state.goto(current - 1);
          return;
        case "Delete":
        case "Backspace":
          event.preventDefault();
          if (maskUnavailable) state.clearFrame();
          return;
        case "u":
        case "U":
          event.preventDefault();
          if (maskUnavailable) void state.resetFrame();
          return;
        case "0":
          event.preventDefault();
          state.resetView();
          return;
        case "+":
        case "=":
          event.preventDefault();
          state.setZoom(state.zoom * 1.3);
          return;
        case "-":
          event.preventDefault();
          state.setZoom(state.zoom / 1.3);
          return;
        case "Escape":
          event.preventDefault();
          onBack();
          return;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [current, maskUnavailable, onBack]);

  const saveSegment = useCallback(async () => {
    setCommitError(null);
    navigationCommittedRef.current = false;
    if (maskLoadError) {
      setCommitError("Recarregue o frame antes de salvar: " + maskLoadError);
      return;
    }
    if (maskUnavailable) {
      await useReview.getState().confirmRest();
      navigationCommittedRef.current = true;
      pendingReviewRef.current = false;
      void refreshPipeline();
      onBack();
      return;
    }
    const missing = Math.max(frameCount - stagedReviewed, 0);
    if (missing > 0) {
      setCommitError(`Ainda faltam ${missing} frame${missing === 1 ? "" : "s"} para visualizar.`);
      return;
    }

    const updates = [...visited]
      .sort((left, right) => left - right)
      .flatMap((frame) => {
        const state = maskStates[frame];
        const draft = drafts[frame];
        const persisted = state?.status;
        if (!state || (persisted && !draft)) return [];
        return [{
          frame,
          expected_revision: state.revision,
          status: draft ? "edited" as const : "ok" as const,
          instances: draft?.instances ?? [],
          retain_obj_ids: draft?.retain_obj_ids ?? [],
        }];
      });

    setCommitting(true);
    try {
      const result = await api.saveMaskReviewBatch(
        video.video_id,
        segment ?? "",
        updates,
        exportVersion,
      );

      // O lote já foi confirmado no manifesto canônico. Um diagnóstico de
      // certificação não pode deixar drafts antigos que causariam conflito na
      // próxima tentativa.
      setVisited(new Set());
      setDrafts({});
      if (result.completion_error) {
        await open(video.video_id, segment ?? "");
        setCommitError(`Revisão salva, mas não concluída: ${result.completion_error}`);
        return;
      }

      navigationCommittedRef.current = true;
      pendingReviewRef.current = false;
      if (result.pipeline) {
        applyPipelineSnapshot(video.video_id, result.pipeline);
      }
      if (result.video.complete) {
        onBack();
        if (result.projection_pending) {
          window.setTimeout(() => void refreshPipeline(), 2500);
        } else {
          void refreshPipeline();
        }
        return;
      }
      const next = result.video.segments.find((item) => !item.complete);
      if (next) {
        navigationCommittedRef.current = false;
        await open(video.video_id, next.segment);
      }
      else onBack();
    } catch (exception) {
      navigationCommittedRef.current = false;
      setCommitError((exception as Error).message);
    } finally {
      setCommitting(false);
    }
  }, [applyPipelineSnapshot, drafts, exportVersion, frameCount, maskLoadError, maskStates, maskUnavailable, onBack, open, refreshPipeline, segment, stagedReviewed, video.video_id, visited]);

  const onWheel = useCallback((event: React.WheelEvent) => {
    const container = event.currentTarget.getBoundingClientRect();
    const focus = {
      x: (event.clientX - container.left) / container.width,
      y: (event.clientY - container.top) / container.height,
    };
    const state = useReview.getState();
    state.setZoom(state.zoom * (event.deltaY < 0 ? 1.18 : 1 / 1.18), focus);
  }, []);

  const startPan = useCallback((event: React.PointerEvent) => {
    const state = useReview.getState();
    if (state.zoom <= 1) return;
    // O arrasto esquerdo desenha a caixa, então o pan exige Alt ou o botão do
    // meio — mesma convenção da tela de triagem.
    if (!(event.button === 1 || event.altKey)) return;
    event.preventDefault();
    event.stopPropagation();
    setPanning(true);
    const rectNow = event.currentTarget.getBoundingClientRect();
    let lastX = event.clientX;
    let lastY = event.clientY;
    const move = (moveEvent: PointerEvent) => {
      const dx = (moveEvent.clientX - lastX) / rectNow.width / state.zoom;
      const dy = (moveEvent.clientY - lastY) / rectNow.height / state.zoom;
      lastX = moveEvent.clientX;
      lastY = moveEvent.clientY;
      useReview.getState().nudgePan(-dx, -dy);
    };
    const up = () => {
      setPanning(false);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }, []);

  if (loading) {
    return (
      <div className="grid h-full place-items-center">
        <Spinner className="text-zinc-600" />
      </div>
    );
  }

  const transform =
    zoom > 1 ? `scale(${zoom}) translate(${-panX * 100}%, ${-panY * 100}%)` : undefined;
  const percent = frameCount ? Math.round((stagedReviewed / frameCount) * 100) : 0;
  const currentDraft = drafts[current] ?? null;
  const currentStatus = currentDraft
    ? "edited"
    : visited.has(current) || entry?.status
      ? entry?.status === "edited" ? "edited" : "ok"
      : null;
  const missingFrames = Math.max(frameCount - stagedReviewed, 0);

  return (
    <div className="flex h-full flex-col">
      <header className="flex shrink-0 items-center gap-3 border-b border-zinc-800 px-4 py-2.5">
        <Button variant="ghost" onClick={onBack}>
          ← biblioteca
        </Button>
        <h1 className="max-w-md truncate text-sm font-medium text-zinc-100">{video.name}</h1>

        {segments.length > 1 && (
          <select
            aria-label="Intervalo para revisar"
            value={segment ?? ""}
            onChange={(event) => {
              if (canLeaveReview()) {
                pendingReviewRef.current = false;
                setDrafts({});
                setVisited(new Set());
                setCommitError(null);
                void open(video.video_id, event.target.value);
              }
            }}
            className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1 text-xs text-zinc-300"
          >
            {segments.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        )}

        <div className="flex items-center gap-2">
          <div className="h-1.5 w-32 overflow-hidden rounded-full bg-zinc-800">
            <div
              className="h-full rounded-full bg-violet-500 transition-[width]"
              style={{ width: `${percent}%` }}
            />
          </div>
          <span className="tnum text-xs text-zinc-500">
            {stagedReviewed}/{frameCount} conferidos
          </span>
          {stagedEdited > 0 && (
            <span className="tnum text-xs text-amber-500">{stagedEdited} corrigidos</span>
          )}
        </div>

        <div className="flex-1" />
        {(saving || committing || maskLoading) && <Spinner className="text-zinc-600" />}
        <span className="tnum text-xs text-zinc-600">{zoom.toFixed(1)}×</span>
      </header>

      {(error || maskLoadError) && (
        <div className="shrink-0 bg-red-950/60 px-4 py-1.5 text-xs text-red-300">
          {error || maskLoadError}
        </div>
      )}

      <div className="flex min-h-0 flex-1 bg-zinc-950">
        <aside
          ref={setToolControlsTarget}
          aria-label="Ferramentas de máscara"
          className="w-28 shrink-0 border-r border-zinc-800 bg-zinc-950"
        />
        <div
          aria-label="Área do frame para revisão"
          className={cx(
            "relative min-h-0 min-w-0 flex-1 overflow-hidden bg-black",
            panning ? "cursor-grabbing" : undefined,
          )}
          onWheel={onWheel}
          onPointerDown={startPan}
        >
        <div
          data-review-camera-plane
          className="relative h-full w-full"
          style={{ transform, transformOrigin: "center center" }}
        >
          <img
            ref={imgRef}
            src={src}
            alt={`frame ${current}`}
            className="absolute inset-0 block h-full w-full select-none object-contain"
            draggable={false}
            onLoad={measure}
          />
          {rect && (
            <div style={{ ...rect }} className="absolute">
              {maskFrame && !maskUnavailable && imageWidth > 0 && imageHeight > 0 ? (
                <>
                  <MaskEditor
                    key={`${segment}:${current}`}
                    frame={maskFrame}
                    draft={currentDraft}
                    imageWidth={imageWidth}
                    imageHeight={imageHeight}
                    toolControlsTarget={toolControlsTarget}
                    adjustmentControlsTarget={adjustmentControlsTarget}
                    onDraftChange={(draft) => {
                      navigationCommittedRef.current = false;
                      setDrafts((currentDrafts) => ({ ...currentDrafts, [current]: draft }));
                    }}
                  />
                  <BboxCanvas
                    bboxes={maskBoxes}
                    selected={-1}
                    onChange={() => undefined}
                    onSelect={() => undefined}
                    enabled={false}
                    zoom={zoom}
                    ariaLabel="BBox derivada da máscara"
                  />
                </>
              ) : maskUnavailable ? (
                <BboxCanvas
                  bboxes={boxes}
                  selected={selectedBox}
                  onChange={(next) => useReview.getState().setBoxes(next)}
                  onSelect={(index) => useReview.getState().selectBox(index)}
                  enabled
                  zoom={zoom}
                />
              ) : null}
            </div>
          )}
        </div>

        </div>
      </div>

      <div className="flex shrink-0 flex-wrap items-center gap-3 border-t border-zinc-800 bg-zinc-900 px-3 py-2">
        <span
          className={cx(
            "tnum rounded px-1.5 py-0.5 text-[11px]",
            currentStatus === "edited"
              ? "bg-amber-950 text-amber-300"
              : currentStatus === "ok"
                ? "bg-violet-950 text-violet-300"
                : "bg-zinc-800 text-zinc-400",
          )}
        >
          frame {current} / {frameCount - 1}
          {currentStatus === "edited"
            ? " · corrigido"
            : currentStatus === "ok"
              ? " · conferido"
              : " · pendente"}
        </span>
        {maskLoading && <span className="text-xs text-zinc-400">carregando máscara…</span>}
        {(maskUnavailable ? boxes.length === 0 : maskFrame?.instances.length === 0) && (
          <span className="text-xs text-zinc-500">sem objeto neste frame</span>
        )}
        <div
          ref={setAdjustmentControlsTarget}
          aria-label="Ajustes da máscara"
          className="min-w-0 flex-1"
        />
      </div>

      <ReviewStrip
        videoId={video.video_id}
        segment={segment ?? ""}
        exportVersion={exportVersion}
        frames={frames}
        current={current}
        onPick={(frame) => useReview.getState().goto(frame)}
      />

      <footer className="flex shrink-0 flex-wrap items-center gap-3 border-t border-zinc-800 px-4 py-2 text-xs text-zinc-500">
        <span className="flex items-center gap-1.5">
          <Kbd>←</Kbd>
          <Kbd>→</Kbd> navega e confere automaticamente
        </span>
        <span className="flex items-center gap-1.5">
          <Kbd>+</Kbd>
          <Kbd>−</Kbd>
          <Kbd>0</Kbd> zoom · <Kbd>Alt</Kbd>+arraste move
        </span>
        <div className="flex-1" />
        {commitError && <span className="text-red-400">{commitError}</span>}
        <span className={missingFrames > 0 ? "text-zinc-400" : "text-emerald-400"}>
          {missingFrames > 0
            ? `${missingFrames} frame${missingFrames === 1 ? "" : "s"} ainda não visualizado${missingFrames === 1 ? "" : "s"}`
            : "trecho pronto para salvar"}
        </span>
        <Button
          variant="primary"
          onClick={() => void saveSegment()}
          disabled={committing || maskLoading || Boolean(maskLoadError)}
        >
          {committing ? "Salvando…" : "Salvar trecho"}
        </Button>
      </footer>
    </div>
  );
}
