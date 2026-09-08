import { useEffect, useRef } from "react";
import { api } from "../api/client";
import { useAnnotator } from "../store/annotator";

/** `requestVideoFrameCallback` só existe em Chromium/WebKit. */
interface VideoFrameMeta {
  mediaTime: number;
  presentedFrames: number;
}
type RVFC = HTMLVideoElement & {
  requestVideoFrameCallback?: (cb: (now: number, meta: VideoFrameMeta) => void) => number;
  cancelVideoFrameCallback?: (handle: number) => void;
};

/**
 * Player de varredura grossa.
 *
 * O frame derivado daqui é SEMPRE uma estimativa — browsers arredondam
 * `currentTime` pelo relógio do compositor, o seek é ancorado em keyframe, há
 * reordenação de B-frames, edit lists e `start_time` != 0. Em VFR a multiplicação
 * por fps nem é válida. Por isso todo frame que sai daqui entra na store marcado
 * como provisório, e o filmstrip é quem confirma.
 */
export function VideoPlayer({ videoId }: { videoId: string }) {
  const ref = useRef<HTMLVideoElement>(null);
  const meta = useAnnotator((s) => s.meta);
  const playing = useAnnotator((s) => s.playing);
  const rate = useAnnotator((s) => s.playbackRate);
  const currentFrame = useAnnotator((s) => s.currentFrame);

  const fps = meta?.media.fps ?? 30;
  const offset = meta?.media.start_time_sec ?? 0;

  // Enquanto toca, o frame vem do próprio vídeo. Fora disso, a store manda.
  const drivenByVideo = useRef(false);

  useEffect(() => {
    const video = ref.current as RVFC | null;
    if (!video) return;

    let handle = 0;
    let cancelled = false;

    const report = (mediaTime: number) => {
      const frame = Math.round((mediaTime - offset) * fps);
      useAnnotator.getState().setFrame(frame, true);
    };

    if (video.requestVideoFrameCallback) {
      const tick = (_now: number, frameMeta: VideoFrameMeta) => {
        if (cancelled) return;
        if (drivenByVideo.current) report(frameMeta.mediaTime);
        handle = video.requestVideoFrameCallback!(tick);
      };
      handle = video.requestVideoFrameCallback(tick);
      return () => {
        cancelled = true;
        video.cancelVideoFrameCallback?.(handle);
      };
    }

    const onTimeUpdate = () => {
      if (drivenByVideo.current) report(video.currentTime);
    };
    video.addEventListener("timeupdate", onTimeUpdate);
    return () => video.removeEventListener("timeupdate", onTimeUpdate);
  }, [fps, offset]);

  useEffect(() => {
    const video = ref.current;
    if (!video) return;
    video.playbackRate = Math.abs(rate) || 1;
    if (playing) {
      drivenByVideo.current = true;
      void video.play().catch(() => useAnnotator.getState().setPlaying(false));
    } else {
      video.pause();
      drivenByVideo.current = false;
    }
  }, [playing, rate]);

  // Seek quando a store mudou o frame por outro caminho (setas, timeline).
  useEffect(() => {
    const video = ref.current;
    if (!video || drivenByVideo.current) return;
    const target = offset + currentFrame / fps;
    if (Math.abs(video.currentTime - target) > 0.5 / fps) {
      video.currentTime = target;
    }
  }, [currentFrame, fps, offset]);

  return (
    <video
      ref={ref}
      src={api.streamUrl(videoId)}
      className="h-full w-full bg-black object-contain"
      preload="auto"
      playsInline
      loop
      onClick={() => useAnnotator.getState().setPlaying(!playing)}
      onSeeked={(event) => {
        if (drivenByVideo.current) return;
        const video = event.currentTarget;
        useAnnotator.getState().setFrame(Math.round((video.currentTime - offset) * fps), true);
      }}
    />
  );
}
