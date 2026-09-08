/**
 * Atalhos como DADOS.
 *
 * A triagem é repetida centenas de vezes, então o teclado é a interface
 * principal. Esta lista alimenta tanto o handler quanto o overlay de ajuda —
 * é impossível a documentação divergir do comportamento.
 */

export type Action =
  | "playPause"
  | "stepBack1"
  | "stepFwd1"
  | "stepBack10"
  | "stepFwd10"
  | "stepBack100"
  | "stepFwd100"
  | "shuttleBack"
  | "shuttleStop"
  | "shuttleFwd"
  | "goStart"
  | "goEnd"
  | "setIn"
  | "setOut"
  | "newInterval"
  | "deleteInterval"
  | "gotoIntervalStart"
  | "gotoIntervalEnd"
  | "prevInterval"
  | "nextInterval"
  | "bboxMode"
  | "escape"
  | "cycleBbox"
  | "deleteBbox"
  | "zoomIn"
  | "zoomOut"
  | "zoomReset"
  | "toggleBoost"
  | "difficulty1"
  | "difficulty2"
  | "difficulty3"
  | "focusFlags"
  | "noBoom"
  | "save"
  | "saveExportNext"
  | "prevVideo"
  | "nextVideo"
  | "help";

export interface Shortcut {
  action: Action;
  keys: string;
  label: string;
  group: string;
}

export const SHORTCUTS: Shortcut[] = [
  { action: "playPause", keys: "Space", label: "Play / pause", group: "Navegação" },
  { action: "stepBack1", keys: "←", label: "1 frame atrás", group: "Navegação" },
  { action: "stepFwd1", keys: "→", label: "1 frame à frente", group: "Navegação" },
  { action: "stepBack10", keys: "Shift+←", label: "10 frames atrás", group: "Navegação" },
  { action: "stepFwd10", keys: "Shift+→", label: "10 frames à frente", group: "Navegação" },
  { action: "stepBack100", keys: "Ctrl+←", label: "100 frames atrás", group: "Navegação" },
  { action: "stepFwd100", keys: "Ctrl+→", label: "100 frames à frente", group: "Navegação" },
  { action: "shuttleBack", keys: "J", label: "Rebobinar (repetir acelera)", group: "Navegação" },
  { action: "shuttleStop", keys: "K", label: "Parar", group: "Navegação" },
  { action: "shuttleFwd", keys: "L", label: "Avançar (repetir acelera)", group: "Navegação" },
  { action: "goStart", keys: "Home", label: "Primeiro frame", group: "Navegação" },
  { action: "goEnd", keys: "End", label: "Último frame", group: "Navegação" },

  { action: "setIn", keys: "I", label: "Novo intervalo começando aqui", group: "Intervalos" },
  { action: "setOut", keys: "O", label: "Fechar o intervalo aqui", group: "Intervalos" },
  { action: "newInterval", keys: "N", label: "Novo intervalo (vazio)", group: "Intervalos" },
  { action: "deleteInterval", keys: "Delete", label: "Apagar intervalo selecionado", group: "Intervalos" },
  { action: "gotoIntervalStart", keys: "[", label: "Ir ao início do intervalo", group: "Intervalos" },
  { action: "gotoIntervalEnd", keys: "]", label: "Ir ao fim do intervalo", group: "Intervalos" },
  { action: "prevInterval", keys: "Alt+←", label: "Intervalo anterior", group: "Intervalos" },
  { action: "nextInterval", keys: "Alt+→", label: "Próximo intervalo", group: "Intervalos" },

  { action: "bboxMode", keys: "B", label: "Modo bbox no frame inicial", group: "Bbox" },
  { action: "cycleBbox", keys: "Tab", label: "Ciclar entre bboxes", group: "Bbox" },
  { action: "deleteBbox", keys: "Delete", label: "Remover bbox selecionado", group: "Bbox" },
  { action: "escape", keys: "Esc", label: "Cancelar / sair do modo", group: "Bbox" },

  { action: "zoomIn", keys: "+", label: "Ampliar (ou scroll no frame)", group: "Imagem" },
  { action: "zoomOut", keys: "-", label: "Reduzir", group: "Imagem" },
  { action: "zoomReset", keys: "0", label: "Resetar zoom", group: "Imagem" },
  { action: "toggleBoost", keys: "G", label: "Realçar contraste (material log)", group: "Imagem" },

  { action: "difficulty1", keys: "1", label: "Dificuldade: fácil", group: "Flags" },
  { action: "difficulty2", keys: "2", label: "Dificuldade: médio", group: "Flags" },
  { action: "difficulty3", keys: "3", label: "Dificuldade: difícil", group: "Flags" },
  { action: "focusFlags", keys: "F", label: "Focar painel de flags", group: "Flags" },

  { action: "noBoom", keys: "X", label: 'Marcar "sem objeto"', group: "Salvar" },
  { action: "save", keys: "Ctrl+S", label: "Salvar", group: "Salvar" },
  { action: "saveExportNext", keys: "Ctrl+Enter", label: "Salvar, enviar ao SAM3 e próximo", group: "Salvar" },
  { action: "prevVideo", keys: "PageUp", label: "Vídeo anterior", group: "Salvar" },
  { action: "nextVideo", keys: "PageDown", label: "Próximo vídeo", group: "Salvar" },
  { action: "help", keys: "?", label: "Esta ajuda", group: "Salvar" },
];

/** Traduz um KeyboardEvent na Action correspondente, ou null. */
export function resolveAction(event: KeyboardEvent): Action | null {
  const { key, shiftKey, altKey } = event;
  const mod = event.ctrlKey || event.metaKey;

  switch (key) {
    case " ":
      return "playPause";
    case "ArrowLeft":
      if (altKey) return "prevInterval";
      if (mod) return "stepBack100";
      return shiftKey ? "stepBack10" : "stepBack1";
    case "ArrowRight":
      if (altKey) return "nextInterval";
      if (mod) return "stepFwd100";
      return shiftKey ? "stepFwd10" : "stepFwd1";
    case ",":
      return "stepBack1";
    case ".":
      return "stepFwd1";
    case "Home":
      return "goStart";
    case "End":
      return "goEnd";
    case "[":
      return "gotoIntervalStart";
    case "]":
      return "gotoIntervalEnd";
    case "Tab":
      return "cycleBbox";
    case "Escape":
      return "escape";
    case "Delete":
    case "Backspace":
      return "deleteBbox";
    case "PageUp":
      return "prevVideo";
    case "PageDown":
      return "nextVideo";
    case "Enter":
      return mod ? "saveExportNext" : null;
    case "?":
      return "help";
    default:
      break;
  }

  const lower = key.toLowerCase();
  if (mod && lower === "s") return "save";
  if (mod) return null;

  // Zoom: aceita as duas fileiras de teclado (numérica e principal).
  if (key === "+" || key === "=") return "zoomIn";
  if (key === "-" || key === "_") return "zoomOut";
  if (key === "0") return "zoomReset";

  switch (lower) {
    case "i":
      return "setIn";
    case "o":
      return "setOut";
    case "n":
      return "newInterval";
    case "b":
      return "bboxMode";
    case "j":
      return "shuttleBack";
    case "k":
      return "shuttleStop";
    case "l":
      return "shuttleFwd";
    case "f":
      return "focusFlags";
    case "g":
      return "toggleBoost";
    case "x":
      return "noBoom";
    case "1":
      return "difficulty1";
    case "2":
      return "difficulty2";
    case "3":
      return "difficulty3";
    default:
      return null;
  }
}

/** Ações que continuam valendo mesmo com o foco num campo de texto. */
export const ALLOWED_IN_INPUT = new Set<Action>(["escape", "save", "saveExportNext"]);
