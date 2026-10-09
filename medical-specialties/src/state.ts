import type { Category } from './data/schema';
import type { GraphNode } from './graph';
import type { LayoutId } from './layouts/index';

export type LensId = 'category' | 'domain' | 'training' | 'workforce' | 'credential';

export interface AppState {
  layout: LayoutId;
  lens: LensId;
  showFellowships: boolean;
  categories: Set<Category>;
  selected: GraphNode | null;
  hovered: GraphNode | null;
  /** Ids matched by the current search (null = no active search). */
  searchHits: Set<string> | null;
  labelDensity: number; // 0..1
  autoRotate: boolean;
  bloom: boolean;
  flow: boolean;
  tourActive: boolean;
}

type Listener = (state: AppState, changed: Set<keyof AppState>) => void;

export class Store {
  state: AppState;
  private listeners = new Set<Listener>();

  constructor(initial: AppState) {
    this.state = initial;
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  set(patch: Partial<AppState>) {
    const changed = new Set<keyof AppState>();
    for (const k of Object.keys(patch) as (keyof AppState)[]) {
      if (this.state[k] !== patch[k]) {
        (this.state as unknown as Record<string, unknown>)[k] = patch[k];
        changed.add(k);
      }
    }
    if (changed.size) for (const l of this.listeners) l(this.state, changed);
  }

  /** Visibility predicate derived from the current filters. */
  isVisible(n: GraphNode): boolean {
    const s = this.state;
    if (n.tier === 'root') return true;
    if (!s.showFellowships && n.tier === 'fellowship') return false;
    if (!s.categories.has(n.category)) return false;
    if (s.searchHits && !s.searchHits.has(n.id)) return false;
    return true;
  }
}

export function createStore(allCategories: Category[]): Store {
  return new Store({
    layout: 'orbits',
    lens: 'category',
    showFellowships: true,
    categories: new Set(allCategories),
    selected: null,
    hovered: null,
    searchHits: null,
    labelDensity: 0.5,
    autoRotate: true,
    bloom: true,
    flow: true,
    tourActive: false,
  });
}
