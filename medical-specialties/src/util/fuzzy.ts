/**
 * Small subsequence/word-prefix matcher for the search box.
 * Returns a score (higher is better) or -1 for no match.
 */
export function fuzzyScore(query: string, text: string): number {
  const q = query.trim().toLowerCase();
  if (!q) return 0;
  const t = text.toLowerCase();
  if (t === q) return 1000;
  if (t.startsWith(q)) return 800 - t.length * 0.1;
  const wordIndex = t.split(/[^a-z0-9]+/).findIndex((w) => w.startsWith(q));
  if (wordIndex >= 0) return 600 - wordIndex * 10 - t.length * 0.1;
  const idx = t.indexOf(q);
  if (idx >= 0) return 400 - idx - t.length * 0.1;
  // Subsequence match with a penalty for gaps.
  let ti = 0;
  let gaps = 0;
  for (let qi = 0; qi < q.length; qi++) {
    const ch = q[qi]!;
    if (ch === ' ') continue;
    const next = t.indexOf(ch, ti);
    if (next < 0) return -1;
    gaps += next - ti;
    ti = next + 1;
  }
  return Math.max(1, 200 - gaps * 4 - t.length * 0.1);
}

export function bestScore(query: string, candidates: string[]): number {
  let best = -1;
  for (const c of candidates) best = Math.max(best, fuzzyScore(query, c));
  return best;
}
