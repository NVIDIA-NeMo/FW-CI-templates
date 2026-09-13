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

// Converts one CODEOWNERS-style pattern into one or more minimatch-compatible
// glob strings (the syntax step-security/changed-files' `files_yaml:` input
// expects), preserving the same CODEOWNERS/.gitignore semantics this module
// used to implement by hand via a custom regex builder:
//   - a leading "/" (or any "/" at all) anchors the pattern to the repo root
//   - a trailing "/" is a directory pattern: matches that directory and
//     everything under it
//   - a pattern with no "/" anywhere matches at any depth (basename match)
//   - "**"/"*" keep their normal glob meaning; minimatch's own "**" already
//     handles the zero-or-more-directories case correctly (unlike this
//     module's first hand-rolled regex attempt, which needed a follow-up fix)
// Returns an array because the "matches at any depth" case needs two
// alternatives (root-level and nested) rather than one pattern with branching.
function toGlob(pattern) {
  const anchored = pattern.startsWith('/');
  let p = pattern.replace(/^\//, '');
  const dirOnly = p.endsWith('/');
  if (dirOnly) p = p.slice(0, -1);

  if (p === '*') return ['**'];

  const withDirSuffix = dirOnly ? `${p}/**` : p;

  if (anchored || p.includes('/')) {
    return [withDirSuffix];
  }
  // Unanchored, no "/" anywhere in the pattern (e.g. "docker/" or a bare
  // filename like "package.json") — matches at any depth, not just the root.
  return dirOnly ? [`${p}/**`, `**/${p}/**`] : [p, `**/${p}`];
}

// CODEOWNERS/.gitignore semantics: the LAST matching rule wins for a path.
// Takes rules in FILE ORDER, each annotated with which changed files it
// matched (however that matching was actually done -- by us, or by an
// external tool like step-security/changed-files) and resolves each file to
// exactly one winning rule by walking the rules in order and letting each
// later match overwrite any earlier one for the same file.
function resolveLastMatchPerFile(rulesWithMatches) {
  const winningRuleByFile = new Map();
  for (const rule of rulesWithMatches) {
    for (const filename of rule.matchedFiles) {
      winningRuleByFile.set(filename, rule);
    }
  }
  return winningRuleByFile;
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
  toGlob,
  resolveLastMatchPerFile,
  parseRuleLine,
  parseCodeowners,
  patternCovers,
  findRedundantRules,
};
