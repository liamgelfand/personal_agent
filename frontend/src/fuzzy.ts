// Subsequence match for quick open and @mentions. Higher score is better; null means no match.
// Rewards consecutive characters, matches at word boundaries, and matches in the file name.
export function fuzzyScore(query: string, path: string): number | null {
  if (!query) return 0
  const q = query.toLowerCase()
  const p = path.toLowerCase()
  const nameStart = p.lastIndexOf('/') + 1
  // The query inside the file name beats any scattered match; at the start of the name, more so.
  const inName = p.slice(nameStart).indexOf(q)
  if (inName >= 0) return 1000 + (inName === 0 ? 500 : 0) - path.length * 0.01
  let score = 0
  let pi = 0
  let streak = 0
  for (const ch of q) {
    const found = p.indexOf(ch, pi)
    if (found < 0) return null
    streak = found === pi ? streak + 1 : 0
    score += 1 + streak * 2
    if (found === 0 || '/._-'.includes(p[found - 1])) score += 3
    if (found >= nameStart) score += 2
    pi = found + 1
  }
  return score - path.length * 0.01
}

export function fuzzyFilter(query: string, paths: string[], limit: number): string[] {
  return paths
    .map((path) => ({ path, score: fuzzyScore(query, path) }))
    .filter((r): r is { path: string; score: number } => r.score !== null)
    .sort((a, b) => b.score - a.score)
    .slice(0, limit)
    .map((r) => r.path)
}
