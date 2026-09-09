import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { cx } from "../lib/format";
import { frameInsideMarked, useAnnotator, ZOOM_MAX, ZOOM_MIN } from "../store/annotator";
import { BboxCanvas } from "./BboxCanvas";
import { Spinner } from "./ui";

const PREFETCH_RADIUS = 30;
const FULL_PREFETCH_RADIUS = 6;

interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
}

type FrameTier = "small" | "full";

/**
 * Exibição frame-exata.
 *
 * Trocar o `src` de um <img> é a forma mais confiável de mostrar um frame
 * específico: o índice na URL é o mesmo contador que o ffmpeg usou para extrair e
 * que o export vai usar. Nada de seek, nada de aproximação.
 */
export function FramePreview({ videoId }: { videoId: string }) {
  const frame = useAnnotator((s) => s.currentFrame);
  const phase = useAnnotator((s) => s.phase);
  const intervals = useAnnotator((s) => s.intervals);
  const selected = useAnnotator((s) => s.selected);
  const selectedBbox = useAnnotator((s) => s.selectedBbox);
  const setBboxes = useAnnotator((s) => s.setBboxes);
  const selectBbox = useAnnotator((s) => s.selectBbox);
  const meta = useAnnotator((s) => s.meta);
  const proxy = useAnnotator((s) => s.proxy);
  const playing = useAnnotator((s) => s.playing);
  const zoom = useAnnotator((s) => s.zoom);
  const panX = useAnnotator((s) => s.panX);
  const panY = useAnnotator((s) => s.panY);
  const adjust = useAnnotator((s) => s.adjust);

  const [missing, setMissing] = useState(false);
  const [loading, setLoading] = useState(false);
  const [recoveryError, setRecoveryError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [rect, setRect] = useState<Rect | null>(null);
  const [panning, setPanning] = useState(false);
  const imgRef = useRef<HTMLImageElement>(null);
  const recoveryAttemptRef = useRef<string | null>(null);
  const recoverySucceededRef = useRef<string | null>(null);
  const previousAvailabilityRef = useRef<string | null>(null);

  const interval = selected >= 0 ? intervals[selected] : null;
  // O bbox pertence ao frame do prompt: só é desenhado e só APARECE ali.
  const onPromptFrame = interval?.start != null && interval.start === frame;
  const bboxEditable = phase === "bbox" && onPromptFrame;
  const insideMarked = frameInsideMarked(intervals, frame);

  // Parado, mostra a resolução original — é onde se decide se aquilo é mesmo o
  // boom. Tocando, usa a versão pequena: 24 fps de JPEG 4K (~300 KB cada) não
  // sustenta reprodução fluida, e durante a passagem o detalhe não é o que importa.
  const preferredTier: FrameTier = playing ? "small" : "full";
  const availabilityKey = `${proxy?.mode ?? "none"}:${proxy?.complete ?? false}:${JSON.stringify(proxy?.available_ranges ?? [])}`;
  const [requestedTier, setRequestedTier] = useState<FrameTier>(preferredTier);
  const src = api.frameUrl(videoId, frame, requestedTier);

  /**
   * Retângulo realmente ocupado pela imagem dentro do <img>.
   *
   * A imagem preenche o palco inteiro com `object-contain`, o que gera barras
   * pretas nas laterais. O overlay de bbox precisa cobrir só o conteúdo, senão as
   * coordenadas normalizadas saem deslocadas do que o SAM3 vai receber.
   */
  const measure = useCallback(() => {
    const image = imgRef.current;
    if (!image) return;
    const boxWidth = image.clientWidth;
    const boxHeight = image.clientHeight;
    const naturalWidth = image.naturalWidth || meta?.media.width || 0;
    const naturalHeight = image.naturalHeight || meta?.media.height || 0;
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
  }, [meta]);

  useEffect(() => {
    recoveryAttemptRef.current = null;
    recoverySucceededRef.current = null;
    setRequestedTier(preferredTier);
    setMissing(false);
    setRecoveryError(null);
    setLoading(true);
  }, [videoId, frame, preferredTier]);

  useEffect(() => {
    if (
      previousAvailabilityRef.current !== null &&
      previousAvailabilityRef.current !== availabilityKey
    ) {
      setLoading(true);
      setReloadKey((value) => value + 1);
    }
    previousAvailabilityRef.current = availabilityKey;
  }, [availabilityKey]);

  useEffect(() => {
    const image = imgRef.current;
    if (!image) return;
    const observer = new ResizeObserver(measure);
    observer.observe(image);
    measure();
    return () => observer.disconnect();
  }, [measure]);

  useEffect(() => {
    if (!proxy?.complete) return;
    // Raio menor no tier grande: cada frame 4K é ~20x mais pesado, e prefetchar
    // 60 deles a cada passo entupiria a rede em vez de acelerar a navegação.
    const radius = requestedTier === "full" ? FULL_PREFETCH_RADIUS : PREFETCH_RADIUS;
    const images: HTMLImageElement[] = [];
    for (let offset = -radius; offset <= radius; offset += 1) {
      const target = frame + offset;
      if (target < 0 || offset === 0) continue;
      const image = new Image();
      image.src = api.frameUrl(videoId, target, requestedTier);
      images.push(image);
    }
    return () => images.forEach((image) => (image.src = ""));
  }, [videoId, frame, requestedTier, proxy?.complete]);

  const recoverFrame = useCallback(() => {
    const recoveryKey = `${videoId}:${frame}:${availabilityKey}`;
    if (recoveryAttemptRef.current === recoveryKey) {
      if (recoverySucceededRef.current === recoveryKey) {
        setMissing(true);
        setLoading(false);
        setRecoveryError("O frame continuou indisponível após o reparo. Tente novamente.");
      }
      return;
    }
    recoveryAttemptRef.current = recoveryKey;
    recoverySucceededRef.current = null;
    setMissing(true);
    setLoading(true);
    setRecoveryError(null);
    void useAnnotator
      .getState()
      .ensureFrameAvailable(frame)
      .then(() => {
        if (recoveryAttemptRef.current !== recoveryKey) return;
        recoverySucceededRef.current = recoveryKey;
        setRequestedTier(preferredTier);
        setLoading(true);
        setReloadKey((value) => value + 1);
      })
      .catch((error) => {
        if (recoveryAttemptRef.current !== recoveryKey) return;
        setLoading(false);
        setRecoveryError((error as Error).message || "não foi possível extrair este frame");
      });
  }, [videoId, frame, availabilityKey, preferredTier]);

  // -- zoom e pan ---------------------------------------------------------

  const onWheel = useCallback(
    (event: React.WheelEvent) => {
      const container = event.currentTarget.getBoundingClientRect();
      // Foco em coordenada do frame para ampliar exatamente sob o cursor.
      const focus = {
        x: (event.clientX - container.left) / container.width,
        y: (event.clientY - container.top) / container.height,
      };
      const state = useAnnotator.getState();
      state.setZoom(state.zoom * (event.deltaY < 0 ? 1.18 : 1 / 1.18), focus);
    },
    [],
  );

  const startPan = useCallback(
    (event: React.PointerEvent) => {
      const state = useAnnotator.getState();
      if (state.zoom <= 1) return;
      // Em modo bbox o arrasto esquerdo desenha, então o pan exige Alt ou o botão
      // do meio. Fora dele, arrastar move a imagem.
      const wantsPan = event.button === 1 || event.altKey || state.phase !== "bbox";
      if (!wantsPan) return;

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
        useAnnotator.getState().nudgePan(-dx, -dy);
      };
      const up = () => {
        setPanning(false);
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    },
    [],
  );

  // Verde diferencia os modos de trabalho; assistindo, só acende quando o frame
  // já pertence a um intervalo marcado ("esse trecho já foi").
  const outlineColor =
    phase === "bbox"
      ? "rgb(52 211 153)"
      : phase === "refine"
        ? "rgb(5 150 105 / 0.7)"
        : insideMarked
          ? "rgb(16 185 129)"
          : null;

  const zoomed = zoom > 1;
  const transform = zoomed
    ? `scale(${zoom}) translate(${-panX * 100}%, ${-panY * 100}%)`
    : undefined;
  const filter =
    adjust.brightness === 1 && adjust.contrast === 1 && adjust.saturate === 1
      ? undefined
      : `brightness(${adjust.brightness}) contrast(${adjust.contrast}) saturate(${adjust.saturate})`;

  return (
    <div
      className={cx(
        "relative h-full w-full overflow-hidden bg-black",
        panning ? "cursor-grabbing" : zoomed && phase !== "bbox" ? "cursor-grab" : undefined,
      )}
      onWheel={onWheel}
      onPointerDown={startPan}
    >
      {/* Um único wrapper transformado: o overlay de bbox escala junto com a
          imagem, então as coordenadas normalizadas continuam válidas em qualquer
          zoom — o canvas mede o próprio getBoundingClientRect, que já reflete a
          transformação. */}
      <div
        className="h-full w-full"
        style={{ transform, transformOrigin: "center center", filter }}
      >
        <img
          ref={imgRef}
          key={`${src}#${reloadKey}`}
          src={src}
          alt={`frame ${frame}`}
          className="h-full w-full object-contain"
          draggable={false}
          onLoad={() => {
            setLoading(false);
            setMissing(false);
            setRecoveryError(null);
            measure();
          }}
          onError={() => {
            setLoading(false);
            if (requestedTier === "full") {
              setRequestedTier("small");
              setRecoveryError(null);
              setLoading(true);
              return;
            }
            recoverFrame();
          }}
        />

        {/*
          A moldura nunca usa `border`: os 2px comiam a área útil
          (box-sizing: border-box), o que impedia posicionar um bbox rente à borda
          do frame E deslocava o overlay em relação à imagem — as coordenadas
          normalizadas saíam sistematicamente erradas em ~2px.

          Em estilo inline de propósito: a espessura não pode depender de qual
          utilitário do Tailwind acaba gerado, porque qualquer valor que volte a
          entrar no box aqui distorce a área de anotação.

          A espessura é dividida pelo zoom porque esta div está DENTRO do wrapper
          transformado: sem isso a moldura de 2px vira 16px em 8×. E é `box-shadow`
          e não `outline` porque o Chrome arredonda `outline-width` para pixel
          inteiro (mínimo 1px), o que devolveria 8px de tela em 8× — box-shadow
          aceita sub-pixel e também fica fora do modelo de caixa. Mesmo motivo da
          cromagem do BboxCanvas: ver o comentário longo lá.
        */}
        {rect && (
          <div
            style={{
              ...rect,
              boxShadow: outlineColor
                ? `0 0 0 ${2 / Math.max(zoom, 1)}px ${outlineColor}`
                : undefined,
            }}
            className="absolute"
          >
            {interval && onPromptFrame && (
              <BboxCanvas
                bboxes={interval.bboxes}
                selected={selectedBbox}
                onChange={setBboxes}
                onSelect={selectBbox}
                enabled={bboxEditable}
                zoom={zoom}
              />
            )}
          </div>
        )}
      </div>

      {zoomed && (
        <span className="tnum pointer-events-none absolute top-2 left-2 rounded bg-zinc-950/80 px-1.5 py-0.5 text-[10px] text-zinc-300">
          {zoom.toFixed(1)}× · scroll ajusta, arraste move, 0 reseta
        </span>
      )}

      {phase === "scan" && insideMarked && (
        <span className="pointer-events-none absolute top-2 right-2 rounded bg-emerald-950/85 px-1.5 py-0.5 text-[10px] text-emerald-300">
          trecho já marcado
        </span>
      )}

      {loading && !missing && (
        <div className="absolute right-3 bottom-3">
          <Spinner className="text-zinc-500" />
        </div>
      )}

      {missing && (
        <div className="absolute inset-0 grid place-items-center bg-zinc-950/80">
          <div className="flex flex-col items-center gap-2 text-xs text-zinc-400">
            {recoveryError ? (
              <>
                <p>{recoveryError}</p>
                <button
                  type="button"
                  className="rounded border border-zinc-700 px-3 py-1 text-zinc-200 hover:bg-zinc-800"
                  onClick={() => {
                    recoveryAttemptRef.current = null;
                    recoverySucceededRef.current = null;
                    recoverFrame();
                  }}
                >
                  tentar novamente
                </button>
              </>
            ) : (
              <p className="flex items-center gap-2">
                <Spinner /> extraindo os frames deste vídeo…
              </p>
            )}
          </div>
        </div>
      )}

      {phase === "bbox" && !onPromptFrame && interval?.start != null && (
        <div className="absolute inset-x-0 bottom-0 bg-amber-950/80 px-3 py-1.5 text-center text-xs text-amber-300">
          o bbox é desenhado no frame inicial ({interval.start}) — aperte{" "}
          <span className="font-medium">B</span> para ir até lá
        </div>
      )}
    </div>
  );
}

export { ZOOM_MAX, ZOOM_MIN };
