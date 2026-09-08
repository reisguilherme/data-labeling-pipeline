import type { VideoListItem } from "../api/types";
import { cx } from "../lib/format";

export type Stage = "library" | "sam3" | "review" | "dataset";

/**
 * Navegação entre as etapas do pipeline.
 *
 * Cada etapa é uma tela dedicada, e a ordem na barra é a ordem real do
 * trabalho: triar → conferir a caixa inicial e propagar → corrigir as bboxes →
 * exportar. Antes disso, o controle do SAM3 não existia e a revisão só aparecia
 * num hover do card — duas etapas inteiras escondidas atrás de um gesto.
 *
 * As etapas que agem sobre UM vídeo ficam desabilitadas sem vídeo selecionado,
 * em vez de sumirem: assim dá para ver que existem e o que falta para chegar
 * nelas.
 */
export function StageNav({
  active,
  video,
  onGo,
}: {
  active: Stage;
  video: VideoListItem | null;
  onGo: (stage: Stage) => void;
}) {
  const exportado = video?.status === "done";
  const propagado = video?.sam3?.state === "done";

  const stages: {
    id: Stage;
    label: string;
    hint?: string;
    disabled?: boolean;
  }[] = [
    { id: "library", label: "Triagem" },
    {
      id: "sam3",
      label: "SAM3",
      disabled: !video || !exportado,
      hint: !video
        ? "escolha um vídeo"
        : !exportado
          ? "exporte o vídeo na triagem primeiro"
          : "ajustar a caixa inicial e propagar",
    },
    {
      id: "review",
      label: "Revisão",
      disabled: !video || !propagado,
      hint: !video
        ? "escolha um vídeo"
        : !propagado
          ? "propague com o SAM3 primeiro"
          : "corrigir as bboxes frame a frame",
    },
    { id: "dataset", label: "Dataset", hint: "exportar YOLO ou COCO" },
  ];

  return (
    <nav className="flex items-center gap-1">
      {stages.map((stage, index) => (
        <div key={stage.id} className="flex items-center">
          {index > 0 && <span className="px-1 text-zinc-700">›</span>}
          <button
            onClick={() => !stage.disabled && onGo(stage.id)}
            disabled={stage.disabled}
            title={stage.hint}
            className={cx(
              "rounded-md px-2.5 py-1 text-xs transition-colors",
              active === stage.id
                ? "bg-zinc-800 font-medium text-zinc-100"
                : stage.disabled
                  ? "cursor-not-allowed text-zinc-700"
                  : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200",
            )}
          >
            {stage.label}
          </button>
        </div>
      ))}
    </nav>
  );
}
