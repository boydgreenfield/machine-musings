/**
 * `functions.jsonl` rows: the regions of `regions.ts` with formatting-insensitive hashes and the
 * declared types of their parameters (through a TypeScript program over the measured files and
 * the project's `tsconfig.json`).
 *
 * `body_hash` is the sha256 of the body's tokens joined by single spaces (comments and layout do
 * not count); for a class or the module, the bodies of the functions and classes directly inside
 * are left out (as pytest-coverset replaces nested definitions with `pass`), so editing a method
 * changes the method's hash, not its class's. `sig_hash` hashes the tokens from the start of the
 * declaration to its body: modifiers, decorators, name, parameters with their types, return type
 * (a class's heritage clauses).
 */
import { createHash } from "node:crypto";
import { dirname, resolve } from "node:path";

import ts from "typescript";

import { type DescribeContext, type Descriptor, paramDescriptor, returnDescriptor } from "./descriptors.js";
import { moduleOf } from "./recorder.js";
import { indexRegions, type Region } from "./regions.js";

export interface FunctionRow {
  path: string;
  qualname: string;
  ordinal: number;
  def_line: number;
  body_start: number;
  end_line: number;
  body_hash: string;
  sig_hash: string;
  params: string[];
  kind: string;
  types?: Record<string, Descriptor | null>;
}

function sha256(text: string): string {
  return createHash("sha256").update(text).digest("hex");
}

function bodyRange(region: Region, sf: ts.SourceFile): [number, number] {
  if (region.fn?.body !== undefined) return [region.fn.body.getStart(sf), region.fn.body.end];
  if (ts.isClassLike(region.node)) return [region.node.members.pos, region.node.members.end];
  return [0, sf.end];
}

function tokens(sf: ts.SourceFile, start: number, end: number, masks: [number, number][]): string {
  const scanner = ts.createScanner(
    ts.ScriptTarget.Latest,
    true,
    sf.languageVariant,
    sf.text,
    undefined,
    start,
    Math.max(0, end - start),
  );
  const out: string[] = [];
  let masked = -1;
  for (let kind = scanner.scan(); kind !== ts.SyntaxKind.EndOfFileToken; kind = scanner.scan()) {
    const pos = scanner.getTokenStart();
    const mask = masks.findIndex(([a, b]) => pos >= a && pos < b);
    if (mask >= 0) {
      if (mask !== masked) out.push("{…}");
      masked = mask;
      continue;
    }
    masked = -1;
    out.push(scanner.getTokenText());
  }
  return out.join(" ");
}

/** Hashes of a region: its body (nested definitions masked for classes and the module) and signature. */
export function regionHashes(region: Region, regions: Region[], sf: ts.SourceFile): { body: string; sig: string } {
  const [start, end] = bodyRange(region, sf);
  const masks: [number, number][] =
    region.kind === "class" || region.kind === "module"
      ? regions.filter((r) => r.parent === region && r !== region).map((r) => bodyRange(r, sf))
      : [];
  const body = tokens(sf, start, end, masks);
  const declStart = region.kind === "module" ? 0 : region.declNode.getStart(sf);
  const sig = region.kind === "module" ? "" : tokens(sf, declStart, Math.max(declStart, start), []);
  return { body: sha256(body), sig: sha256(sig) };
}

/** A program over `files` with the compiler options of the `tsconfig.json` nearest `projectRoot`. */
export function createProgram(files: string[], projectRoot: string): ts.Program {
  let options: ts.CompilerOptions = {
    strict: true,
    target: ts.ScriptTarget.ESNext,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    allowJs: true,
    skipLibCheck: true,
  };
  const configPath = ts.findConfigFile(projectRoot, ts.sys.fileExists, "tsconfig.json");
  if (configPath !== undefined) {
    const read = ts.readConfigFile(configPath, ts.sys.readFile);
    if (read.error === undefined) {
      const parsed = ts.parseJsonConfigFileContent(read.config, ts.sys, dirname(configPath));
      options = { ...parsed.options, allowJs: true };
    }
  }
  return ts.createProgram({ rootNames: files.map((f) => resolve(f)), options: { ...options, noEmit: true } });
}

/**
 * The `functions.jsonl` rows of one file. `relOf` maps a declaration's file to its repository
 * path (undefined outside the repository), for `object` descriptors named like `obj:` tokens.
 */
export function functionRows(
  sf: ts.SourceFile,
  path: string,
  checker: ts.TypeChecker | undefined,
  relOf: (fileName: string) => string | undefined,
): FunctionRow[] {
  const regions = indexRegions(sf);
  const ctx: DescribeContext | undefined =
    checker === undefined
      ? undefined
      : {
          checker,
          qualify: (symbol) => {
            const decl = symbol.declarations?.[0];
            const rel = decl === undefined ? undefined : relOf(decl.getSourceFile().fileName);
            return rel === undefined ? undefined : `${moduleOf(rel)}.${symbol.getName()}`;
          },
        };
  return regions.map((region) => {
    const hashes = regionHashes(region, regions, sf);
    const row: FunctionRow = {
      path,
      qualname: region.qualname,
      ordinal: region.ordinal,
      def_line: region.defLine,
      body_start: region.bodyStart,
      end_line: region.endLine,
      body_hash: hashes.body,
      sig_hash: hashes.sig,
      params: region.params.map((p) => p.label),
      kind: region.kind,
    };
    if (ctx !== undefined && region.fn !== undefined) {
      const types: Record<string, Descriptor | null> = {};
      for (const p of region.params) {
        try {
          types[p.label] = paramDescriptor(ctx, p.node);
        } catch {
          types[p.label] = null;
        }
      }
      if (!region.isConstructor) {
        try {
          types["return"] = returnDescriptor(ctx, region.fn, region.isAsync);
        } catch {
          types["return"] = null;
        }
      }
      row.types = types;
    }
    return row;
  });
}
