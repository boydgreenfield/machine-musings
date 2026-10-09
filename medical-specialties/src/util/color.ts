import { Color } from 'three';

const cache = new Map<string, Color>();

export function colorOf(hex: string): Color {
  let c = cache.get(hex);
  if (!c) {
    c = new Color(hex);
    cache.set(hex, c);
  }
  return c;
}

/** Hex colour with alpha, for CSS. */
export function withAlpha(hex: string, alpha: number): string {
  const c = new Color(hex);
  return `rgba(${Math.round(c.r * 255)}, ${Math.round(c.g * 255)}, ${Math.round(c.b * 255)}, ${alpha})`;
}
