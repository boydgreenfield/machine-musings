/** Repository root and repository-relative POSIX paths (what every bundle path is relative to). */
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

export const ROOT_ENV = "COVERSET_ROOT";
export const MAX_PATH_CHARS = 512;

/**
 * The directory bundle paths are relative to: `explicit`, else `COVERSET_ROOT`, else the git work
 * tree containing `from` (`git rev-parse --show-toplevel`, or the nearest directory with a `.git`
 * entry when git cannot run), else `from` itself.
 */
export function repoRoot(from: string, explicit?: string): string {
  const chosen = explicit ?? process.env[ROOT_ENV];
  if (chosen) return resolve(from, chosen);
  try {
    const out = execFileSync("git", ["rev-parse", "--show-toplevel"], {
      cwd: from,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 10_000,
    }).trim();
    if (out) return resolve(out);
  } catch {
    // not a git work tree, or no git: walk up below
  }
  for (let dir = resolve(from); ; dir = dirname(dir)) {
    if (existsSync(join(dir, ".git"))) return dir;
    if (dirname(dir) === dir) return resolve(from);
  }
}

/** `abs` relative to `root` as a safe bundle path, or null when it is outside or unsafe. */
export function relPath(root: string, abs: string): string | null {
  const rel = relative(root, abs);
  if (!rel || isAbsolute(rel)) return null;
  const posix = rel.split(sep).join("/");
  const segments = posix.split("/");
  if (segments.some((s) => s === "" || s === "." || s === "..")) return null;
  if (posix.length > MAX_PATH_CHARS || /[\\\u0000-\u001f\u007f]/.test(posix) || /^[A-Za-z]:/.test(posix)) return null;
  return posix;
}

/** A module id without its query (`/src/a.ts?v=1` → `/src/a.ts`). */
export function cleanId(id: string): string {
  const q = id.search(/[?#]/);
  return q === -1 ? id : id.slice(0, q);
}

const TEST_FILE = /(^|\/)(__tests__|__mocks__)\/|\.(test|spec|bench)\.[cm]?[jt]sx?$/;

/** Whether a repository path is a test file (never instrumented, never a caller). */
export function isTestFile(path: string): boolean {
  return TEST_FILE.test(path);
}
