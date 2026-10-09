/** Minimal time-based tween manager; no dependencies. */

export type Easing = (t: number) => number;

export const easeInOutCubic: Easing = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
export const easeOutCubic: Easing = (t) => 1 - Math.pow(1 - t, 3);
export const easeOutQuint: Easing = (t) => 1 - Math.pow(1 - t, 5);
export const easeOutBack: Easing = (t) => {
  const c1 = 1.70158;
  const c3 = c1 + 1;
  return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
};
export const linear: Easing = (t) => t;

export interface Tween {
  /** Advance the tween. Returns false once finished. */
  update(now: number): boolean;
  cancel(): void;
}

interface TweenOptions {
  duration: number;
  delay?: number;
  easing?: Easing;
  onUpdate: (t: number) => void;
  onComplete?: () => void;
}

export class TweenManager {
  private tweens = new Set<Tween>();

  add(opts: TweenOptions, now = performance.now()): Tween {
    const start = now + (opts.delay ?? 0);
    const easing = opts.easing ?? easeInOutCubic;
    let cancelled = false;
    let done = false;
    const tween: Tween = {
      update: (t) => {
        if (cancelled || done) return false;
        if (t < start) return true;
        const raw = Math.min(1, (t - start) / Math.max(1, opts.duration));
        opts.onUpdate(easing(raw));
        if (raw >= 1) {
          done = true;
          opts.onComplete?.();
          return false;
        }
        return true;
      },
      cancel: () => {
        cancelled = true;
      },
    };
    this.tweens.add(tween);
    return tween;
  }

  update(now: number): void {
    for (const t of this.tweens) {
      if (!t.update(now)) this.tweens.delete(t);
    }
  }

  get active(): number {
    return this.tweens.size;
  }
}

export const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));
export const lerp = (a: number, b: number, t: number): number => a + (b - a) * t;
/** Exponential smoothing that is frame-rate independent. */
export const damp = (a: number, b: number, lambda: number, dt: number): number => lerp(a, b, 1 - Math.exp(-lambda * dt));
