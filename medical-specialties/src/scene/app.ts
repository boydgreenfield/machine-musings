import {
  Color,
  Fog,
  PerspectiveCamera,
  Scene,
  Vector2,
  Vector3,
  WebGLRenderer,
  NoToneMapping,
  SRGBColorSpace,
} from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js';
import { CSS2DRenderer } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import type { Graph } from '../data/graph';
import { subtree } from '../data/graph';
import type { SpecialtyNode } from '../data/types';
import { computeLayout, LAYOUT_BY_ID, type LayoutId, type Positions, systemClusters, timelineLanes, timelineTicks } from '../graph/layouts';
import type { Store } from '../state';
import { clamp, damp, easeInOutCubic, easeOutBack, easeOutQuint, TweenManager } from '../util/tween';
import { EdgeLines } from './edges';
import { NodeLabels } from './labels';
import { NodeCloud } from './nodes';
import { createStarfield } from './starfield';
import { SelectionRing } from './ring';
import { baseRadius } from './nodes';
import { TimelineAxis } from './axis';
import { SunburstGuides } from './guides';

export interface SceneAppOptions {
  container: HTMLElement;
  labelContainer: HTMLElement;
  /** Reduced-motion preference: skip intro and shorten transitions. */
  reducedMotion?: boolean;
}

/** Everything WebGL: owns the scene graph and the render loop. */
export class SceneApp {
  readonly renderer: WebGLRenderer;
  readonly labelRenderer: CSS2DRenderer;
  readonly scene = new Scene();
  readonly camera: PerspectiveCamera;
  readonly controls: OrbitControls;
  readonly composer: EffectComposer;
  readonly bloom: UnrealBloomPass;
  readonly cloud: NodeCloud;
  readonly edges: EdgeLines;
  readonly labels: NodeLabels;
  readonly axis: TimelineAxis;
  readonly guides: SunburstGuides;
  readonly ring = new SelectionRing();
  readonly tweens = new TweenManager();

  private readonly graph: Graph;
  private readonly store: Store;
  private readonly container: HTMLElement;
  private layoutTargets: Positions;
  private layoutFrom: Positions;
  private layoutProgress = 1;
  private introProgress = 0;
  private lastInteraction = performance.now();
  private pointer = new Vector2(-10, -10);
  private pointerDown = new Vector2();
  private pointerMoved = false;
  private cameraTween: { from: Vector3; to: Vector3; targetFrom: Vector3; targetTo: Vector3 } | null = null;
  private frame = 0;
  private running = true;
  private readonly reducedMotion: boolean;
  private resizeObserver: ResizeObserver;
  private readonly tmpV = new Vector3();
  private idleRotate = true;
  private needsVisualRefresh = true;

  constructor(graph: Graph, store: Store, opts: SceneAppOptions) {
    this.graph = graph;
    this.store = store;
    this.container = opts.container;
    this.reducedMotion = opts.reducedMotion ?? false;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.renderer = new WebGLRenderer({ antialias: true, alpha: false, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(dpr);
    this.renderer.outputColorSpace = SRGBColorSpace;
    this.renderer.toneMapping = NoToneMapping;
    this.renderer.setClearColor(new Color('#070a14'), 1);
    this.renderer.domElement.className = 'gl';
    this.container.appendChild(this.renderer.domElement);

    this.labelRenderer = new CSS2DRenderer({ element: opts.labelContainer });

    this.scene.fog = new Fog(new Color('#070a14'), 120, 330);

    this.camera = new PerspectiveCamera(48, 1, 0.5, 1200);
    const cam = LAYOUT_BY_ID[store.state.layout].camera;
    this.camera.position.set(cam[0], cam[1], cam[2]).multiplyScalar(this.reducedMotion ? 1 : 2.6);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.06;
    this.controls.rotateSpeed = 0.55;
    this.controls.zoomSpeed = 0.8;
    this.controls.minDistance = 10;
    this.controls.maxDistance = 420;
    this.controls.enablePan = true;
    this.controls.panSpeed = 0.6;
    this.controls.autoRotate = false;
    this.controls.autoRotateSpeed = 0.35;
    this.controls.addEventListener('start', () => this.noteInteraction());

    this.scene.add(createStarfield());
    this.cloud = new NodeCloud(graph);
    this.edges = new EdgeLines(graph, this.cloud);
    this.labels = new NodeLabels(graph, this.cloud);
    this.axis = new TimelineAxis(timelineTicks(graph), timelineLanes());
    this.labels.setClusters(systemClusters(graph));
    this.guides = new SunburstGuides();
    this.scene.add(this.guides, this.edges, this.cloud, this.labels, this.axis, this.ring);

    this.composer = new EffectComposer(this.renderer);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    this.bloom = new UnrealBloomPass(new Vector2(1, 1), 0.85, 0.55, 0.18);
    this.composer.addPass(this.bloom);
    this.composer.addPass(new OutputPass());

    this.layoutTargets = computeLayout(graph, store.state.layout);
    this.layoutFrom = this.layoutTargets;
    this.cloud.nodes.forEach((n, i) => this.cloud.positions[i]!.copy(this.layoutTargets.get(n.id)!));
    this.edges.updatePositions();

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(this.container);
    this.resize();
    this.bindPointer();

    store.on('change', () => (this.needsVisualRefresh = true));
    store.on('layout', (s) => this.transitionTo(s.layout));
    document.body.dataset.layout = store.state.layout;
    store.on('select', (s) => {
      this.needsVisualRefresh = true;
      if (s.selected) this.focus(s.selected);
    });

    document.addEventListener('visibilitychange', () => {
      this.running = document.visibilityState !== 'hidden';
      if (this.running) this.lastFrameTime = performance.now();
    });

    this.playIntro();
    this.lastFrameTime = performance.now();
    requestAnimationFrame(this.loop);
  }

  private lastFrameTime = 0;

  /** Intro: camera dollies in while nodes bloom outward from the root, level by level. */
  private playIntro(): void {
    const now = performance.now();
    if (this.reducedMotion) {
      this.cloud.scales.fill(1);
      this.introProgress = 1;
      return;
    }
    const cam = LAYOUT_BY_ID[this.store.state.layout].camera;
    const from = this.camera.position.clone();
    const to = new Vector3(cam[0], cam[1], cam[2]);
    this.tweens.add(
      {
        duration: 2600,
        easing: easeOutQuint,
        onUpdate: (t) => {
          this.camera.position.lerpVectors(from, to, t);
          this.introProgress = t;
        },
        onComplete: () => (this.introProgress = 1),
      },
      now,
    );
    this.cloud.nodes.forEach((n, i) => {
      const delay = 150 + n.depth * 330 + (n.depth > 0 ? (i % 11) * 22 : 0);
      this.tweens.add(
        {
          duration: 900,
          delay,
          easing: easeOutBack,
          onUpdate: (t) => {
            this.cloud.scales[i] = t;
          },
        },
        now,
      );
    });
  }

  private bindPointer(): void {
    const el = this.renderer.domElement;
    el.addEventListener('pointermove', (ev) => {
      const rect = el.getBoundingClientRect();
      this.pointer.set(ev.clientX - rect.left, ev.clientY - rect.top);
      if (ev.buttons) {
        if (this.pointer.distanceTo(this.pointerDown) > 6) this.pointerMoved = true;
      }
    });
    el.addEventListener('pointerdown', (ev) => {
      const rect = el.getBoundingClientRect();
      this.pointerDown.set(ev.clientX - rect.left, ev.clientY - rect.top);
      this.pointer.copy(this.pointerDown);
      this.pointerMoved = false;
      this.noteInteraction();
    });
    el.addEventListener('pointerup', (ev) => {
      if (this.pointerMoved) return;
      if (ev.button !== 0) return;
      const rect = el.getBoundingClientRect();
      this.pointer.set(ev.clientX - rect.left, ev.clientY - rect.top);
      const hit = this.pick(this.pointer, true);
      this.store.select(hit ? hit.id : null);
    });
    el.addEventListener('pointerleave', () => {
      this.pointer.set(-10, -10);
      this.store.setHovered(null);
    });
    el.addEventListener('wheel', () => this.noteInteraction(), { passive: true });
  }

  private noteInteraction(): void {
    this.lastInteraction = performance.now();
    this.controls.autoRotate = false;
  }

  /** Enable or disable idle auto-rotation. */
  setIdleRotate(v: boolean): void {
    this.idleRotate = v;
    if (!v) this.controls.autoRotate = false;
  }

  resize(): void {
    const w = Math.max(1, this.container.clientWidth);
    const h = Math.max(1, this.container.clientHeight);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h, false);
    this.renderer.domElement.style.width = '100%';
    this.renderer.domElement.style.height = '100%';
    this.composer.setSize(w, h);
    this.bloom.resolution.set(w, h);
    this.labelRenderer.setSize(w, h);
    this.labels.setViewport(w, h);
    const dpr = this.renderer.getPixelRatio();
    this.edges.setResolution(w * dpr, h * dpr);
  }

  /** Screen-space picking: nearest visible node within a generous radius. */
  pick(px: Vector2, forClick = false): SpecialtyNode | null {
    const w = this.container.clientWidth;
    const h = this.container.clientHeight;
    const visuals = this.lastVisuals;
    if (!visuals) return null;
    let best: SpecialtyNode | null = null;
    let bestScore = Infinity;
    this.cloud.nodes.forEach((n, i) => {
      const v = visuals.get(n.id)!;
      if (!v.visible || v.emphasis <= 0.2 && !forClick) return;
      if (!v.visible) return;
      this.tmpV.copy(this.cloud.positions[i]!).project(this.camera);
      if (this.tmpV.z > 1 || this.tmpV.z < -1) return;
      const sx = ((this.tmpV.x + 1) / 2) * w;
      const sy = ((1 - this.tmpV.y) / 2) * h;
      const dx = sx - px.x;
      const dy = sy - px.y;
      const d = Math.hypot(dx, dy);
      const radius = n.level === 'subspecialty' ? 13 : n.level === 'specialty' ? 17 : 22;
      if (d > radius) return;
      // Prefer closer-to-camera nodes when overlapping.
      const score = d + this.tmpV.z * 10;
      if (score < bestScore) {
        bestScore = score;
        best = n;
      }
    });
    return best;
  }

  private lastVisuals: Map<string, import('../state').NodeVisual> | null = null;

  refreshVisuals(): void {
    const visuals = this.store.computeVisuals();
    this.lastVisuals = visuals;
    const s = this.store.state;
    this.cloud.applyVisuals(visuals);
    this.edges.applyVisuals(visuals, s.showCross, s.selected, s.hovered);
    this.labels.applyVisuals(visuals, s.labels, LAYOUT_BY_ID[s.layout].showDomains);
    this.axis.visible = s.layout === 'timeline' && this.layoutProgress > 0.5;
    this.guides.visible = s.layout === 'sunburst' && this.layoutProgress > 0.5;
    this.labels.showClusters = s.layout === 'systems' && this.layoutProgress > 0.5;
    this.needsVisualRefresh = false;
  }

  /** Animate all nodes to a new layout and swing the camera to its preferred view. */
  transitionTo(layout: LayoutId): void {
    const meta = LAYOUT_BY_ID[layout];
    document.body.dataset.layout = layout;
    this.layoutFrom = new Map(this.cloud.nodes.map((n, i) => [n.id, this.cloud.positions[i]!.clone()]));
    this.layoutTargets = computeLayout(this.graph, layout);
    this.layoutProgress = 0;
    this.edges.setLayout(layout);
    const duration = this.reducedMotion ? 300 : 1500;
    this.tweens.add({
      duration,
      easing: easeInOutCubic,
      onUpdate: (t) => {
        if (this.layoutProgress < 0.5 && t >= 0.5) this.needsVisualRefresh = true;
        this.layoutProgress = t;
        this.cloud.nodes.forEach((n, i) => {
          const a = this.layoutFrom.get(n.id)!;
          const b = this.layoutTargets.get(n.id)!;
          // Deeper nodes trail their parents slightly for a cascading feel.
          const lag = Math.min(0.25, n.depth * 0.06);
          const tt = clamp((t - lag) / (1 - lag), 0, 1);
          const e = easeInOutCubic(tt);
          this.cloud.positions[i]!.lerpVectors(a, b, e);
        });
        this.edges.updatePositions();
      },
      onComplete: () => {
        this.layoutProgress = 1;
        this.needsVisualRefresh = true;
      },
    });
    this.needsVisualRefresh = true;
    const sel = this.store.state.selected;
    if (sel) this.focus(sel, this.layoutTargets.get(sel));
    else this.flyTo(new Vector3(meta.camera[0], meta.camera[1], meta.camera[2]), new Vector3(), duration + 200);
  }

  /** Move the camera so that `id` sits at the orbit target at a level-appropriate distance. */
  focus(id: string, targetOverride?: Vector3): void {
    const node = this.graph.byId.get(id);
    if (!node) return;
    const target = (targetOverride ?? this.layoutTargets.get(id) ?? this.cloud.positionOf(id))!.clone();
    // Frame the node together with its home subtree and direct neighbours.
    let extent = 0;
    const around = subtree(this.graph, id);
    if (node.parents[0]) around.add(node.parents[0]);
    for (const other of around) {
      const p = this.layoutTargets.get(other);
      if (p) extent = Math.max(extent, p.distanceTo(target));
    }
    const base = node.level === 'root' ? 175 : node.level === 'domain' ? 92 : node.level === 'specialty' ? 56 : 40;
    const distance = clamp(extent * 2.4 + 18, base, 175);
    // Keep the current viewing direction, approaching from slightly outside the
    // node so its children stay in frame.
    const dir = this.camera.position.clone().sub(this.controls.target);
    if (dir.lengthSq() < 1e-3) dir.set(0, 0.3, 1);
    dir.normalize();
    const outward = target.lengthSq() > 1 ? target.clone().normalize() : new Vector3(0, 0.2, 1);
    dir.lerp(outward, node.level === 'root' ? 0 : 0.45).normalize();
    // Keep a comfortable elevation so the oblate tree is never seen edge-on.
    const minY = this.store.state.layout === 'sunburst' ? 0.85 : 0.42;
    if (dir.y < minY) {
      dir.y = minY;
      dir.normalize();
    }
    const camPos = target.clone().addScaledVector(dir, distance);
    // With the detail panel open on wide screens, shift the view so the node
    // sits left of centre instead of underneath the panel.
    if (this.container.clientWidth > 860 && node.level !== 'root') {
      // `dir` points from the node toward the camera, so camera-right is up × dir.
      const right = new Vector3().crossVectors(this.camera.up, dir).normalize();
      const shift = right.multiplyScalar(distance * 0.22);
      target.add(shift);
      camPos.add(shift);
    }
    this.flyTo(camPos, target, this.reducedMotion ? 250 : 1100);
  }

  /** Reset to the layout's default view. */
  resetView(): void {
    const cam = LAYOUT_BY_ID[this.store.state.layout].camera;
    this.flyTo(new Vector3(cam[0], cam[1], cam[2]), new Vector3(), this.reducedMotion ? 250 : 1100);
  }

  private flyTo(position: Vector3, target: Vector3, duration: number): void {
    const from = this.camera.position.clone();
    const targetFrom = this.controls.target.clone();
    this.cameraTween = { from, to: position, targetFrom, targetTo: target };
    this.tweens.add({
      duration,
      easing: easeInOutCubic,
      onUpdate: (t) => {
        if (!this.cameraTween) return;
        this.camera.position.lerpVectors(this.cameraTween.from, this.cameraTween.to, t);
        this.controls.target.lerpVectors(this.cameraTween.targetFrom, this.cameraTween.targetTo, t);
      },
      onComplete: () => (this.cameraTween = null),
    });
  }

  private hoverAccumulator = 0;

  private loop = (now: number): void => {
    requestAnimationFrame(this.loop);
    if (!this.running) return;
    const dt = Math.min(0.1, (now - this.lastFrameTime) / 1000);
    this.lastFrameTime = now;
    this.frame++;
    this.tweens.update(now);

    // Idle auto-rotate after a quiet spell, only in free orbit.
    if (this.idleRotate && !this.cameraTween && now - this.lastInteraction > 9000 && this.introProgress >= 1) {
      this.controls.autoRotate = true;
    }
    this.controls.update();

    // Hover picking a few times per second is plenty.
    this.hoverAccumulator += dt;
    if (this.hoverAccumulator > 0.05 && this.pointer.x >= 0 && this.layoutProgress >= 1) {
      this.hoverAccumulator = 0;
      const hit = this.pick(this.pointer);
      const id = hit ? hit.id : null;
      if (id !== this.store.state.hovered) {
        this.store.setHovered(id);
        this.needsVisualRefresh = true;
      }
      this.renderer.domElement.style.cursor = hit ? 'pointer' : 'grab';
    }
    if (this.needsVisualRefresh) this.refreshVisuals();

    // Nodes keep slowly relaxing their scale back to 1 after hover pulses.
    for (let i = 0; i < this.cloud.scales.length; i++) {
      if (this.introProgress >= 1 && this.cloud.scales[i]! !== 1) {
        this.cloud.scales[i] = damp(this.cloud.scales[i]!, 1, 6, dt);
      }
    }

    const sel = this.store.state.selected;
    if (sel) {
      const p = this.cloud.positionOf(sel);
      const n = this.graph.byId.get(sel);
      if (p && n) {
        this.ring.position.copy(p);
        this.ring.show(baseRadius(n) * (n.level === 'root' ? 1.2 : 1.6));
      }
    } else this.ring.hide();
    this.ring.update(now / 1000);

    const cameraDistance = this.camera.position.distanceTo(this.controls.target);
    // Bloom and halos are tamed as the camera closes in so clusters do not overexpose.
    const closeness = clamp(cameraDistance / 150, 0.4, 1);
    this.bloom.strength = 0.85 * closeness;
    this.cloud.haloScale = 0.45 + 0.55 * closeness;
    this.cloud.update(this.camera.quaternion, now / 1000);
    if (this.cameraTween || this.layoutProgress < 1) this.edges.updatePositions();
    this.labels.update(this.camera, cameraDistance, this.introProgress, dt);
    this.axis.update();

    const star = this.scene.getObjectByName('starfield');
    if (star) star.rotation.y += dt * 0.004;

    this.composer.render();
    this.labelRenderer.render(this.scene, this.camera);
  };

  /** Camera distance to orbit target, used by UI for zoom indicators. */
  get cameraDistance(): number {
    return this.camera.position.distanceTo(this.controls.target);
  }

  dispose(): void {
    this.running = false;
    this.resizeObserver.disconnect();
    this.renderer.dispose();
  }
}
