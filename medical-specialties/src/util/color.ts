import { Color } from 'three';

const tmp = new Color();

export function hexToRgb(hex: string): [number, number, number] {
  tmp.set(hex);
  return [tmp.r, tmp.g, tmp.b];
}

/** Mix two hex colours in linear-ish sRGB space; t in [0,1]. */
export function mixHex(a: string, b: string, t: number): string {
  const ca = new Color(a);
  const cb = new Color(b);
  return '#' + ca.lerp(cb, t).getHexString();
}

/** Multi-stop ramp. */
export function ramp(stops: string[], t: number): string {
  const n = stops.length - 1;
  const x = Math.max(0, Math.min(1, t)) * n;
  const i = Math.min(n - 1, Math.floor(x));
  return mixHex(stops[i], stops[i + 1], x - i);
}

/** Sequential ramps (one hue, dim → bright, tuned for a dark surface). */
export const RAMP_AMBER = ['#3a2d12', '#8a6a1f', '#d9a63a', '#ffd166', '#fff1c2'];
export const RAMP_TEAL = ['#0f3a38', '#1b6f69', '#27a69a', '#2ec4b6', '#b9f3ee'];
