declare module 'd3-force-3d' {
  export interface SimNode {
    index?: number;
    x: number; y: number; z: number;
    vx?: number; vy?: number; vz?: number;
    fx?: number | null; fy?: number | null; fz?: number | null;
  }
  export interface SimLink<N extends SimNode = SimNode> {
    source: N | string | number;
    target: N | string | number;
    index?: number;
  }
  export interface Force<N extends SimNode = SimNode> {
    (alpha: number): void;
    initialize?(nodes: N[], random?: () => number): void;
  }
  export interface Simulation<N extends SimNode = SimNode, L extends SimLink<N> = SimLink<N>> {
    restart(): this;
    stop(): this;
    tick(iterations?: number): this;
    nodes(): N[];
    nodes(nodes: N[]): this;
    alpha(): number; alpha(a: number): this;
    alphaMin(): number; alphaMin(a: number): this;
    alphaDecay(): number; alphaDecay(a: number): this;
    alphaTarget(): number; alphaTarget(a: number): this;
    velocityDecay(): number; velocityDecay(a: number): this;
    numDimensions(): number; numDimensions(n: 1 | 2 | 3): this;
    randomSource(): () => number; randomSource(fn: () => number): this;
    force(name: string): Force<N> | undefined;
    force(name: string, force: Force<N> | null): this;
    find(x: number, y?: number, z?: number, radius?: number): N | undefined;
    on(name: string, listener: ((this: this) => void) | null): this;
  }
  export function forceSimulation<N extends SimNode = SimNode>(nodes?: N[], numDimensions?: 1 | 2 | 3): Simulation<N>;

  export interface ForceLink<N extends SimNode, L extends SimLink<N>> extends Force<N> {
    links(): L[]; links(l: L[]): this;
    id(fn: (d: N, i: number) => string | number): this;
    distance(d: number | ((l: L, i: number) => number)): this;
    strength(s: number | ((l: L, i: number) => number)): this;
    iterations(n: number): this;
  }
  export function forceLink<N extends SimNode = SimNode, L extends SimLink<N> = SimLink<N>>(links?: L[]): ForceLink<N, L>;

  export interface ForceManyBody<N extends SimNode> extends Force<N> {
    strength(s: number | ((d: N, i: number) => number)): this;
    theta(t: number): this;
    distanceMin(d: number): this;
    distanceMax(d: number): this;
  }
  export function forceManyBody<N extends SimNode = SimNode>(): ForceManyBody<N>;

  export interface ForceCenter<N extends SimNode> extends Force<N> {
    x(v: number): this; y(v: number): this; z(v: number): this;
    strength(s: number): this;
  }
  export function forceCenter<N extends SimNode = SimNode>(x?: number, y?: number, z?: number): ForceCenter<N>;

  export interface ForceCollide<N extends SimNode> extends Force<N> {
    radius(r: number | ((d: N, i: number) => number)): this;
    strength(s: number): this;
    iterations(n: number): this;
  }
  export function forceCollide<N extends SimNode = SimNode>(radius?: number | ((d: N, i: number) => number)): ForceCollide<N>;

  export interface ForceRadial<N extends SimNode> extends Force<N> {
    radius(r: number | ((d: N, i: number) => number)): this;
    strength(s: number | ((d: N, i: number) => number)): this;
    x(v: number): this; y(v: number): this; z(v: number): this;
  }
  export function forceRadial<N extends SimNode = SimNode>(radius: number | ((d: N, i: number) => number), x?: number, y?: number, z?: number): ForceRadial<N>;

  export interface ForceAxis<N extends SimNode> extends Force<N> {
    strength(s: number | ((d: N, i: number) => number)): this;
    x?(v: number | ((d: N, i: number) => number)): this;
    y?(v: number | ((d: N, i: number) => number)): this;
    z?(v: number | ((d: N, i: number) => number)): this;
  }
  export function forceX<N extends SimNode = SimNode>(x?: number | ((d: N, i: number) => number)): ForceAxis<N> & { x(v: number | ((d: N, i: number) => number)): ForceAxis<N> };
  export function forceY<N extends SimNode = SimNode>(y?: number | ((d: N, i: number) => number)): ForceAxis<N> & { y(v: number | ((d: N, i: number) => number)): ForceAxis<N> };
  export function forceZ<N extends SimNode = SimNode>(z?: number | ((d: N, i: number) => number)): ForceAxis<N> & { z(v: number | ((d: N, i: number) => number)): ForceAxis<N> };
}
