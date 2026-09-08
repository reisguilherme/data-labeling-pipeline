import type { PipelineStage } from "../api/types";
import type { AppRoute, OperationsStage } from "../lib/routes";
import { navigate } from "../lib/routes";
import { cx } from "../lib/format";

const ITEMS: { stage: OperationsStage; label: string; icon: string }[] = [
  { stage: "overview", label: "Visão geral", icon: "⌂" },
  { stage: "triage", label: "Triagem", icon: "▣" },
  { stage: "sam3", label: "SAM3", icon: "✦" },
  { stage: "review", label: "Revisão", icon: "✓" },
  { stage: "completed", label: "Concluídos", icon: "●" },
];

export function PipelineSidebar({
  objectId,
  route,
  collapsed = false,
  counts,
}: {
  objectId: string;
  route: AppRoute;
  collapsed?: boolean;
  counts?: Partial<Record<PipelineStage, number>>;
}) {
  const active = route.page === "operations" ? route.stage : route.page;
  return (
    <aside
      aria-label="Áreas da aplicação"
      className={cx(
        "flex shrink-0 flex-col border-r border-zinc-800/90 bg-zinc-950/80 py-3 transition-[width]",
        collapsed ? "w-14" : "w-56",
      )}
    >
      <nav className="space-y-1 px-2">
        {ITEMS.map((item) => (
          <button
            key={item.stage}
            type="button"
            title={collapsed ? item.label : undefined}
            aria-current={active === item.stage ? "page" : undefined}
            onClick={() => navigate({ page: "operations", objectId, stage: item.stage })}
            className={cx(
              "flex h-12 w-full items-center rounded-lg px-3 text-sm transition-colors",
              active === item.stage
                ? "bg-emerald-950/55 text-emerald-300"
                : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100",
              collapsed && "justify-center px-0",
            )}
          >
            <span aria-hidden="true" className="w-5 shrink-0 text-center text-xs">
              {item.icon}
            </span>
            {!collapsed && <span className="ml-2 truncate">{item.label}</span>}
            {!collapsed && item.stage !== "overview" && (
              <span className="tnum ml-auto text-xs text-zinc-400">
                {counts?.[item.stage as PipelineStage] ?? 0}
              </span>
            )}
          </button>
        ))}
      </nav>

      <div className="my-3 border-t border-zinc-800/80" />
      <nav className="space-y-1 px-2">
        {[
          { id: "export", label: "Exportar datasets", icon: "⇩" },
          { id: "objects", label: "Objetos e classes", icon: "◇" },
        ].map((item) => (
          <button
            key={item.id}
            type="button"
            title={collapsed ? item.label : undefined}
            onClick={() => navigate(item.id === "export" ? "/export" : "/objects")}
            className={cx(
              "flex h-12 w-full items-center rounded-lg px-3 text-sm text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100",
              collapsed && "justify-center px-0",
            )}
          >
            <span aria-hidden="true" className="w-5 text-center text-xs">{item.icon}</span>
            {!collapsed && <span className="ml-2 truncate">{item.label}</span>}
          </button>
        ))}
      </nav>
    </aside>
  );
}
