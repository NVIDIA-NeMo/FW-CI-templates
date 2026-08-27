'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
  toRegex,
  parseCodeowners,
  resolveOwnersForFiles,
  patternCovers,
  findRedundantRules,
} = require('./codeowners-suggestions.js');

test('toRegex: anchored file pattern matches only from repo root', () => {
  const re = toRegex('/nemo_gym/skills.py');
  assert.ok(re.test('nemo_gym/skills.py'));
  assert.ok(!re.test('other/nemo_gym/skills.py'));
  assert.ok(!re.test('nemo_gym/skills.py.bak'));
});

test('toRegex: directory pattern matches nested files but not sibling prefix dirs', () => {
  const re = toRegex('/nemo_gym/sandbox/');
  assert.ok(re.test('nemo_gym/sandbox/foo.py'));
  assert.ok(re.test('nemo_gym/sandbox/deep/bar.py'));
  assert.ok(!re.test('nemo_gym/sandbox_extra/foo.py'));
});

test('toRegex: unanchored directory pattern matches at any depth', () => {
  const re = toRegex('docker/');
  assert.ok(re.test('docker/Dockerfile'));
  assert.ok(re.test('a/b/docker/Dockerfile'));
  assert.ok(!re.test('my_docker_config/foo.py'));
});

test('toRegex: bare "*" matches every path', () => {
  const re = toRegex('*');
  assert.ok(re.test('README.md'));
  assert.ok(re.test('nemo_gym/registry.py'));
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

test('resolveOwnersForFiles: works for individual users and teams alike', () => {
  const { suggestRules } = parseCodeowners([
    '# suggest: /docker/ @anwithk',
    '# suggest: /nemo_gym/sandbox/ @nvidia-nemo/gym_core',
  ].join('\n'));

  const owners = resolveOwnersForFiles(suggestRules, ['docker/Dockerfile', 'nemo_gym/sandbox/aws.py']);
  assert.deepEqual([...owners].sort(), ['@anwithk', '@nvidia-nemo/gym_core']);
});

test('resolveOwnersForFiles: last matching rule wins, per CODEOWNERS semantics', () => {
  const rules = [
    { pattern: '*', owners: ['@nvidia-nemo/gym_architects'] },
    { pattern: '/nemo_gym/registry.py', owners: ['@nvidia-nemo/gym_core'] },
  ];
  const owners = resolveOwnersForFiles(rules, ['nemo_gym/registry.py']);
  assert.deepEqual([...owners], ['@nvidia-nemo/gym_core']);
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
