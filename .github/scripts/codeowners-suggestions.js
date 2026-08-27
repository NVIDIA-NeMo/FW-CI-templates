'use strict';

// Shared parsing/matching logic for the `# suggest:` convention layered on top
// of a repo's .github/CODEOWNERS file. Consumed via require() from the
// github-script steps in _suggest_reviewers.yml and
// _validate_codeowners_suggestions.yml, and unit tested directly in
// codeowners-suggestions.test.js — kept out of the workflow YAML so there is
// one place to fix and one place to test.
//
// Convention: a normal CODEOWNERS line is enforced (GitHub reads it, branch
// protection can require it). A `# suggest: <pattern> @owner ...` line uses
// the same path-pattern syntax but is a comment, so GitHub's own CODEOWNERS
// parser ignores it entirely — it can never become a required reviewer. It's
// meant to be read by a workflow that requests those owners as PR reviewers
// without blocking merge.

const SUGGEST_PREFIX = /^#\s*suggest:\s*(.+)$/;

function toRegex(pattern) {
  const anchored = pattern.startsWith('/');
  let p = pattern.replace(/^\//, '');
  const dirOnly = p.endsWith('/');
  if (dirOnly) p = p.slice(0, -1);

  const GLOBSTAR = '@@GLOBSTAR@@';
  const escaped = p
    .replace(/[.+^${}()|[\]\\]/g, '\\$&')
    .replace(/\*\*/g, GLOBSTAR)
    .replace(/\*/g, '[^/]*')
    .replace(new RegExp(GLOBSTAR, 'g'), '.*');

  const body = dirOnly ? `${escaped}(/.*)?` : escaped;
  return anchored || p.includes('/')
    ? new RegExp(`^${body}$`)
    : new RegExp(`(^|/)${body}$`);
}

function parseRuleLine(line) {
  const [pattern, ...owners] = line.trim().split(/\s+/);
  return { pattern, owners };
}

// Splits a raw CODEOWNERS file into its two rule kinds:
//   enforcedRules — real, non-comment lines. GitHub reads these; branch
//                   protection can require them.
//   suggestRules  — `# suggest: <pattern> @owner ...` comment lines. Invisible
//                   to GitHub's own CODEOWNERS parser; advisory only.
function parseCodeowners(text) {
  const lines = text.split('\n').map(l => l.trim());

  const enforcedRules = lines
    .filter(l => l && !l.startsWith('#'))
    .map(parseRuleLine);

  const suggestRules = lines
    .map(l => l.match(SUGGEST_PREFIX))
    .filter(Boolean)
    .map(m => m[1].trim())
    .filter(Boolean)
    .map(parseRuleLine);

  return { enforcedRules, suggestRules };
}

function compileRules(rules) {
  return rules.map(r => ({ ...r, regex: toRegex(r.pattern) }));
}

// CODEOWNERS/.gitignore semantics: the LAST matching rule wins for a path.
// Works identically for individual-user owners (`@name`) and team owners
// (`@org/team`) — both are opaque strings to the matcher.
function resolveWinningRule(compiledRules, filename) {
  let match = null;
  for (const rule of compiledRules) {
    if (rule.regex.test(filename)) match = rule;
  }
  return match;
}

// Resolves the union of owners across a set of changed files, each file
// independently resolved via last-match-wins.
function resolveOwnersForFiles(rules, filenames) {
  const compiled = compileRules(rules);
  const owners = new Set();
  for (const filename of filenames) {
    const match = resolveWinningRule(compiled, filename);
    if (match) match.owners.forEach(o => owners.add(o));
  }
  return owners;
}

// Best-effort static "does pattern A's matched file set fully contain pattern
// B's" check, for the redundancy lint below. Only reasons about the simple
// anchored file/directory patterns and the bare `*` backstop — anything with
// an inner wildcard is reported as "unknown" rather than guessed at, so this
// only ever produces confident, low-noise warnings, never false positives
// from a pattern it doesn't understand.
function normalizePattern(pattern) {
  const p = pattern.replace(/^\//, '');
  if (p === '*') return { type: 'wildcard' };
  if (p.includes('*')) return { type: 'unknown' };
  if (p.endsWith('/')) return { type: 'dir', value: p.slice(0, -1) };
  return { type: 'file', value: p };
}

function patternCovers(coveringPattern, coveredPattern) {
  const a = normalizePattern(coveringPattern);
  const b = normalizePattern(coveredPattern);
  if (a.type === 'unknown' || b.type === 'unknown') return false;
  if (a.type === 'wildcard') return true;
  if (b.type === 'wildcard') return false;
  if (a.type === 'file') return b.type === 'file' && a.value === b.value;
  return b.value === a.value || b.value.startsWith(a.value + '/');
}

function sameOwners(a, b) {
  if (a.length !== b.length) return false;
  const setA = new Set(a);
  return b.every(o => setA.has(o));
}

// Flags a later rule (enforced or suggested, in file order) as redundant when
// an earlier rule already covers its full path AND grants the exact same
// owners — i.e. adding it changes nothing, since last-match-wins means the
// earlier rule already applied to every file the later one matches. Example:
// `docker/ @team` followed later by `# suggest: docker/scripts/ @team` is
// redundant; the same followed by `@other-team` is not (that's a deliberate,
// meaningful override).
function findRedundantRules(enforcedRules, suggestRules) {
  const all = [
    ...enforcedRules.map((r, i) => ({ ...r, source: 'enforced', order: i })),
    ...suggestRules.map((r, i) => ({ ...r, source: 'suggest', order: i + enforcedRules.length })),
  ];

  const redundant = [];
  for (let j = 0; j < all.length; j++) {
    for (let i = 0; i < j; i++) {
      const earlier = all[i];
      const later = all[j];
      if (earlier.pattern === later.pattern && earlier.source === later.source) {
        redundant.push({ rule: later, coveredBy: earlier, reason: 'exact duplicate pattern' });
        break;
      }
      if (patternCovers(earlier.pattern, later.pattern) && sameOwners(earlier.owners, later.owners)) {
        redundant.push({ rule: later, coveredBy: earlier, reason: 'nested path, identical owners' });
        break;
      }
    }
  }
  return redundant;
}

module.exports = {
  toRegex,
  parseRuleLine,
  parseCodeowners,
  compileRules,
  resolveWinningRule,
  resolveOwnersForFiles,
  patternCovers,
  findRedundantRules,
};
