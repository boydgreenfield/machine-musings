export enum Color {
  Red = "red",
  Green = "green",
}

export enum Level {
  Low,
  High,
}

export interface Point {
  x: number;
  y: number;
}

export class Money {
  constructor(readonly cents: number) {}

  /** Adds two amounts. */
  plus(other: Money): Money {
    return new Money(this.cents + other.cents);
  }

  get euros(): number {
    return this.cents / 100;
  }

  set euros(value: number) {
    void value;
  }

  static zero(): Money {
    return new Money(0);
  }
}

export function scalars(n: number, s: string, b: boolean, big: bigint, maybe?: string, nothing?: null): void {
  void [n, s, b, big, maybe, nothing];
}

export function literals(zone: "eu" | "world", digit: 1 | 2 | 3, flag: true, color: Color | undefined, low: Color.Red): void {
  void [zone, digit, flag, color, low];
}

export function containers(
  xs: number[],
  ro: readonly string[],
  pair: [number, string],
  rest: [number, ...string[]],
  rec: Record<string, number>,
  map: Map<string, Point>,
  set: Set<number>,
  it: Iterable<number>,
  bytes: Uint8Array,
  index: { [key: string]: boolean },
): void {
  void [xs, ro, pair, rest, rec, map, set, it, bytes, index];
}

export function objects(p: Point, m: Money, level: Level, a: any, u: unknown, d: Date, f: (x: number) => number, untyped = 1): void {
  void [p, m, level, a, u, d, f, untyped];
}

export async function fetchCount(id: string | number): Promise<number> {
  return Number(id);
}

export function outer(values: number[]): number[] {
  function inner(v: number): number {
    return v * 2;
  }
  return values.map((v) => inner(v)).filter((v) => v > 2);
}

export const api = {
  get(key: string): string {
    return key;
  },
  put: (key: string, { value }: { value: number }) => key.length + value,
};

export const double = (n: number) => n * 2;

export default function (x: number): number {
  return x;
}

export namespace Geometry {
  export function area(p: Point): number {
    return p.x * p.y;
  }
}
