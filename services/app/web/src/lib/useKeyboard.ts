import { useEffect, useRef } from "react";
import { useAnnotator } from "../store/annotator";
import { ALLOWED_IN_INPUT, resolveAction, type Action } from "./keys";

const SHUTTLE_RATES = [1, 2, 4, 8];

export interface KeyboardHandlers {
  onSave: () => void;
  onSaveExportNext: () => void;
  onNoBoom: () => void;
  onPrevVideo: () => void;
  onNextVideo: () => void;
}

/**
 * Único listener de teclado do annotator.
 *
 * O auto-repeat é estrangulado a um passo por animation frame: segurar `→` deve
 * scrubbar suave, não enfileirar 300 carregamentos de imagem.
 */
export function useKeyboard(handlers: KeyboardHandlers) {
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    let pendingStep = 0;
    let rafHandle = 0;

    const flushStep = () => {
      rafHandle = 0;
      if (pendingStep === 0) return;
      useAnnotator.getState().stepFrame(pendingStep);
      pendingStep = 0;
    };

    const step = (delta: number) => {
      const state = useAnnotator.getState();
      if (state.playing) state.setPlaying(false);
      pendingStep += delta;
      if (!rafHandle) rafHandle = requestAnimationFrame(flushStep);
    };

    const shuttle = (direction: 1 | -1) => {
      const state = useAnnotator.getState();
      if (!state.playing) {
        state.setPlaybackRate(SHUTTLE_RATES[0] * direction);
        state.setPlaying(true);
        return;
      }
      const current = Math.abs(state.playbackRate);
      const at = SHUTTLE_RATES.indexOf(current);
      const next = SHUTTLE_RATES[Math.min(at + 1, SHUTTLE_RATES.length - 1)];
      state.setPlaybackRate(next * direction);
    };

    const run = (action: Action, event: KeyboardEvent) => {
      const state = useAnnotator.getState();
      const interval = state.selected >= 0 ? state.intervals[state.selected] : null;

      switch (action) {
        case "playPause":
          state.setPlaying(!state.playing);
          break;

        case "stepBack1": step(-1); break;
        case "stepFwd1": step(1); break;
        case "stepBack10": step(-10); break;
        case "stepFwd10": step(10); break;
        case "stepBack100": step(-100); break;
        case "stepFwd100": step(100); break;

        case "shuttleBack": shuttle(-1); break;
        case "shuttleFwd": shuttle(1); break;
        case "shuttleStop":
          state.setPlaying(false);
          state.setPlaybackRate(1);
          break;

        case "goStart": state.setFrame(0, false); break;
        case "goEnd": state.setFrame(state.frameCount() - 1, false); break;

        case "setIn":
          state.setPlaying(false);
          state.setIn(state.currentFrame, state.frameProvisional);
          // Marcar no player só ancora: leva direto ao filmstrip para confirmar.
          if (state.phase === "scan") {
            state.setPhase("refine");
            void state.ensureFrameAvailable(state.currentFrame).catch(() => undefined);
          }
          break;

        case "setOut":
          state.setPlaying(false);
          state.setOut(state.currentFrame, state.frameProvisional);
          if (state.phase === "scan") {
            state.setPhase("refine");
            void state.ensureFrameAvailable(state.currentFrame).catch(() => undefined);
          }
          break;

        case "newInterval":
          state.addInterval(state.currentFrame);
          break;

        case "deleteInterval":
          if (state.selected >= 0) state.removeInterval(state.selected);
          break;

        case "gotoIntervalStart":
          if (interval?.start != null) state.setFrame(interval.start, interval.startProvisional);
          break;

        case "gotoIntervalEnd":
          if (interval?.end != null) state.setFrame(interval.end, interval.endProvisional);
          break;

        case "prevInterval":
          if (state.intervals.length)
            state.selectInterval(Math.max(state.selected - 1, 0));
          break;

        case "nextInterval":
          if (state.intervals.length)
            state.selectInterval(Math.min(state.selected + 1, state.intervals.length - 1));
          break;

        case "bboxMode":
          if (interval?.start != null) {
            state.setPlaying(false);
            state.setFrame(interval.start, false);
            state.setPhase("bbox");
            void state.ensureFrameAvailable(interval.start).catch(() => undefined);
          }
          break;

        case "escape":
          if (state.showHelp) state.toggleHelp(false);
          else if (state.phase === "bbox") state.setPhase("refine");
          (document.activeElement as HTMLElement | null)?.blur();
          break;

        case "cycleBbox":
          if (interval && interval.bboxes.length) {
            event.preventDefault();
            const next = event.shiftKey ? state.selectedBbox - 1 : state.selectedBbox + 1;
            const count = interval.bboxes.length;
            state.selectBbox(((next % count) + count) % count);
          }
          break;

        case "deleteBbox":
          if (state.phase === "bbox" && interval && state.selectedBbox >= 0) {
            state.setBboxes(interval.bboxes.filter((_, at) => at !== state.selectedBbox));
            state.selectBbox(-1);
          } else if (state.selected >= 0) {
            state.removeInterval(state.selected);
          }
          break;

        case "zoomIn": state.setZoom(state.zoom * 1.5); break;
        case "zoomOut": state.setZoom(state.zoom / 1.5); break;
        case "zoomReset": state.resetView(); break;
        case "toggleBoost": state.toggleBoost(); break;

        case "difficulty1": state.setFlag("dificuldade", "facil"); break;
        case "difficulty2": state.setFlag("dificuldade", "medio"); break;
        case "difficulty3": state.setFlag("dificuldade", "dificil"); break;

        case "focusFlags":
          document
            .querySelector<HTMLButtonElement>("#flags-panel button")
            ?.focus();
          break;

        case "noBoom": handlersRef.current.onNoBoom(); break;
        case "save": handlersRef.current.onSave(); break;
        case "saveExportNext": handlersRef.current.onSaveExportNext(); break;
        case "prevVideo": handlersRef.current.onPrevVideo(); break;
        case "nextVideo": handlersRef.current.onNextVideo(); break;
        case "help": state.toggleHelp(); break;
      }
    };

    const onKeyDown = (event: KeyboardEvent) => {
      const action = resolveAction(event);
      if (!action) return;

      const target = event.target as HTMLElement | null;
      const typing =
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target?.isContentEditable;
      if (typing && !ALLOWED_IN_INPUT.has(action)) return;

      // Space e setas rolariam a página; Ctrl+S abriria o salvar do browser.
      event.preventDefault();
      run(action, event);
    };

    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      if (rafHandle) cancelAnimationFrame(rafHandle);
    };
  }, []);
}
