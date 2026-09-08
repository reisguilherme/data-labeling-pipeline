import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import type { BBox, ReviewSegment, Sam3Session, VideoListItem } from "../api/types";
import { BboxCanvas } from "../components/BboxCanvas";
import { Button, Panel, Spinner } from "../components/ui";
import { cx } from "../lib/format";
import { useLibrary } from "../store/library";

/**
 * Controle da propagação do SAM3.
 *
 * A ideia inteira: a caixa que a triagem marcou é um retângulo feito à mão. A
 * partir dela o SAM3 pode segmentar o objeto — ou vazar para o fundo. Aqui você
 * ajusta a caixa, vê a máscara daquele ÚNICO frame, e só propaga para os
 * outros quando o resultado estiver bom. Descobrir o vazamento depois de
 * processar 800 frames custa refazer tudo.
 */
export function Sam3View({
  video,
  onBack,
  onReview,
}: {
  video: VideoListItem;
  onBack: () => void;
  onReview: (video: VideoListItem) => void;
}) {
  const [segments, setSegments] = useState<string[]>([]);
  const [segment, setSegment] = useState<string>("");
  const [info, setInfo] = useState<ReviewSegment | null>(null);
  const [boxes, setBoxes] = useState<BBox[]>([]);
  const [selected, setSelected] = useState(-1);
  const [session, setSession] = useState<Sam3Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showMask, setShowMask] = useState(true);
  const [propagating, setPropagating] = useState(false);
  const refreshPipeline = useLibrary((state) => state.refresh);

  const [rect, setRect] = useState<{ left: number; top: number; width: number; height: number } | null>(null);
  const imgRef = useRef<HTMLImageElement>(null);

  const preview = session?.preview ?? null;
  const busy = session?.state === "busy" || session?.state === "opening";
  const promptFrame = info?.prompt.frame_idx ?? 0;

  // -- carga do segmento ---------------------------------------------------

  useEffect(() => {
    let cancelado = false;
    setLoading(true);
    api
      .segmentReview(video.video_id, segment || "seg_00")
      .then((data) => {
        if (cancelado) return;
        setInfo(data);
        setSegments(data.segments);
        setSegment(data.segment);
        // A caixa de partida é a do frame do prompt — a que a triagem marcou,
        // ou o override já salvo aqui.
        const frameIdx = data.prompt.frame_idx ?? 0;
        setBoxes(
          data.prompt.objects?.length
            ? data.prompt.objects
            : (data.frames[frameIdx]?.boxes ?? []),
        );
        setLoading(false);
        void api.sam3Queue().then((queue) => {
          if (cancelado) return;
          if (queue.worker.state === "error" || queue.worker.state === "unavailable") {
            setError(queue.worker.message ?? "worker SAM3 indisponível");
          }
        }).catch(() => undefined);
      })
      .catch((exc) => {
        if (!cancelado) {
          setError((exc as Error).message);
          setLoading(false);
        }
      });
    return () => {
      cancelado = true;
    };
  }, [video.video_id, segment]);

  // -- sessão --------------------------------------------------------------

  // Enquanto a sessão está abrindo ou processando, o servidor segura a resposta
  // até algo mudar. Sem isto seria um laço de consultas a cada segundo.
  useEffect(() => {
    if (!session || !segment) return;
    if (session.state !== "opening" && session.state !== "busy") return;
    let cancelado = false;
    api
      .sam3Session(video.video_id, segment, 30)
      .then((next) => {
        if (!cancelado) setSession(next);
      })
      .catch(() => undefined);
    return () => {
      cancelado = true;
    };
  }, [session, segment, video.video_id]);

  // Mantém a sessão viva enquanto a tela está aberta. Sem isto, parar para
  // pensar na caixa por alguns minutos derrubaria o estado carregado na GPU, e
  // o próximo teste pagaria o carregamento inteiro de novo.
  useEffect(() => {
    if (!segment || !session || session.state === "closed" || session.state === "none") return;
    const timer = setInterval(() => {
      void api.sam3Session(video.video_id, segment, 0).then(setSession).catch(() => undefined);
    }, 60_000);
    return () => clearInterval(timer);
  }, [video.video_id, segment, session]);

  // Fechar a sessão devolve a VRAM em vez de esperar o tempo de inatividade.
  useEffect(() => {
    return () => {
      const id = session?.session_id;
      if (id) void api.sam3CloseSession(video.video_id, id).catch(() => undefined);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [video.video_id]);

  const testar = async () => {
    if (!boxes.length) {
      setError("desenhe uma caixa antes de testar");
      return;
    }
    setError(null);
    try {
      let ativa = session;
      if (!ativa || ativa.state === "closed" || ativa.state === "error" || ativa.state === "none") {
        ativa = await api.sam3OpenSession(video.video_id, segment, promptFrame);
        setSession(ativa);
      }
      if (!ativa.session_id) return;
      setSession(await api.sam3Preview(video.video_id, ativa.session_id, boxes));
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const propagar = async () => {
    setError(null);
    setPropagating(true);
    try {
      // Grava a caixa aprovada antes de enfileirar: é ela que o runner vai usar
      // em todos os frames.
      await api.sam3SaveOverride(video.video_id, segment, boxes);
      await api.sam3Enqueue(video.video_id, true);
      // Atualiza antes de voltar ao quadro. Sem isto o estado `queued` ainda
      // não existe na store, o polling não é ativado e a propagação parece
      // não ter acontecido até uma recarga manual da página.
      await refreshPipeline();
      const id = session?.session_id;
      if (id) await api.sam3CloseSession(video.video_id, id).catch(() => undefined);
      onBack();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setPropagating(false);
    }
  };

  // -- geometria da imagem -------------------------------------------------

  const measure = useCallback(() => {
    const image = imgRef.current;
    if (!image) return;
    const boxWidth = image.clientWidth;
    const boxHeight = image.clientHeight;
    const { naturalWidth, naturalHeight } = image;
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

  const src = segment ? api.segmentFrameUrl(video.video_id, segment, promptFrame) : "";
  const jaPropagado = video.sam3?.state === "done";

  const areaTexto = useMemo(() => {
    if (preview?.area_frac == null) return null;
    return `${(preview.area_frac * 100).toFixed(2)}% do frame`;
  }, [preview]);

  if (loading) {
    return (
      <div className="grid h-full place-items-center">
        <Spinner className="text-zinc-600" />
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex shrink-0 items-center gap-3 border-b border-zinc-800 px-4 py-2.5">
        <Button variant="ghost" onClick={onBack}>
          ← biblioteca
        </Button>
        <h1 className="max-w-sm truncate text-sm font-medium text-zinc-100">{video.name}</h1>

        {segments.length > 1 && (
          <select
            aria-label="Intervalo para pré-anotar"
            value={segment}
            onChange={(event) => {
              setSession(null);
              setSegment(event.target.value);
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

        <span className="tnum text-xs text-zinc-500">
          frame inicial do intervalo · {info?.frame_count ?? 0} frames no trecho
        </span>

        <div className="flex-1" />

        {session && session.state !== "none" && (
          <span
            className={cx(
              "tnum rounded px-1.5 py-0.5 text-[11px]",
              session.state === "ready"
                ? "bg-emerald-950/80 text-emerald-300"
                : session.state === "error"
                  ? "bg-red-950/80 text-red-300"
                  : "bg-zinc-900 text-zinc-400",
            )}
          >
            {session.state === "opening" && "carregando o segmento na GPU…"}
            {session.state === "busy" && "segmentando…"}
            {session.state === "ready" && "sessão pronta"}
            {session.state === "error" && (session.error ?? "erro")}
            {session.state === "closed" && "sessão encerrada"}
          </span>
        )}
      </header>

      {error && (
        <div className="shrink-0 bg-red-950/60 px-4 py-1.5 text-xs text-red-300">{error}</div>
      )}

      <div className="flex min-h-0 flex-1">
        <div className="relative min-w-0 flex-1 bg-black">
          <img
            ref={imgRef}
            src={src}
            alt="frame do prompt"
            className="h-full w-full object-contain"
            draggable={false}
            onLoad={measure}
          />

          {rect && (
            <div style={{ ...rect }} className="absolute">
              {/* A máscara vai por baixo das caixas: ela mostra o que o SAM3
                  pegou, e a caixa é o que você está ajustando. */}
              {showMask && preview?.mask_url && (
                <img
                  src={preview.mask_url}
                  alt=""
                  className="pointer-events-none absolute inset-0 h-full w-full"
                />
              )}
              <BboxCanvas
                bboxes={boxes}
                selected={selected}
                onChange={setBoxes}
                onSelect={setSelected}
                enabled
                zoom={1}
              />
              {/* Caixa que o SAM3 derivou da máscara — é o que viraria rótulo. */}
              {preview?.bbox && showMask && (
                <div
                  className="pointer-events-none absolute border border-dashed border-emerald-400"
                  style={{
                    left: `${preview.bbox[0] * 100}%`,
                    top: `${preview.bbox[1] * 100}%`,
                    width: `${(preview.bbox[2] - preview.bbox[0]) * 100}%`,
                    height: `${(preview.bbox[3] - preview.bbox[1]) * 100}%`,
                  }}
                />
              )}
            </div>
          )}

          {busy && (
            <div className="absolute inset-0 grid place-items-center bg-zinc-950/40">
              <span className="flex items-center gap-2 rounded bg-zinc-950/90 px-3 py-1.5 text-xs text-zinc-300">
                <Spinner />
                {session?.state === "opening"
                  ? "carregando o segmento na GPU (só na primeira vez)…"
                  : "segmentando o frame…"}
              </span>
            </div>
          )}
        </div>

        <aside className="w-80 shrink-0 space-y-3 overflow-y-auto border-l border-zinc-800 p-3">
          <Panel title="Caixa inicial">
            {boxes.length === 0 ? (
              <p className="text-xs text-zinc-500">
                arraste sobre o objeto para desenhar a caixa de partida
              </p>
            ) : (
              <ul className="space-y-1">
                {boxes.map((box, index) => (
                  <li
                    key={index}
                    className="tnum flex items-center gap-2 rounded bg-zinc-900 px-2 py-1 text-[11px] text-zinc-400"
                  >
                    <span className="text-amber-400">#{box.obj_id || index + 1}</span>
                    <span className="flex-1">
                      {box.normalized.map((v) => v.toFixed(3)).join(", ")}
                    </span>
                    <button
                      onClick={() => setBoxes(boxes.filter((_, i) => i !== index))}
                      className="text-zinc-600 hover:text-red-400"
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            )}

            <Button
              variant="primary"
              className="mt-3 w-full justify-center"
              onClick={testar}
              disabled={busy || !boxes.length}
            >
              {busy && <Spinner />} testar neste frame
            </Button>
            <p className="mt-2 text-[11px] text-zinc-600">
              Segmenta só este frame. A primeira vez carrega o segmento na GPU;
              os testes seguintes são rápidos.
            </p>
          </Panel>

          {preview && (
            <Panel
              title="Resultado"
              right={
                <label className="flex items-center gap-1.5 text-[11px] text-zinc-500">
                  <input
                    type="checkbox"
                    checked={showMask}
                    onChange={(event) => setShowMask(event.target.checked)}
                  />
                  máscara
                </label>
              }
            >
              {preview.error ? (
                <p className="text-xs text-amber-400">{preview.error}</p>
              ) : (
                <div className="space-y-1.5 text-xs text-zinc-400">
                  <p>
                    área segmentada:{" "}
                    <span className="tnum text-zinc-200">{areaTexto}</span>
                  </p>
                  {preview.bbox && (
                    <p className="tnum text-[11px] text-emerald-400">
                      caixa resultante:{" "}
                      {preview.bbox.map((v) => v.toFixed(3)).join(", ")}
                    </p>
                  )}
                  {preview.area_frac != null && preview.area_frac > 0.4 && (
                    <p className="rounded bg-amber-950/60 px-2 py-1 text-[11px] text-amber-300">
                      A máscara cobre quase metade do frame — provavelmente
                      vazou para o fundo. Tente uma caixa mais justa.
                    </p>
                  )}
                </div>
              )}
            </Panel>
          )}

          <Panel title="Propagar">
            <p className="mb-3 text-[11px] text-zinc-500">
              Aplica esta caixa aos {info?.frame_count ?? 0} frames do trecho.
              {jaPropagado && " Já foi propagado antes — isto refaz."}
            </p>
            <Button
              variant="primary"
              className="w-full justify-center"
              onClick={propagar}
              disabled={propagating || !boxes.length}
            >
              {propagating && <Spinner />} propagar para o trecho
            </Button>
            {jaPropagado && (
              <Button
                variant="ghost"
                className="mt-2 w-full justify-center"
                onClick={() => onReview(video)}
              >
                revisar o resultado atual →
              </Button>
            )}
          </Panel>
        </aside>
      </div>
    </div>
  );
}
