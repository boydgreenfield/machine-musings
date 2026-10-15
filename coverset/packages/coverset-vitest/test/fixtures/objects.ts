export interface Item {
  sku: string;
  qty: number;
}

export type ItemAlias = { sku: string; qty: number };

export class Sq {
  constructor(readonly side: number) {}
}

export type Zone = "world" | "eu" | "domestic";

export enum Dir {
  Up = "up",
  Down = "down",
}

export function maybeItem(c: Item | null, d: Sq | null, e: ItemAlias | undefined): number {
  return (c?.qty ?? 0) + (d?.side ?? 0) + (e?.qty ?? 0);
}

export function shapes(
  ctor: typeof Sq,
  cb: (n: number) => void,
  part: Partial<Item>,
  methods: { a(): void },
  when: Date,
  pending: Promise<number>,
  bag: { [k: string]: number; size: number },
): void {
  void [ctor, cb, part, methods, when, pending, bag];
}

export function ordered(zone: Zone, mixed: number | "auto" | null, flags: false | "x" | true, dir: undefined | Dir): "b" | "a" {
  void [zone, mixed, flags, dir];
  return "b";
}
