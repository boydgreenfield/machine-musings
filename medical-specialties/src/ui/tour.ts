import type { Graph } from '../data/graph';
import type { SceneApp } from '../scene/app';
import type { Store } from '../state';

interface TourStep {
  title: string;
  text: string;
  run: () => void;
}

/** A short scripted walk through the atlas. */
export function setupTour(graph: Graph, store: Store, app: SceneApp): { start: () => void; stop: () => void; active: () => boolean } {
  const card = document.getElementById('tour')!;
  const title = card.querySelector<HTMLElement>('.tour-title')!;
  const text = card.querySelector<HTMLElement>('.tour-text')!;
  const counter = card.querySelector<HTMLElement>('.tour-counter')!;
  const back = card.querySelector<HTMLButtonElement>('[data-tour="back"]')!;
  const next = card.querySelector<HTMLButtonElement>('[data-tour="next"]')!;
  const end = card.querySelector<HTMLButtonElement>('[data-tour="end"]')!;
  const startBtn = document.getElementById('tour-btn')!;

  const s = graph.stats;
  const steps: TourStep[] = [
    {
      title: 'One degree, many paths',
      text: 'Four years of medical school lead to a single qualification. Everything after it branches: the atlas maps those branches.',
      run: () => {
        store.setLayout('constellation');
        store.setShowCross(false);
        store.select('medicine');
      },
    },
    {
      title: 'Nine domains',
      text: `Colour is the domain, size is the level. Nine families of practice contain ${s.specialties} specialties, each with its own residency and board.`,
      run: () => {
        store.select(null);
        app.resetView();
      },
    },
    {
      title: 'A specialty and its fellowships',
      text: 'Internal medicine is the largest trunk: three years of residency open more than twenty organ-system fellowships.',
      run: () => store.select('internal-medicine'),
    },
    {
      title: 'Paths that stack',
      text: 'Some certificates sit three deep. Interventional cardiology is three years of internal medicine, three of cardiology and one more in the catheter lab.',
      run: () => store.select('interventional-cardiology'),
    },
    {
      title: 'Cross-board pathways',
      text: `Dashed arcs show fellowships reachable from more than one specialty. Sleep medicine is certified by six boards; the atlas holds ${s.crossPathways} such routes.`,
      run: () => {
        store.setShowCross(true);
        store.select('sleep-medicine');
      },
    },
    {
      title: 'Training years',
      text: 'Lay the same graph out by time and the shape changes: a three-year family medicine residency on the left, a nine-year neurosurgical path on the right.',
      run: () => {
        store.setShowCross(false);
        store.select(null);
        store.setLayout('timeline');
      },
    },
    {
      title: 'Organ systems',
      text: 'Regroup by what each discipline serves and kinships across boards appear: cardiology, cardiac surgery, cardiac anesthesia and cardiac imaging share one cluster.',
      run: () => store.setLayout('systems'),
    },
    {
      title: 'Explore',
      text: 'Drag to orbit, scroll to zoom, click any node. Press / to search, 1–4 to switch layouts and ? for every shortcut.',
      run: () => {
        store.setLayout('constellation');
        store.select(null);
      },
    },
  ];

  let index = -1;

  const render = () => {
    const step = steps[index];
    if (!step) return;
    title.textContent = step.title;
    text.textContent = step.text;
    counter.textContent = `${index + 1} / ${steps.length}`;
    back.disabled = index === 0;
    next.textContent = index === steps.length - 1 ? 'Finish' : 'Next';
    step.run();
  };

  const start = () => {
    index = 0;
    card.removeAttribute('hidden');
    document.body.classList.add('tour-active');
    app.setIdleRotate(false);
    render();
  };
  const stop = () => {
    index = -1;
    card.setAttribute('hidden', '');
    document.body.classList.remove('tour-active');
    app.setIdleRotate(true);
  };

  startBtn.addEventListener('click', start);
  back.addEventListener('click', () => {
    if (index > 0) {
      index--;
      render();
    }
  });
  next.addEventListener('click', () => {
    if (index >= steps.length - 1) stop();
    else {
      index++;
      render();
    }
  });
  end.addEventListener('click', stop);

  return { start, stop, active: () => index >= 0 };
}
