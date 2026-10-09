import {
  Color, FogExp2, PerspectiveCamera, Raycaster, Scene, Vector2, Vector3, WebGLRenderer,
} from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import type { Graph, GraphNode } from '../graph';
import { CATEGORY_META, DOMAIN_META } from '../data/schema';
import { computeLayout, LAYOUTS, type LayoutId } from '../layouts/index';
import type { Store, LensId, AppState } from '../state';
import { easeInOutCubic, clamp } from '../util/easing';
import { hash01 } from '../util/prng';
import { ramp, RAMP_AMBER, RAMP_TEAL } from '../util/color';
import { NodeCloud } from './nodes';
import { EdgeCloud } from './edges';
import { Labels } from './labels';
import { Stars } from './stars';
import { Guides } from './guides';

export const CREDENTIAL_COLORS: Record<string, string> = {
  board: '#ffd166',
  subspecialty: '#2ec4b6',
  'focused-practice': '#c4b5fd',
  fellowship: '#8da2c0',
};

export interface AtlasEvents {
  onHover(node: GraphNode | null, clientX: number, clientY: number): void;
  onSelect(node: GraphNode | null): void;
}

interface Flight {
  fromPos: Vector3; toPos: Vector3; fromTarget: Vector3; toTarget: Vector3; t: number; duration: number;
}

const BG = '#06091a';

export class Atlas {
  readonly renderer: WebGLRenderer;
  readonly scene = new Scene();
  readonly camera: PerspectiveCamera;
  readonly controls: OrbitControls;
  readonly composer: EffectComposer;
  readonly bloomPass: UnrealBloomPass;
  readonly nodes: NodeCloud;
  readonly edges: EdgeCloud;
  readonly labels: Labels;
  readonly stars: Stars;
  readonly guides: Guides;

  /** Layout positions: where nodes are heading, where they came from, and the blend. */
  private readonly posFrom: Float32Array;
  private readonly posTo: Float32Array;
  private readonly posCur: Float32Array;
  private readonly posDisplay: Float32Array;
  private readonly stagger: Float32Array;
  private transitionT = 1;
  private transitionDur = 1.6;
  private readonly layoutCache = new Map<LayoutId, Float32Array>();

  private readonly raycaster = new Raycaster();
  private readonly pointer = new Vector2();
  private pointerDirty = false;
  private pointerClient = { x: 0, y: 0 };
  private hovered: GraphNode | null = null;
  private lastInteraction = 0;
  private flight: Flight | null = null;
  private time = 0;
  private lastFrame = performance.now();
  private focus = new Set<number>();
  private running = true;
  private frameHook: ((dt: number) => void) | null = null;

  constructor(
    readonly canvas: HTMLCanvasElement,
    readonly graph: Graph,
    readonly store: Store,
    fontUrl: string,
    readonly events: AtlasEvents,
  ) {
    const n = graph.nodes.length;
    this.posFrom = new Float32Array(n * 3);
    this.posTo = new Float32Array(n * 3);
    this.posCur = new Float32Array(n * 3);
    this.posDisplay = new Float32Array(n * 3);
    this.stagger = new Float32Array(n);
    for (const node of graph.nodes) this.stagger[node.index] = hash01(node.id + 'stagger') * 0.35;

    this.renderer = new WebGLRenderer({ canvas, antialias: true, alpha: false, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setClearColor(new Color(BG), 1);

    this.scene.background = new Color(BG);
    this.scene.fog = new FogExp2(BG, 0.0024);

    this.camera = new PerspectiveCamera(48, canvas.clientWidth / Math.max(1, canvas.clientHeight) || 1.6, 0.5, 2000);
    const cam = this.defaultCamera(store.state.layout);
    this.camera.position.copy(cam.position);

    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.06;
    this.controls.rotateSpeed = 0.6;
    this.controls.zoomSpeed = 0.8;
    this.controls.minDistance = 6;
    this.controls.maxDistance = 420;
    this.controls.autoRotateSpeed = 0.35;
    this.controls.target.copy(cam.target);
    this.controls.addEventListener('start', () => { this.lastInteraction = this.time; this.flight = null; });

    this.nodes = new NodeCloud(graph);
    this.edges = new EdgeCloud(graph);
    this.labels = new Labels(graph, fontUrl);
    this.stars = new Stars();
    this.guides = new Guides(fontUrl);
    this.scene.add(this.stars.points, this.edges.lines, this.edges.flow, this.nodes.mesh, this.nodes.halos, this.labels.group, this.guides.group);

    this.composer = new EffectComposer(this.renderer);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    this.bloomPass = new UnrealBloomPass(new Vector2(1, 1), 0.6, 0.5, 0.9);
    this.composer.addPass(this.bloomPass);
    this.composer.addPass(new OutputPass());

    // Initial layout
    const first = this.layoutFor(store.state.layout);
    this.posFrom.set(first); this.posTo.set(first); this.posCur.set(first);
    this.applyLens(store.state.lens);
    this.nodes.snapColors();
    this.applyFilters();
    this.applyEmphasis();

    this.resize();
    window.addEventListener('resize', () => this.resize());
    this.bindPointer();

    store.subscribe((s, changed) => this.onState(s, changed));
    requestAnimationFrame(() => this.frame());
  }

  // ── State plumbing ────────────────────────────────────────────────────────

  private onState(s: AppState, changed: Set<keyof AppState>) {
    if (changed.has('layout')) this.goToLayout(s.layout);
    if (changed.has('lens')) this.applyLens(s.lens);
    if (changed.has('showFellowships') || changed.has('categories') || changed.has('searchHits')) this.applyFilters();
    if (changed.has('selected') || changed.has('hovered') || changed.has('searchHits') || changed.has('showFellowships') || changed.has('categories')) this.applyEmphasis();
    if (changed.has('bloom')) this.bloomPass.enabled = s.bloom;
    if (changed.has('autoRotate')) this.lastInteraction = this.time;
  }

  private layoutFor(id: LayoutId): Float32Array {
    let p = this.layoutCache.get(id);
    if (!p) {
      const r = computeLayout(id, this.graph);
      p = r.positions;
      this.layoutCache.set(id, p);
    }
    return p;
  }

  goToLayout(id: LayoutId) {
    const res = computeLayout(id, this.graph);
    this.layoutCache.set(id, res.positions);
    this.posFrom.set(this.posCur);
    this.posTo.set(res.positions);
    this.transitionT = 0;
    this.transitionDur = 1.7;
    this.guides.setRings(res.guides);
    this.guides.setMarkers(res.markers);
    this.edges.globalTarget = res.edgeOpacity ?? 1;
    if (res.lens && res.lens !== this.store.state.lens) this.store.set({ lens: res.lens });
    const sel = this.store.state.selected;
    if (sel) this.flyToNode(sel, 1.7);
    else { const { position, target } = this.defaultCamera(id); this.flyTo(position, target, 1.7); }
  }

  applyLens(lens: LensId) {
    const maxYears = Math.max(...this.graph.nodes.map((n) => n.totalYears));
    const wfs = this.graph.nodes.map((n) => Math.log10(n.workforce ?? 100));
    const wfMin = Math.min(...wfs), wfMax = Math.max(...wfs);
    for (const n of this.graph.nodes) {
      let hex: string;
      switch (lens) {
        case 'category': hex = CATEGORY_META[n.category].color; break;
        case 'domain': hex = DOMAIN_META[n.domain].color; break;
        case 'training': hex = ramp(RAMP_AMBER, (n.totalYears - 3) / (maxYears - 3)); break;
        case 'workforce': hex = ramp(RAMP_TEAL, (Math.log10(n.workforce ?? 100) - wfMin) / (wfMax - wfMin)); break;
        case 'credential': hex = CREDENTIAL_COLORS[n.credential]; break;
      }
      if (n.tier === 'root') hex = '#fff1c2';
      this.nodes.setColor(n.index, hex);
    }
  }

  applyFilters() {
    for (const n of this.graph.nodes) this.nodes.visTarget[n.index] = this.store.isVisible(n) ? 1 : 0;
  }

  /** Nodes whose labels/links should stay bright given selection, hover and search. */
  private neighbourhood(n: GraphNode): Set<number> {
    const set = new Set<number>([n.index]);
    for (const a of n.ancestors) set.add(a.index);
    const stack = [...n.children];
    while (stack.length) { const c = stack.pop()!; if (set.has(c.index)) continue; set.add(c.index); stack.push(...c.children); }
    return set;
  }

  applyEmphasis() {
    const s = this.store.state;
    const sel = s.selected;
    const hov = s.hovered;
    const nodes = this.graph.nodes;
    const focus = new Set<number>();
    const emph = this.nodes.emphTarget;
    const glow = this.nodes.glowTarget;
    emph.fill(1); glow.fill(0);

    let hood: Set<number> | null = null;
    if (sel) {
      hood = this.neighbourhood(sel);
      for (const n of nodes) emph[n.index] = hood.has(n.index) ? 1 : 0.1;
      glow[sel.index] = 1;
      for (const i of hood) focus.add(i);
    }
    if (hov && hov !== sel) {
      glow[hov.index] = Math.max(glow[hov.index], 0.7);
      const local = new Set<number>([hov.index, ...hov.parentNodes.map((p) => p.index), ...hov.children.map((c) => c.index)]);
      if (!sel) for (const n of nodes) emph[n.index] = local.has(n.index) ? 1 : 0.7;
      for (const i of local) if (!sel || hood!.has(i) || i === hov.index) focus.add(i);
    }
    if (s.searchHits) for (const n of nodes) if (s.searchHits.has(n.id)) focus.add(n.index);

    const eEmph = this.edges.emphTarget;
    for (const e of this.graph.edges) {
      let v = 1;
      if (sel) {
        const inHood = hood!.has(e.source.index) && hood!.has(e.target.index);
        v = inHood ? (e.source === sel || e.target === sel ? 1 : 0.7) : 0.05;
      }
      if (hov && hov !== sel && (e.source === hov || e.target === hov)) v = Math.max(v, 1);
      else if (hov && !sel) v *= 0.7;
      eEmph[e.index] = v;
    }
    this.focus = focus;
  }

  // ── Camera ────────────────────────────────────────────────────────────────

  flyTo(pos: Vector3, target: Vector3, duration = 1.4) {
    this.flight = { fromPos: this.camera.position.clone(), toPos: pos, fromTarget: this.controls.target.clone(), toTarget: target, t: 0, duration };
    this.lastInteraction = this.time;
  }

  /** Position of a node at the end of the current transition (layout target). */
  nodeTarget(n: GraphNode): Vector3 {
    return new Vector3(this.posTo[n.index * 3], this.posTo[n.index * 3 + 1], this.posTo[n.index * 3 + 2]);
  }

  flyToNode(n: GraphNode, duration = 1.4) {
    if (n.tier === 'root') { this.resetView(duration); return; }
    const node = this.nodeTarget(n);
    const aspect = this.camera.aspect || 1.6;
    const portrait = aspect < 1.3;
    let dist = n.tier === 'specialty' ? 30 + n.radius * 6 : 14 + n.radius * 7;
    if (portrait) dist *= Math.pow(1.3 / aspect, 0.6);
    // Keep the current viewing direction, lifted a little, so the flight feels like a zoom.
    const dir = this.camera.position.clone().sub(this.controls.target);
    if (dir.lengthSq() < 1e-3) dir.set(0, 0.4, 1);
    dir.normalize();
    dir.y = Math.max(dir.y, 0.25);
    dir.normalize();
    // Face the node from outside the structure so its cluster is in front of it.
    const outward = node.clone().setY(0);
    if (outward.lengthSq() > 1) { outward.normalize(); dir.lerp(outward, 0.35).normalize(); }
    // Nudge the framing so the node is not hidden behind the detail panel:
    // left of centre on wide screens, upper third on portrait screens.
    const up = new Vector3(0, 1, 0);
    const right = new Vector3().crossVectors(up, dir).normalize();
    const target = node.clone();
    if (portrait) target.addScaledVector(up, -dist * 0.28);
    else if (this.canvas.clientWidth > 900) target.addScaledVector(right, -dist * 0.16);
    this.flyTo(target.clone().addScaledVector(dir, dist), target, duration);
  }

  /** Default camera for a layout, pushed back on narrow (portrait) viewports so the whole structure fits. */
  private defaultCamera(id: LayoutId): { position: Vector3; target: Vector3 } {
    const meta = LAYOUTS.find((l) => l.id === id)!;
    const target = new Vector3(...meta.camera.target);
    const position = new Vector3(...meta.camera.position);
    const aspect = this.camera.aspect || 1.6;
    const fit = aspect < 1.3 ? Math.pow(1.3 / aspect, 0.75) : 1;
    position.sub(target).multiplyScalar(fit).add(target);
    return { position, target };
  }

  resetView(duration = 1.4) {
    const { position, target } = this.defaultCamera(this.store.state.layout);
    this.flyTo(position, target, duration);
  }

  /** Project a node's display position to CSS pixels (or null if behind the camera). */
  project(n: GraphNode): { x: number; y: number } | null {
    const v = new Vector3(this.posDisplay[n.index * 3], this.posDisplay[n.index * 3 + 1], this.posDisplay[n.index * 3 + 2]).project(this.camera);
    if (v.z > 1) return null;
    const r = this.canvas.getBoundingClientRect();
    return { x: (v.x * 0.5 + 0.5) * r.width + r.left, y: (-v.y * 0.5 + 0.5) * r.height + r.top };
  }

  // ── Input ─────────────────────────────────────────────────────────────────

  private bindPointer() {
    const c = this.canvas;
    let downX = 0, downY = 0, downT = 0;
    c.addEventListener('pointermove', (e) => {
      const r = c.getBoundingClientRect();
      this.pointer.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
      this.pointerClient = { x: e.clientX, y: e.clientY };
      this.pointerDirty = true;
    });
    c.addEventListener('pointerleave', () => { this.setHover(null); });
    c.addEventListener('pointerdown', (e) => { downX = e.clientX; downY = e.clientY; downT = performance.now(); });
    c.addEventListener('pointerup', (e) => {
      const moved = Math.hypot(e.clientX - downX, e.clientY - downY);
      if (moved > 6 || performance.now() - downT > 500) return;
      this.pick();
      this.events.onSelect(this.hovered);
    });
  }

  private setHover(n: GraphNode | null) {
    if (n === this.hovered) return;
    this.hovered = n;
    this.store.set({ hovered: n });
    this.events.onHover(n, this.pointerClient.x, this.pointerClient.y);
    this.canvas.style.cursor = n ? 'pointer' : '';
  }

  private pick() {
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hits = this.raycaster.intersectObject(this.nodes.mesh, false);
    let found: GraphNode | null = null;
    for (const h of hits) {
      const id = h.instanceId;
      if (id === undefined) continue;
      if (this.nodes.visCurrent[id] < 0.5) continue;
      found = this.graph.nodes[id];
      break;
    }
    this.setHover(found);
  }

  // ── Frame loop ────────────────────────────────────────────────────────────

  setFrameHook(fn: ((dt: number) => void) | null) { this.frameHook = fn; }

  /** Simulation clock in seconds (advances by at most 0.1 s per frame). */
  get simTime() { return this.time; }
  /** True when no layout transition or camera flight is in progress. */
  get settled() { return this.transitionT >= 1 && !this.flight; }

  resize() {
    const w = this.canvas.clientWidth || window.innerWidth;
    const h = this.canvas.clientHeight || window.innerHeight;
    this.renderer.setSize(w, h, false);
    this.composer.setSize(w, h);
    this.bloomPass.resolution.set(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    const ph = h * this.renderer.getPixelRatio();
    this.nodes.setHaloScale(ph, this.camera.fov);
    this.edges.setScale(ph, this.camera.fov);
    this.stars.setScale(ph, this.camera.fov);
  }

  stop() { this.running = false; }

  private frame() {
    if (!this.running) return;
    requestAnimationFrame(() => this.frame());
    const now = performance.now();
    const dt = Math.min(0.1, (now - this.lastFrame) / 1000);
    this.lastFrame = now;
    this.time += dt;
    const s = this.store.state;

    // Layout transition
    if (this.transitionT < 1) {
      this.transitionT = Math.min(1, this.transitionT + dt / this.transitionDur);
      const n = this.graph.nodes.length;
      for (let i = 0; i < n; i++) {
        const local = clamp((this.transitionT - this.stagger[i]) / (1 - 0.35), 0, 1);
        const e = easeInOutCubic(local);
        for (let c = 0; c < 3; c++) this.posCur[i * 3 + c] = this.posFrom[i * 3 + c] + (this.posTo[i * 3 + c] - this.posFrom[i * 3 + c]) * e;
      }
    }
    // Gentle breathing so the structure feels alive.
    const n = this.graph.nodes.length;
    for (let i = 0; i < n; i++) {
      const node = this.graph.nodes[i];
      const ph = this.stagger[i] * 40;
      const amp = node.tier === 'root' ? 0 : 0.18 + node.radius * 0.1;
      this.posDisplay[i * 3] = this.posCur[i * 3] + Math.sin(this.time * 0.5 + ph) * amp * 0.6;
      this.posDisplay[i * 3 + 1] = this.posCur[i * 3 + 1] + Math.sin(this.time * 0.7 + ph * 1.3) * amp;
      this.posDisplay[i * 3 + 2] = this.posCur[i * 3 + 2] + Math.cos(this.time * 0.45 + ph * 0.7) * amp * 0.6;
    }

    // Camera flight
    if (this.flight) {
      const f = this.flight;
      f.t = Math.min(1, f.t + dt / f.duration);
      const e = easeInOutCubic(f.t);
      this.camera.position.lerpVectors(f.fromPos, f.toPos, e);
      this.controls.target.lerpVectors(f.fromTarget, f.toTarget, e);
      if (f.t >= 1) this.flight = null;
    }
    this.controls.autoRotate = s.autoRotate && !this.flight && this.time - this.lastInteraction > 4;
    this.controls.update();

    if (this.pointerDirty && !this.flight) { this.pointerDirty = false; this.pick(); }
    if (this.hovered) this.events.onHover(this.hovered, this.pointerClient.x, this.pointerClient.y);

    this.nodes.update(this.posDisplay, dt, this.time);
    this.edges.update(this.posDisplay, this.nodes.colorCurrent, this.nodes.visCurrent, dt, this.time, s.flow);
    this.labels.update(this.posDisplay, this.nodes.visCurrent, this.nodes.emphCurrent, this.focus, !!s.selected, s.labelDensity, this.camera, this.canvas.clientHeight, dt);
    this.stars.update(this.time);
    this.guides.update(dt, this.camera.quaternion);
    this.frameHook?.(dt);

    if (s.bloom) this.composer.render();
    else this.renderer.render(this.scene, this.camera);
  }
}
