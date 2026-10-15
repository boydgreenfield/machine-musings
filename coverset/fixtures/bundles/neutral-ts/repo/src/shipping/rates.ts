export type Zone = "domestic" | "eu" | "world";

const BASE: Record<Zone, number> = { domestic: 500, eu: 900, world: 1500 };

export function rateFor(zone: Zone, weightsGrams: number[]): number {
  const total = weightsGrams.reduce((sum, g) => sum + g, 0);
  const extra = Math.max(0, Math.ceil((total - 1000) / 500)) * 200;
  return BASE[zone] + extra;
}

export function isFreeShipping(totalCents: number): boolean {
  return totalCents >= 5000;
}
