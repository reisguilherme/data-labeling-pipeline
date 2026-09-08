import { useMemo, useState } from "react";
import type { SelectedVideo, VideoListItem } from "../api/types";

export function VideoMultiSelector({
  videos,
  selected,
  onChange,
}: {
  videos: { object_id: string; object_name: string; video: VideoListItem }[];
  selected: SelectedVideo[];
  onChange: (items: SelectedVideo[]) => void;
}) {
  const [search, setSearch] = useState("");
  const visible = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    return videos.filter((item) => !needle || item.video.relpath.toLocaleLowerCase().includes(needle));
  }, [videos, search]);
  const has = (objectId: string, videoId: string) => selected.some((item) => item.object_id === objectId && item.video_id === videoId);
  return (
    <div>
      <div className="flex items-center gap-2">
        <input name="dataset_video_search" aria-label="Buscar vídeo concluído" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Buscar vídeo concluído…" className="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-xs text-zinc-200" />
        {selected.length > 0 && <button type="button" onClick={() => onChange([])} className="shrink-0 text-xs text-zinc-400 hover:text-zinc-200">usar todos</button>}
      </div>
      <p className="mt-2 text-[11px] text-zinc-400">Nenhuma seleção = todos os concluídos dos objetos escolhidos.</p>
      {visible.length > 0 && (
        <div className="mt-3 max-h-48 space-y-1 overflow-y-auto rounded-lg border border-zinc-800 p-2">
          {visible.map(({ object_id, object_name, video }) => {
            const checked = has(object_id, video.video_id);
            return <label key={`${object_id}:${video.video_id}`} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 hover:bg-zinc-900"><input name={`dataset_video_${object_id}_${video.video_id}`} type="checkbox" checked={checked} onChange={() => onChange(checked ? selected.filter((item) => item.object_id !== object_id || item.video_id !== video.video_id) : [...selected, { object_id, video_id: video.video_id }])} className="accent-emerald-500" /><span className="min-w-0 flex-1 truncate text-xs text-zinc-300">{video.name}</span><span className="text-[10px] text-zinc-400">{object_name}</span></label>;
          })}
        </div>
      )}
    </div>
  );
}
