declare module 'troika-three-text' {
  import { Mesh, Color, Material } from 'three';
  export class Text extends Mesh {
    text: string;
    anchorX: number | string;
    anchorY: number | string;
    font: string | null;
    fontSize: number;
    fontWeight: string | number;
    letterSpacing: number;
    lineHeight: number | string;
    maxWidth: number;
    textAlign: 'left' | 'right' | 'center' | 'justify';
    color: string | number | Color;
    fillOpacity: number;
    outlineWidth: number | string;
    outlineColor: string | number | Color;
    outlineOpacity: number;
    outlineBlur: number | string;
    strokeWidth: number | string;
    depthOffset: number;
    sdfGlyphSize: number;
    material: Material;
    textRenderInfo: { blockBounds: [number, number, number, number] } | null;
    sync(callback?: () => void): void;
    dispose(): void;
  }
  export function preloadFont(options: { font?: string; characters?: string; sdfGlyphSize?: number }, callback: () => void): void;
  export function configureTextBuilder(config: { useWorker?: boolean; sdfGlyphSize?: number }): void;
}
