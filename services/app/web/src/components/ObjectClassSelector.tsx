import type { ObjectInfo } from "../api/types";

export function ObjectClassSelector({
  objects,
  selected,
  onChange,
}: {
  objects: ObjectInfo[];
  selected: string[];
  onChange: (ids: string[]) => void;
}) {
  const ordered = [...objects].filter((item) => !item.archived).sort((a, b) => a.object_id.localeCompare(b.object_id));
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {ordered.map((object, index) => {
        const checked = selected.includes(object.object_id);
        return (
          <label key={object.object_id} className={`flex cursor-pointer items-center gap-3 rounded-lg border p-3 ${checked ? "border-emerald-800 bg-emerald-950/25" : "border-zinc-800 bg-zinc-950/40"}`}>
            <input
              name={`dataset_object_${object.object_id}`}
              type="checkbox"
              checked={checked}
              onChange={() => onChange(checked ? selected.filter((id) => id !== object.object_id) : [...selected, object.object_id])}
              className="accent-emerald-500"
            />
            <span className="min-w-0 flex-1"><span className="block truncate text-sm text-zinc-200">{object.display_name}</span><span className="block truncate text-[11px] text-zinc-400">{object.object_id}</span></span>
            <span className="tnum rounded bg-zinc-800 px-2 py-1 text-[11px] text-zinc-300">{index} · {object.label}</span>
          </label>
        );
      })}
    </div>
  );
}
