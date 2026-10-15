/**
 * Function regions of a TypeScript or JavaScript source file, shared by the instrumentation
 * plugin (which needs the same qualnames and first lines the shape rows carry) and the reporter
 * (which writes them to `functions.jsonl`).
 *
 * Rules (one convention for this producer):
 *
 * * a region per function declaration, function expression, arrow function, method, accessor,
 *   constructor and class with a body, plus one `<module>` region per file; overload signatures,
 *   `declare` and abstract members (no body) are not regions;
 * * `qualname` joins the enclosing regions' names with `.`: `Cart.addItem`, `outer.inner`,
 *   `rateFor.<anonymous>`; a function expression or arrow takes the name it is bound to (a
 *   variable, a class field, an object property, `export default`), else its own name, else
 *   `<anonymous>`; `const api = { get() {} }` gives `api.get` and `{ nested: { put() {} } }`
 *   `api.nested.put` (a quoted key's blanks and dots become `_`); a `namespace N` adds `N`;
 * * `ordinal` numbers same-qualname regions of the file in source order (get/set pairs, two
 *   anonymous callbacks of one function);
 * * `def_line` is the first line of the declaration (decorators and `export` included; for a
 *   function bound to a variable, the `const` line); `body_start` the first statement of the body
 *   (a leading comment or JSDoc is not a statement), the line of `{` for an empty body, the
 *   expression's first line for an arrow with an expression body, the first member of a class;
 *   `end_line` the closing brace;
 * * `kind` is `method` for class members (methods, accessors, constructors, arrow functions in
 *   class fields), `class`, `module`, else `function`.
 */
import ts from "typescript";

export const MODULE_QUALNAME = "<module>";
export const ANONYMOUS = "<anonymous>";

export type RegionKind = "function" | "method" | "class" | "module";

export interface ParamInfo {
  /** The name written to `params`/`types` and to shape rows: `x`, `...rest`, `arg1` for a pattern. */
  label: string;
  /** Position among the declared parameters (`this` excluded). */
  index: number;
  kind: "ident" | "rest" | "pattern";
  /** The identifier to read the value from (ident and rest). */
  ident?: string;
  node: ts.ParameterDeclaration;
}

export interface Region {
  qualname: string;
  ordinal: number;
  kind: RegionKind;
  defLine: number;
  bodyStart: number;
  endLine: number;
  params: ParamInfo[];
  /** The function-like node, class or source file. */
  node: ts.Node;
  /** For function-like regions: the node with the body. */
  fn?: ts.FunctionLikeDeclaration;
  /** The node the declaration starts at (`export const f = ...` for a bound arrow). */
  declNode: ts.Node;
  parent: Region | undefined;
  /** For class members: the class's qualname. */
  classQualname?: string;
  isArrow: boolean;
  isAsync: boolean;
  isGenerator: boolean;
  isConstructor: boolean;
  isStatic: boolean;
  /** A class member whose `this` is the instance (or the class, when static). */
  hasReceiver: boolean;
}

function line(sf: ts.SourceFile, pos: number): number {
  return sf.getLineAndCharacterOfPosition(pos).line + 1;
}

export function propertyNameText(name: ts.PropertyName | ts.BindingName | undefined, sf: ts.SourceFile): string {
  if (name === undefined) return ANONYMOUS;
  if (ts.isIdentifier(name) || ts.isPrivateIdentifier(name)) return name.text;
  if (ts.isStringLiteral(name) || ts.isNumericLiteral(name) || ts.isNoSubstitutionTemplateLiteral(name)) {
    // A quoted key may hold what separates qualname parts (`"a.b"`) or blanks: `_` instead.
    const text = name.text.replace(/[\s.]+/gu, "_");
    return text.length ? text : ANONYMOUS;
  }
  if (ts.isComputedPropertyName(name)) return "[" + name.expression.getText(sf).replace(/\s+/g, "") + "]";
  return ANONYMOUS;
}

function hasModifier(node: ts.Node, kind: ts.SyntaxKind): boolean {
  return ts.canHaveModifiers(node) && (ts.getModifiers(node) ?? []).some((m) => m.kind === kind);
}

/** Skip wrappers that do not change what a function expression is bound to. */
function bindingParent(node: ts.Node): ts.Node {
  let parent = node.parent;
  while (
    parent !== undefined &&
    (ts.isParenthesizedExpression(parent) ||
      ts.isAsExpression(parent) ||
      ts.isSatisfiesExpression(parent) ||
      ts.isTypeAssertionExpression(parent) ||
      ts.isNonNullExpression(parent))
  ) {
    parent = parent.parent;
  }
  return parent;
}

interface Binding {
  name: string;
  declNode: ts.Node;
  classMember?: ts.PropertyDeclaration;
}

function bindingOf(node: ts.Node, sf: ts.SourceFile): Binding | undefined {
  const parent = bindingParent(node);
  if (parent === undefined) return undefined;
  if (ts.isVariableDeclaration(parent) && ts.isIdentifier(parent.name)) {
    const list = parent.parent;
    const single = ts.isVariableDeclarationList(list) && ts.isVariableStatement(list.parent) && list.declarations.length === 1;
    return { name: parent.name.text, declNode: single ? list.parent : parent };
  }
  if (ts.isPropertyDeclaration(parent)) {
    return { name: propertyNameText(parent.name, sf), declNode: parent, classMember: parent };
  }
  if (ts.isPropertyAssignment(parent)) return { name: propertyNameText(parent.name, sf), declNode: parent };
  if (ts.isExportAssignment(parent)) return { name: "default", declNode: parent };
  if (
    ts.isBinaryExpression(parent) &&
    parent.operatorToken.kind === ts.SyntaxKind.EqualsToken &&
    ts.isIdentifier(parent.left)
  ) {
    return { name: parent.left.text, declNode: parent };
  }
  return undefined;
}

function functionName(node: ts.FunctionLikeDeclaration, sf: ts.SourceFile): { name: string; declNode: ts.Node; classMember?: ts.PropertyDeclaration } {
  if (ts.isConstructorDeclaration(node)) return { name: "constructor", declNode: node };
  if (ts.isMethodDeclaration(node) || ts.isGetAccessorDeclaration(node) || ts.isSetAccessorDeclaration(node)) {
    return { name: propertyNameText(node.name, sf), declNode: node };
  }
  if (ts.isFunctionDeclaration(node)) {
    const name = node.name?.text ?? (hasModifier(node, ts.SyntaxKind.DefaultKeyword) ? "default" : ANONYMOUS);
    return { name, declNode: node };
  }
  const binding = bindingOf(node, sf);
  if (binding !== undefined) return binding;
  if (ts.isFunctionExpression(node) && node.name !== undefined) return { name: node.name.text, declNode: node };
  return { name: ANONYMOUS, declNode: node };
}

function className(node: ts.ClassLikeDeclaration, sf: ts.SourceFile): { name: string; declNode: ts.Node } {
  if (node.name !== undefined) return { name: node.name.text, declNode: node };
  if (ts.isClassDeclaration(node)) {
    return { name: hasModifier(node, ts.SyntaxKind.DefaultKeyword) ? "default" : ANONYMOUS, declNode: node };
  }
  return bindingOf(node, sf) ?? { name: ANONYMOUS, declNode: node };
}

function paramsOf(fn: ts.FunctionLikeDeclaration): ParamInfo[] {
  const out: ParamInfo[] = [];
  let index = 0;
  for (const p of fn.parameters) {
    if (ts.isIdentifier(p.name) && p.name.text === "this") continue;
    if (ts.isIdentifier(p.name)) {
      const rest = p.dotDotDotToken !== undefined;
      out.push({ label: rest ? "..." + p.name.text : p.name.text, index, kind: rest ? "rest" : "ident", ident: p.name.text, node: p });
    } else {
      out.push({ label: `arg${index}`, index, kind: "pattern", node: p });
    }
    index++;
  }
  return out;
}

function isFunctionLike(node: ts.Node): node is ts.FunctionLikeDeclaration {
  return (
    ts.isFunctionDeclaration(node) ||
    ts.isFunctionExpression(node) ||
    ts.isArrowFunction(node) ||
    ts.isMethodDeclaration(node) ||
    ts.isConstructorDeclaration(node) ||
    ts.isGetAccessorDeclaration(node) ||
    ts.isSetAccessorDeclaration(node)
  );
}

/** The function-like regions and classes of `sf`, plus `<module>`, in source order. */
export function indexRegions(sf: ts.SourceFile): Region[] {
  const regions: Region[] = [];
  const ordinals = new Map<string, number>();
  const lastLine = sf.statements.length ? line(sf, sf.statements[sf.statements.length - 1]!.end) : 1;
  const firstStatement = sf.statements.length ? line(sf, sf.statements[0]!.getStart(sf)) : 1;
  const moduleRegion: Region = {
    qualname: MODULE_QUALNAME,
    ordinal: 0,
    kind: "module",
    defLine: 1,
    bodyStart: firstStatement,
    endLine: Math.max(lastLine, firstStatement),
    params: [],
    node: sf,
    declNode: sf,
    parent: undefined,
    isArrow: false,
    isAsync: false,
    isGenerator: false,
    isConstructor: false,
    isStatic: false,
    hasReceiver: false,
  };
  regions.push(moduleRegion);

  const add = (region: Omit<Region, "ordinal">): Region => {
    const ordinal = ordinals.get(region.qualname) ?? 0;
    ordinals.set(region.qualname, ordinal + 1);
    const full = { ...region, ordinal };
    full.bodyStart = Math.max(full.defLine, full.bodyStart);
    full.endLine = Math.max(full.bodyStart, full.endLine);
    regions.push(full);
    return full;
  };

  const visit = (node: ts.Node, scope: string[], parent: Region, classQualname: string | undefined): void => {
    if (isFunctionLike(node) && node.body !== undefined) {
      const { name, declNode, classMember } = functionName(node, sf);
      const qualname = [...scope, name].join(".");
      const inClass = node.parent !== undefined && ts.isClassLike(node.parent);
      const isMember = inClass || classMember !== undefined;
      const isArrow = ts.isArrowFunction(node);
      const isStatic = hasModifier(classMember ?? node, ts.SyntaxKind.StaticKeyword);
      const body = node.body;
      let bodyStart: number;
      if (ts.isBlock(body)) {
        bodyStart = body.statements.length ? line(sf, body.statements[0]!.getStart(sf)) : line(sf, body.getStart(sf));
      } else {
        bodyStart = line(sf, body.getStart(sf));
      }
      const region = add({
        qualname,
        kind: isMember ? "method" : "function",
        defLine: line(sf, declNode.getStart(sf)),
        bodyStart,
        endLine: line(sf, node.end),
        params: paramsOf(node),
        node,
        fn: node,
        declNode,
        parent,
        classQualname: isMember ? classQualname : undefined,
        isArrow,
        isAsync: hasModifier(node, ts.SyntaxKind.AsyncKeyword),
        isGenerator: "asteriskToken" in node && node.asteriskToken !== undefined,
        isConstructor: ts.isConstructorDeclaration(node),
        isStatic,
        hasReceiver: isMember && (!isArrow || classMember !== undefined),
      });
      ts.forEachChild(node, (child) => visit(child, [...scope, name], region, undefined));
      return;
    }
    if (ts.isClassLike(node)) {
      const { name, declNode } = className(node, sf);
      const qualname = [...scope, name].join(".");
      const members = node.members.filter((m) => !ts.isSemicolonClassElement(m));
      const region = add({
        qualname,
        kind: "class",
        defLine: line(sf, declNode.getStart(sf)),
        bodyStart: members.length ? line(sf, members[0]!.getStart(sf)) : line(sf, node.getStart(sf)),
        endLine: line(sf, node.end),
        params: [],
        node,
        declNode,
        parent,
        isArrow: false,
        isAsync: false,
        isGenerator: false,
        isConstructor: false,
        isStatic: false,
        hasReceiver: false,
      });
      ts.forEachChild(node, (child) => visit(child, [...scope, name], region, qualname));
      return;
    }
    if (ts.isModuleDeclaration(node) && ts.isIdentifier(node.name)) {
      ts.forEachChild(node, (child) => visit(child, [...scope, node.name.getText(sf)], parent, classQualname));
      return;
    }
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.initializer !== undefined &&
      ts.isObjectLiteralExpression(node.initializer)
    ) {
      const name = node.name.text;
      ts.forEachChild(node.initializer, (child) => visit(child, [...scope, name], parent, classQualname));
      return;
    }
    // `nested: { put() {} }` inside an object literal: `api.nested.put`, every key kept.
    if (
      ts.isPropertyAssignment(node) &&
      ts.isObjectLiteralExpression(node.parent) &&
      ts.isObjectLiteralExpression(node.initializer)
    ) {
      const name = propertyNameText(node.name, sf);
      ts.forEachChild(node.initializer, (child) => visit(child, [...scope, name], parent, classQualname));
      return;
    }
    ts.forEachChild(node, (child) => visit(child, scope, parent, classQualname));
  };
  ts.forEachChild(sf, (child) => visit(child, [], moduleRegion, undefined));
  return regions;
}

/** Script kind from a file name (`.tsx`, `.jsx`, `.js`/`.mjs`/`.cjs`, else TypeScript). */
export function scriptKind(fileName: string): ts.ScriptKind {
  if (fileName.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (fileName.endsWith(".jsx")) return ts.ScriptKind.JSX;
  if (/\.[cm]?js$/.test(fileName)) return ts.ScriptKind.JS;
  return ts.ScriptKind.TS;
}

export function parseSource(fileName: string, text: string): ts.SourceFile {
  return ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true, scriptKind(fileName));
}
