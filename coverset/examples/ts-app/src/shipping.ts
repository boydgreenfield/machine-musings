export type Zone = "domestic" | "eu" | "world";

const BASE_CENTS: Record<Zone, number> = { domestic: 300, eu: 700, world: 1500 };

/** Shipping for a parcel of items: a base rate per zone plus 2.00 per started kilogram above one. */
export function rateFor(zone: Zone, weightsGrams: number[]): number {
  const grams = weightsGrams.reduce((sum, g) => sum + g, 0);
  const extraKg = Math.max(0, Math.ceil((grams - 1000) / 1000));
  return BASE_CENTS[zone] + extraKg * 200;
}
