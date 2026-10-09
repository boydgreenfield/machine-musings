import type { Graph, GraphNode } from '../graph';
import type { LayoutId } from '../layouts/index';
import type { LensId, Store } from '../state';

export interface TourStop {
  title: string;
  text: string;
  layout?: LayoutId;
  lens?: LensId;
  select?: string | null;
  /** Seconds before auto-advancing. */
  dwell?: number;
}

export const TOUR: TourStop[] = [
  {
    title: 'One profession, many worlds',
    text: 'At the centre sits medicine itself. Around it circle the primary specialties a graduate can enter directly from medical school, grouped by family: generalists, medical, surgical, acute care, diagnostic, mind, and population health. Each is sized by how many physicians practise it.',
    layout: 'orbits', lens: 'category', select: null, dwell: 14,
  },
  {
    title: 'The front door',
    text: 'Family medicine and internal medicine are the two largest specialties, each with well over a hundred thousand active physicians. Together with paediatrics they form the primary-care core from which much of the rest of medicine grows.',
    select: 'family-medicine', dwell: 11,
  },
  {
    title: 'A tree within a tree',
    text: 'Internal medicine sponsors more subspecialty certificates than any other board. Cardiology alone spawns four further fellowships: interventional cardiology, electrophysiology, advanced heart failure, and adult congenital heart disease.',
    select: 'internal-medicine', dwell: 12,
  },
  {
    title: 'Fields that belong to everyone',
    text: 'Some subspecialties cannot be claimed by one board. Hospice and palliative medicine is co-sponsored by ten of them, so it drifts toward the centre of the map, reachable from almost any corner of medicine. Fields like this are marked ⟡ as bridges.',
    select: 'hospice-palliative', dwell: 13,
  },
  {
    title: 'Where surgery meets medicine',
    text: 'Neuroendovascular surgery threads catheters into the vessels of the brain. It can be reached from neurosurgery, neurology, or interventional radiology, three very different training cultures converging on one technique.',
    select: 'neuroendovascular', dwell: 11,
  },
  {
    title: 'The long road',
    text: 'In the training tower, height is years after medical school. Neurosurgery has the longest residency at seven years, before any fellowship. Each ring is one more year.',
    layout: 'tower', lens: 'training', select: 'neurological-surgery', dwell: 12,
  },
  {
    title: 'The top floor',
    text: 'The highest nodes stand eight years above medical school: three years of internal medicine, three of cardiology, and two more to become a clinical cardiac electrophysiologist.',
    select: 'electrophysiology', dwell: 10,
  },
  {
    title: 'Medicine by body system',
    text: 'The same fields regrouped by the organ system they serve. A cardiologist, a cardiac surgeon, a cardiac anaesthesiologist, and a cardiothoracic radiologist sit together here, though they share no board.',
    layout: 'systems', lens: 'domain', select: null, dwell: 12,
  },
  {
    title: 'Who does the work',
    text: 'Colour and size now follow workforce. A handful of large generalist and hospital-based fields dwarf the rest; many subspecialties number only a few hundred physicians nationwide.',
    layout: 'constellation', lens: 'workforce', select: null, dwell: 11,
  },
  {
    title: 'Explore on your own',
    text: 'Click any node to read about it, search with /, switch layouts with 1–4, and change the colour lens with C. The map is yours.',
    layout: 'orbits', lens: 'category', select: null, dwell: 8,
  },
];

export class Tour {
  private index = -1;
  private timer = 0;
  private elapsed = 0;
  private running = false;
  private readonly bar: HTMLElement;
  private readonly title: HTMLElement;
  private readonly text: HTMLElement;
  private readonly progress: HTMLElement;

  constructor(
    root: Document,
    private readonly graph: Graph,
    private readonly store: Store,
    private readonly select: (n: GraphNode | null, fly: boolean) => void,
  ) {
    this.bar = root.getElementById('tour-bar')!;
    this.title = root.getElementById('tour-title')!;
    this.text = root.getElementById('tour-text')!;
    this.progress = root.getElementById('tour-progress')!;
    root.getElementById('tour-prev')!.addEventListener('click', () => this.go(this.index - 1));
    root.getElementById('tour-next')!.addEventListener('click', () => this.go(this.index + 1));
    root.getElementById('tour-close')!.addEventListener('click', () => this.stop());
  }

  get active() { return this.running; }

  start() {
    this.running = true;
    this.bar.hidden = false;
    this.store.set({ tourActive: true });
    this.progress.innerHTML = TOUR.map(() => '<i><b></b></i>').join('');
    this.go(0);
  }

  stop() {
    this.running = false;
    this.bar.hidden = true;
    this.store.set({ tourActive: false });
  }

  toggle() { this.running ? this.stop() : this.start(); }

  private go(i: number) {
    if (i < 0) i = TOUR.length - 1;
    if (i >= TOUR.length) { this.stop(); return; }
    this.index = i;
    this.elapsed = 0;
    this.timer = TOUR[i].dwell ?? 10;
    const stop = TOUR[i];
    this.title.textContent = stop.title;
    this.text.textContent = stop.text;
    const patch: Partial<typeof this.store.state> = {};
    if (stop.layout && stop.layout !== this.store.state.layout) patch.layout = stop.layout;
    if (stop.lens && stop.lens !== this.store.state.lens) patch.lens = stop.lens;
    if (!this.store.state.showFellowships) patch.showFellowships = true;
    if (this.store.state.categories.size !== this.graph.categories.length) patch.categories = new Set(this.graph.categories);
    this.store.set(patch);
    if (stop.select !== undefined) {
      const n = stop.select ? this.graph.byId.get(stop.select) ?? null : null;
      // Let the layout transition begin before flying.
      setTimeout(() => { if (this.running && this.index === i) this.select(n, true); }, patch.layout ? 120 : 0);
    }
    Array.from(this.progress.children).forEach((el, k) => {
      el.classList.toggle('done', k < i);
      const b = el.querySelector('b') as HTMLElement;
      if (k > i) b.style.width = '0';
    });
  }

  /** Called every frame. */
  tick(dt: number) {
    if (!this.running) return;
    this.elapsed += dt;
    const el = this.progress.children[this.index]?.querySelector('b') as HTMLElement | undefined;
    if (el) el.style.width = `${Math.min(100, (this.elapsed / this.timer) * 100)}%`;
    if (this.elapsed >= this.timer) this.go(this.index + 1);
  }
}
