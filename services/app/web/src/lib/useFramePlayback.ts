import { useEffect } from "react";
import { useAnnotator } from "../store/annotator";

/**
 * Reprodução da sequência de frames extraídos.
 *
 * Existe porque o material é HEVC, que nenhum navegador decodifica. As duas
 * alternativas eram transcodificar um preview H.264 ou tocar os JPEGs que já
 * extraímos de qualquer jeito. Medido num 4K HEVC de 87 s: o transcode custa
 * ~35 s contra ~13 s da extração do proxy — e seria um SEGUNDO passe sobre o
 * arquivo.
 *
 * Além de mais barato, isto é melhor: a posição de reprodução É um índice de
 * frame, então não existe a estimativa `currentTime × fps`. Nada aqui fica
 * provisório — cada frame exibido é o mesmo que o export vai gerar.
 */
export function useFramePlayback(active: boolean) {
  const playing = useAnnotator((s) => s.playing);
  const rate = useAnnotator((s) => s.playbackRate);
  const fps = useAnnotator((s) => s.meta?.media.fps ?? 24);

  useEffect(() => {
    if (!active || !playing) return;

    let handle = 0;
    let previous = performance.now();
    // Acumulador fracionário: a 24 fps um frame dura 41,7 ms, então arredondar a
    // cada tick de rAF (16,7 ms) faria a reprodução derrapar.
    let carry = 0;

    const tick = (now: number) => {
      const elapsed = (now - previous) / 1000;
      previous = now;

      carry += elapsed * fps * rate;
      const step = Math.trunc(carry);

      if (step !== 0) {
        carry -= step;
        const state = useAnnotator.getState();
        const last = state.frameCount() - 1;
        let target = state.currentFrame + step;

        // Em loop: varrer um clipe de 14 s procurando o boom exige rever o
        // mesmo trecho várias vezes, e parar no fim obrigaria a rebobinar
        // manualmente a cada passada.
        if (last > 0) {
          if (target > last) target = target % (last + 1);
          else if (target < 0) target = last + ((target + 1) % (last + 1));
        } else {
          target = 0;
        }

        state.setFrame(target, false);
      }

      handle = requestAnimationFrame(tick);
    };

    handle = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(handle);
  }, [active, playing, rate, fps]);
}
