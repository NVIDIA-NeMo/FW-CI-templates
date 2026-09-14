'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
  toGlob,
  resolveLastMatchPerFile,
  parseCodeowners,
  patternCovers,
  findRedundantRules,
} = require('./codeowners-suggestions.js');

// These assert the exact glob string(s) toGlob emits, which is what actually
// gets handed to step-security/changed-files' `files_yaml:` input. Verified
// separately against a real minimatch install to confirm these globs produce
// the same match/no-match results our old hand-rolled regex engine did.

test('toGlob: anchored file pattern -> exact path, no wildcarding', () => {
  assert.deepEqual(toGlob('/nemo_gym/skills.py'), ['nemo_gym/skills.py']);
});

test('toGlob: anchored directory pattern -> single "dir/**" glob', () => {
  assert.deepEqual(toGlob('/nemo_gym/sandbox/'), ['nemo_gym/sandbox/**']);
});

test('toGlob: unanchored directory pattern -> matches at any depth via two globs', () => {
  assert.deepEqual(toGlob('docker/'), ['docker/**', '**/docker/**']);
});

test('toGlob: unanchored bare filename -> matches at any depth via two globs, no dir suffix', () => {
  assert.deepEqual(toGlob('package.json'), ['package.json', '**/package.json']);
});

test('toGlob: patterns already containing "**" pass through unchanged (minimatch owns globstar semantics)', () => {
  assert.deepEqual(toGlob('/a/**/b.py'), ['a/**/b.py']);
  assert.deepEqual(toGlob('**/docs/'), ['**/docs/**']);
  assert.deepEqual(toGlob('docs/**'), ['docs/**']);
});

test('toGlob: bare "*" -> single "**" glob (matches everything)', () => {
  assert.deepEqual(toGlob('*'), ['**']);
});

test('parseCodeowners: separates enforced lines from `# suggest:` lines', () => {
  const { enforcedRules, suggestRules } = parseCodeowners([
    'docker/ @nvidia-nemo/automation',
    '# a plain comment, not a suggestion',
    '# suggest: docker/ @anwithk',
  ].join('\n'));

  assert.deepEqual(enforcedRules, [{ pattern: 'docker/', owners: ['@nvidia-nemo/automation'] }]);
  assert.deepEqual(suggestRules, [{ pattern: 'docker/', owners: ['@anwithk'] }]);
});

test('resolveLastMatchPerFile: works for individual users and teams alike', () => {
  const rulesWithMatches = [
    { pattern: '/docker/', owners: ['@anwithk'], matchedFiles: ['docker/Dockerfile'] },
    { pattern: '/nemo_gym/sandbox/', owners: ['@nvidia-nemo/gym_core'], matchedFiles: ['nemo_gym/sandbox/aws.py'] },
  ];
  const winners = resolveLastMatchPerFile(rulesWithMatches);
  assert.equal(winners.get('docker/Dockerfile').owners[0], '@anwithk');
  assert.equal(winners.get('nemo_gym/sandbox/aws.py').owners[0], '@nvidia-nemo/gym_core');
});

test('resolveLastMatchPerFile: last matching rule wins, per CODEOWNERS semantics', () => {
  const rulesWithMatches = [
    // Listed first, matches every file (like a "*" backstop rule).
    { pattern: '*', owners: ['@nvidia-nemo/gym_architects'], matchedFiles: ['nemo_gym/registry.py'] },
    // Listed second (later in the file) -- must win for the file it also matches.
    { pattern: '/nemo_gym/registry.py', owners: ['@nvidia-nemo/gym_core'], matchedFiles: ['nemo_gym/registry.py'] },
  ];
  const winners = resolveLastMatchPerFile(rulesWithMatches);
  assert.equal(winners.get('nemo_gym/registry.py').owners[0], '@nvidia-nemo/gym_core');
});

test('patternCovers: directory covers itself and nested paths, not siblings', () => {
  assert.ok(patternCovers('/docker/', '/docker/'));
  assert.ok(patternCovers('/docker/', '/docker/scripts/'));
  assert.ok(!patternCovers('/docker/', '/docker_extra/'));
});

test('patternCovers: "*" covers everything; nothing covers "*" but itself', () => {
  assert.ok(patternCovers('*', '/docker/'));
  assert.ok(!patternCovers('/docker/', '*'));
});

test('patternCovers: patterns with inner wildcards are "unknown", never claimed as covering', () => {
  assert.ok(!patternCovers('/nemo_gym/*.py', '/nemo_gym/registry.py'));
});

test('findRedundantRules: nested suggest with identical owner to an enforced parent is flagged', () => {
  const enforcedRules = [{ pattern: '/docker/', owners: ['@nvidia-nemo/automation'] }];
  const suggestRules = [{ pattern: '/docker/scripts/', owners: ['@nvidia-nemo/automation'] }];

  const redundant = findRedundantRules(enforcedRules, suggestRules);
  assert.equal(redundant.length, 1);
  assert.equal(redundant[0].rule.pattern, '/docker/scripts/');
  assert.equal(redundant[0].coveredBy.pattern, '/docker/');
});

test('findRedundantRules: nested suggest with a DIFFERENT owner is a deliberate override, not flagged', () => {
  const enforcedRules = [{ pattern: '/docker/', owners: ['@nvidia-nemo/automation'] }];
  const suggestRules = [{ pattern: '/docker/scripts/', owners: ['@anwithk'] }];

  assert.deepEqual(findRedundantRules(enforcedRules, suggestRules), []);
});

test('findRedundantRules: exact duplicate pattern within the same rule kind is flagged', () => {
  const suggestRules = [
    { pattern: '/docker/', owners: ['@anwithk'] },
    { pattern: '/docker/', owners: ['@anwithk'] },
  ];
  const redundant = findRedundantRules([], suggestRules);
  assert.equal(redundant.length, 1);
  assert.equal(redundant[0].reason, 'exact duplicate pattern');
});

test('findRedundantRules: unrelated rules for disjoint paths are not flagged', () => {
  const enforcedRules = [{ pattern: '/docker/', owners: ['@nvidia-nemo/automation'] }];
  const suggestRules = [{ pattern: '/nemo_gym/sandbox/', owners: ['@nvidia-nemo/gym_core'] }];
  assert.deepEqual(findRedundantRules(enforcedRules, suggestRules), []);
});
