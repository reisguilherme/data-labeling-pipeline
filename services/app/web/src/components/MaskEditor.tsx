import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { MaskReviewDraft, MaskReviewFrame } from "../api/types";
import { cx } from "../lib/format";
import { useReview } from "../store/review";
import { Button, Kbd } from "./ui";

interface MaskEditorProps {
  frame: MaskReviewFrame;
  draft: MaskReviewDraft | null;
  imageWidth: number;
  imageHeight: number;
  toolControlsTarget: HTMLElement | null;
  adjustmentControlsTarget: HTMLElement | null;
  onDraftChange: (draft: MaskReviewDraft) => void;
}

function dataUrlBase64(dataUrl: string): string {
  const comma = dataUrl.indexOf(",");
  if (comma === -1) throw new Error("PNG do editor invalido");
  return dataUrl.slice(comma + 1);
}

export function MaskEditor({
  frame,
  draft,
  imageWidth,
  imageHeight,
  toolControlsTarget,
  adjustmentControlsTarget,
  onDraftChange,
}: MaskEditorProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [selected, setSelected] = useState(0);
  const [tool, setTool] = useState<"move" | "brush" | "eraser">("brush");
  const [brushShape, setBrushShape] = useState<"circle" | "square">("circle");
  const [brushSize, setBrushSize] = useState(32);
  const [opacity, setOpacity] = useState(0.3);
  const [maskVisible, setMaskVisible] = useState(true);
  const [spaceHeld, setSpaceHeld] = useState(false);
  const [cursor, setCursor] = useState<{ x: number; y: number; size: number } | null>(null);
  const [history, setHistory] = useState<string[]>([]);
  const [historyIndex, setHistoryIndex] = useState(-1);
  const [edited, setEdited] = useState<Record<number, string>>(() => Object.fromEntries(
    (draft?.instances ?? []).map((instance) => [
      instance.obj_id,
      `data:image/png;base64,${instance.png_base64}`,
    ]),
  ));
  const [empty, setEmpty] = useState(
    Boolean(draft && draft.instances.length === 0 && draft.retain_obj_ids.length === 0),
  );

  const publishDraft = useCallback((changes: Record<number, string>, isEmpty = false) => {
    setEdited(changes);
    setEmpty(isEmpty);
    if (isEmpty) {
      onDraftChange({ status: "edited", instances: [], retain_obj_ids: [] });
      return;
    }
    onDraftChange({
      status: "edited",
      instances: frame.instances
        .filter((instance) => Boolean(changes[instance.obj_id]))
        .map((instance) => ({
          obj_id: instance.obj_id,
          label: instance.label,
          png_base64: dataUrlBase64(changes[instance.obj_id]),
        })),
      retain_obj_ids: frame.instances
        .filter((instance) => !changes[instance.obj_id])
        .map((instance) => instance.obj_id),
    });
  }, [frame.instances, onDraftChange]);

  const snapshot = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const url = canvas.toDataURL("image/png");
    setHistory((current) => [...current.slice(0, historyIndex + 1), url].slice(-20));
    setHistoryIndex((current) => Math.min(current + 1, 19));
    const instance = frame.instances[selected];
    if (instance) publishDraft({ ...edited, [instance.obj_id]: url });
  }, [edited, frame.instances, historyIndex, publishDraft, selected]);

  const renderUrl = useCallback(
    (url: string) => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      canvas.width = imageWidth;
      canvas.height = imageHeight;
      const context = canvas.getContext("2d", { willReadFrequently: true });
      if (!context) return;
      const source = new Image();
      source.onload = () => {
        context.clearRect(0, 0, imageWidth, imageHeight);
        context.drawImage(source, 0, 0, imageWidth, imageHeight);
        const pixels = context.getImageData(0, 0, imageWidth, imageHeight);
        for (let index = 0; index < pixels.data.length; index += 4) {
          const positive =
            pixels.data[index + 3] > 0 &&
            pixels.data[index] + pixels.data[index + 1] + pixels.data[index + 2] > 384;
          pixels.data[index] = 255;
          pixels.data[index + 1] = 255;
          pixels.data[index + 2] = 255;
          pixels.data[index + 3] = positive ? 255 : 0;
        }
        context.putImageData(pixels, 0, 0);
        const initial = canvas.toDataURL("image/png");
        setHistory([initial]);
        setHistoryIndex(0);
      };
      source.src = url;
    },
    [imageHeight, imageWidth],
  );

  useEffect(() => {
    setSelected(0);
    setHistory([]);
    setHistoryIndex(-1);
    // O draft muda a cada pincelada. Reinicializar aqui apagaria undo/redo;
    // a troca de frame remonta o editor com o draft daquele frame.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [frame.frame, frame.revision]);

  useEffect(() => {
    if (empty) {
      const canvas = canvasRef.current;
      if (canvas) {
        canvas.width = imageWidth;
        canvas.height = imageHeight;
        canvas.getContext("2d")?.clearRect(0, 0, imageWidth, imageHeight);
      }
      return;
    }
    const instance = frame.instances[selected];
    if (instance) renderUrl(edited[instance.obj_id] ?? instance.mask_url);
    // `edited` is intentionally read from the render that selected this instance;
    // adding it as a dependency would reload the canvas after every brush stroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [empty, frame.instances, imageHeight, imageWidth, renderUrl, selected]);

  const restore = useCallback((index: number) => {
    const url = history[index];
    if (!url) return;
    setHistoryIndex(index);
    const image = new Image();
    image.onload = () => {
      const canvas = canvasRef.current;
      const context = canvas?.getContext("2d");
      if (!canvas || !context) return;
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.drawImage(image, 0, 0);
      const instance = frame.instances[selected];
      if (instance) publishDraft({ ...edited, [instance.obj_id]: url });
    };
    image.src = url;
  }, [edited, frame.instances, history, publishDraft, selected]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;

      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        restore(historyIndex + (event.shiftKey ? 1 : -1));
        return;
      }

      switch (event.key.toLowerCase()) {
        case "m":
          setTool("move");
          return;
        case "b":
          setTool("brush");
          return;
        case "e":
          setTool("eraser");
          return;
        case "[":
          setBrushSize((current) => Math.max(2, current - 4));
          return;
        case "]":
          setBrushSize((current) => Math.min(256, current + 4));
          return;
      }

      if (event.code === "Space") {
        event.preventDefault();
        setSpaceHeld(true);
      }
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.code === "Space") setSpaceHeld(false);
    };
    const onBlur = () => setSpaceHeld(false);
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onBlur);
    };
  }, [historyIndex, restore]);

  const updateCursor = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const bounds = canvas.getBoundingClientRect();
    setCursor({
      x: event.clientX - bounds.left,
      y: event.clientY - bounds.top,
      size: (brushSize / canvas.width) * bounds.width,
    });
  };

  const pointerDown = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d");
    if (!canvas || !context) return;
    event.preventDefault();
    canvas.setPointerCapture(event.pointerId);
    const bounds = canvas.getBoundingClientRect();

    if (tool === "move" || spaceHeld) {
      let lastX = event.clientX;
      let lastY = event.clientY;
      const move = (moveEvent: PointerEvent) => {
        const state = useReview.getState();
        const dx = (moveEvent.clientX - lastX) / bounds.width / state.zoom;
        const dy = (moveEvent.clientY - lastY) / bounds.height / state.zoom;
        lastX = moveEvent.clientX;
        lastY = moveEvent.clientY;
        state.nudgePan(-dx, -dy);
      };
      const up = () => {
        canvas.removeEventListener("pointermove", move);
        canvas.removeEventListener("pointerup", up);
        canvas.removeEventListener("pointercancel", up);
      };
      canvas.addEventListener("pointermove", move);
      canvas.addEventListener("pointerup", up);
      canvas.addEventListener("pointercancel", up);
      return;
    }

    const point = (clientX: number, clientY: number) => ({
      x: ((clientX - bounds.left) / bounds.width) * canvas.width,
      y: ((clientY - bounds.top) / bounds.height) * canvas.height,
    });
    const stamp = (x: number, y: number) => {
      context.save();
      context.globalCompositeOperation = tool === "eraser" ? "destination-out" : "source-over";
      context.fillStyle = "white";
      context.beginPath();
      if (brushShape === "circle") {
        context.arc(x, y, brushSize / 2, 0, Math.PI * 2);
      } else {
        context.rect(x - brushSize / 2, y - brushSize / 2, brushSize, brushSize);
      }
      context.fill();
      context.restore();
    };
    let last = point(event.clientX, event.clientY);
    stamp(last.x, last.y);
    const move = (moveEvent: PointerEvent) => {
      const next = point(moveEvent.clientX, moveEvent.clientY);
      const distance = Math.hypot(next.x - last.x, next.y - last.y);
      const steps = Math.max(1, Math.ceil(distance / Math.max(brushSize / 4, 1)));
      for (let step = 1; step <= steps; step += 1) {
        const ratio = step / steps;
        stamp(last.x + (next.x - last.x) * ratio, last.y + (next.y - last.y) * ratio);
      }
      last = next;
    };
    const up = () => {
      canvas.removeEventListener("pointermove", move);
      canvas.removeEventListener("pointerup", up);
      canvas.removeEventListener("pointercancel", up);
      snapshot();
    };
    canvas.addEventListener("pointermove", move);
    canvas.addEventListener("pointerup", up);
    canvas.addEventListener("pointercancel", up);
  };

  const markWithoutObject = () => {
    const canvas = canvasRef.current;
    if (canvas) canvas.getContext("2d")?.clearRect(0, 0, canvas.width, canvas.height);
    setHistory([]);
    setHistoryIndex(-1);
    publishDraft({}, true);
  };

  return (
    <>
      <canvas
        ref={canvasRef}
        aria-label="Máscara de segmentação editável"
        onPointerDown={pointerDown}
        onPointerMove={updateCursor}
        onPointerEnter={updateCursor}
        onPointerLeave={() => setCursor(null)}
        className={cx(
          "absolute inset-0 h-full w-full touch-none",
          tool === "move" || spaceHeld ? "cursor-grab" : "cursor-none",
        )}
        style={{ opacity: maskVisible ? opacity : 0 }}
      />
      {cursor && tool !== "move" && !spaceHeld && (
        <span
          aria-hidden="true"
          className="pointer-events-none absolute z-10 border border-white bg-white/10 shadow-[0_0_0_1px_rgba(0,0,0,.8)]"
          style={{
            left: cursor.x - cursor.size / 2,
            top: cursor.y - cursor.size / 2,
            width: cursor.size,
            height: cursor.size,
            borderRadius: brushShape === "circle" ? "9999px" : "2px",
          }}
        />
      )}

      {toolControlsTarget && createPortal(
        <div className="flex w-full flex-col gap-1 p-2 text-xs text-zinc-300">
        <Button
          variant={tool === "move" ? "primary" : "ghost"}
          className="justify-between px-2"
          onClick={() => setTool("move")}
        >
          Mover <Kbd>M</Kbd>
        </Button>
        <Button
          variant={tool === "brush" ? "primary" : "ghost"}
          className="justify-between px-2"
          onClick={() => setTool("brush")}
        >
          Pincel <Kbd>B</Kbd>
        </Button>
        <Button
          variant={tool === "eraser" ? "primary" : "ghost"}
          className="justify-between px-2"
          onClick={() => setTool("eraser")}
        >
          Borracha <Kbd>E</Kbd>
        </Button>
        </div>,
        toolControlsTarget,
      )}

      {adjustmentControlsTarget && createPortal(
        <div className="flex w-full flex-wrap items-center gap-3 text-xs text-zinc-300">
          <div className="flex items-center gap-2 border-r border-zinc-700 pr-3">
        <select
          aria-label="Instância da máscara"
          value={selected}
          onChange={(event) => setSelected(Number(event.target.value))}
          className="rounded border border-zinc-700 bg-zinc-900 px-1.5 py-1"
        >
          {frame.instances.map((instance, index) => (
            <option key={instance.obj_id} value={index}>
              #{instance.obj_id} {instance.label}
            </option>
          ))}
        </select>
        <Button variant="ghost" onClick={() => setMaskVisible((current) => !current)}>
          {maskVisible ? "Ocultar máscara" : "Mostrar máscara"}
        </Button>
          </div>
        <label className="flex items-center gap-1.5">
          Formato
          <select
            aria-label="Formato do pincel"
            value={brushShape}
            onChange={(event) => setBrushShape(event.target.value as "circle" | "square")}
            className="rounded border border-zinc-700 bg-zinc-900 px-1.5 py-1"
          >
            <option value="circle">Circular</option>
            <option value="square">Quadrado</option>
          </select>
        </label>
        <label className="flex items-center gap-1.5">
          Tamanho {brushSize}px
          <input
            aria-label="Tamanho do pincel"
            type="range"
            min={2}
            max={256}
            value={brushSize}
            onChange={(event) => setBrushSize(Number(event.target.value))}
          />
          <Kbd>[</Kbd>
          <Kbd>]</Kbd>
        </label>
        <label className="flex items-center gap-1.5">
          Opacidade {Math.round(opacity * 100)}%
          <input
            aria-label="Opacidade da máscara"
            type="range"
            min={0.05}
            max={0.8}
            step={0.05}
            value={opacity}
            onChange={(event) => setOpacity(Number(event.target.value))}
          />
        </label>
        <Button variant="ghost" disabled={historyIndex <= 0} onClick={() => restore(historyIndex - 1)}>
          Desfazer
        </Button>
        <Button
          variant="ghost"
          disabled={historyIndex >= history.length - 1}
          onClick={() => restore(historyIndex + 1)}
        >
          Refazer
        </Button>
        <Button variant="ghost" onClick={() => useReview.getState().resetView()}>
          Ajustar à tela <Kbd>0</Kbd>
        </Button>
        <Button variant={empty ? "primary" : "ghost"} onClick={markWithoutObject}>
          {empty ? "Sem objeto ✓" : "Sem objeto"}
        </Button>
        {draft && <span className="text-amber-400">alteração pendente</span>}
        </div>,
        adjustmentControlsTarget,
      )}
    </>
  );
}
