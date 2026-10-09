import { CATEGORY_META, CREDENTIAL_META, DOMAIN_META } from '../data/schema';
import { boardName, type Graph, type GraphNode } from '../graph';
import type { Store } from '../state';

const fmt = new Intl.NumberFormat('en-US');

function esc(s: string) {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!);
}

/** Shortest training path from medical school to this node. */
function trainingPath(n: GraphNode): GraphNode[] {
  const path: GraphNode[] = [n];
  let cur = n;
  while (cur.parentNodes.length) {
    // Prefer the parent with the shortest total years.
    cur = cur.parentNodes.reduce((a, b) => (b.totalYears < a.totalYears ? b : a));
    path.unshift(cur);
  }
  return path;
}

export class DetailPanel {
  constructor(private readonly el: HTMLElement, private readonly graph: Graph, store: Store, onPick: (n: GraphNode) => void) {
    el.addEventListener('click', (e) => {
      const t = (e.target as HTMLElement).closest<HTMLElement>('[data-node]');
      if (t) { const n = graph.byId.get(t.dataset.node!); if (n) onPick(n); return; }
      if ((e.target as HTMLElement).closest('.panel-close')) store.set({ selected: null });
    });
    void this.graph;
  }

  render(n: GraphNode | null) {
    if (!n) { this.el.hidden = true; this.el.innerHTML = ''; return; }
    const cat = CATEGORY_META[n.category];
    const dom = DOMAIN_META[n.domain];
    const cred = CREDENTIAL_META[n.credential];
    const path = trainingPath(n);
    const parents = n.parentNodes.filter((p) => p.tier !== 'root');
    const kids = [...n.children].sort((a, b) => (b.workforce ?? 0) - (a.workforce ?? 0));
    const siblings = n.tier === 'root' ? [] : parents.flatMap((p) => p.children).filter((s, i, arr) => s !== n && arr.indexOf(s) === i).slice(0, 12);

    const years = n.tier === 'root' ? null : n.tier === 'specialty' && n.residency ? `${n.residency}<small>yr residency</small>` : n.fellowship === 0 ? '—' : `+${n.fellowship}<small>yr fellowship</small>`;

    const chips = (arr: GraphNode[], muted = false) => arr.map((c) => `<button class="chip ${muted ? 'muted' : ''}" data-node="${c.id}"><span class="swatch" style="color:${CATEGORY_META[c.category].color};background:${CATEGORY_META[c.category].color}"></span>${esc(c.short ?? c.name)}${c.isBridge ? ' <span title="Bridges several boards">⟡</span>' : ''}</button>`).join('');

    const eyebrow = n.tier === 'root' ? 'Where it all begins' : `${n.tier === 'specialty' ? 'Specialty' : n.tier === 'subspecialty' ? 'Subspecialty' : 'Fellowship'} · ${cat.label}${n.isBridge ? ' · Bridge' : ''}`;

    this.el.innerHTML = `
      <button class="panel-close" aria-label="Close">×</button>
      <p class="panel-eyebrow"><span class="swatch" style="color:${cat.color};background:${cat.color}"></span>${esc(eyebrow)}</p>
      <h2>${esc(n.name)}</h2>
      ${n.aka?.length ? `<p class="aka">Also: ${esc(n.aka.join(', '))}</p>` : ''}
      <p class="desc">${esc(n.description)}</p>
      ${n.note ? `<p class="note"><b>Note</b> · ${esc(n.note)}</p>` : ''}
      ${n.tier === 'root' ? '' : `
      <div class="facts">
        <div class="fact"><span class="k">Training</span><span class="v">${years}</span></div>
        <div class="fact"><span class="k">Years after med school</span><span class="v">${n.totalYears}<small>total</small></span></div>
        <div class="fact"><span class="k">Active US physicians</span><span class="v">${n.workforce ? `≈${fmt.format(n.workforce)}` : '—'}</span></div>
        <div class="fact"><span class="k">Credential</span><span class="v" style="font-size:13px">${esc(cred.label)}</span></div>
        <div class="fact wide"><span class="k">Shortest training path</span>
          <div class="path">${path.map((p, i) => `${i ? '<span class="arrow">→</span>' : ''}<button class="step ${p === n ? 'me' : ''}" data-node="${p.id}">${esc(p.tier === 'root' ? 'Medical school' : (p.short ?? p.name))}${p.tier === 'specialty' && p.residency ? ` · ${p.residency}y` : p.fellowship ? ` · +${p.fellowship}y` : ''}</button>`).join('')}</div>
        </div>
      </div>`}
      ${n.boards.length ? `<h3>Certifying board${n.boards.length > 1 ? 's' : ''}</h3><div class="boards"><ul>${n.boards.map((b) => `<li><b>${esc(b)}</b> · ${esc(boardName(b))}</li>`).join('')}</ul></div>` : ''}
      ${parents.length ? `<h3>${n.isBridge ? `Bridges ${parents.length} specialties` : 'Builds on'}</h3><div class="chips">${chips(parents)}</div>` : ''}
      ${kids.length ? `<h3>${n.tier === 'root' ? `${kids.length} primary specialties` : `${kids.length} subspecialt${kids.length === 1 ? 'y' : 'ies'} & fellowships`}</h3><div class="chips">${chips(kids)}</div>` : ''}
      ${siblings.length ? `<h3>Related fields</h3><div class="chips">${chips(siblings, true)}</div>` : ''}
      <h3>Domain</h3><div class="chips"><span class="chip muted"><span class="swatch" style="color:${dom.color};background:${dom.color}"></span>${esc(dom.label)}</span></div>
    `;
    this.el.hidden = false;
    this.el.scrollTop = 0;
  }
}
