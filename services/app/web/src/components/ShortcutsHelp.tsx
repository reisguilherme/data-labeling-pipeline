import { SHORTCUTS } from "../lib/keys";
import { useAnnotator } from "../store/annotator";
import { Kbd } from "./ui";

export function ShortcutsHelp() {
  const show = useAnnotator((s) => s.showHelp);
  const toggle = useAnnotator((s) => s.toggleHelp);
  if (!show) return null;

  // Renderizado a partir da MESMA lista que o handler consome: a ajuda não pode
  // divergir do comportamento real.
  const groups = [...new Set(SHORTCUTS.map((shortcut) => shortcut.group))];

  return (
    <div
      onClick={() => toggle(false)}
      className="fixed inset-0 z-50 grid place-items-center bg-zinc-950/80 p-8"
    >
      <div
        onClick={(event) => event.stopPropagation()}
        className="max-h-full w-full max-w-3xl overflow-y-auto rounded-md border border-zinc-800 bg-zinc-900 p-5"
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-sm font-medium text-zinc-100">Atalhos de teclado</h2>
          <button onClick={() => toggle(false)} className="text-zinc-500 hover:text-zinc-200">
            ×
          </button>
        </div>

        <div className="grid gap-5 sm:grid-cols-2">
          {groups.map((group) => (
            <section key={group}>
              <h3 className="mb-1.5 text-[10px] tracking-wide text-zinc-500 uppercase">
                {group}
              </h3>
              <ul className="space-y-1">
                {SHORTCUTS.filter((shortcut) => shortcut.group === group).map((shortcut) => (
                  <li
                    key={`${shortcut.action}-${shortcut.keys}`}
                    className="flex items-center justify-between gap-3 text-xs"
                  >
                    <span className="text-zinc-400">{shortcut.label}</span>
                    <Kbd>{shortcut.keys}</Kbd>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}
