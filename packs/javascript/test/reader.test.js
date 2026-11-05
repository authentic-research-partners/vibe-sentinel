import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { Evidence, digest } from '../src/protocol.js';
import { Project, measureWorkspaces } from '../src/project.js';
import { measureImports } from '../src/imports.js';

const bin = fileURLToPath(new URL('../bin/vibe-sentinel-javascript.js', import.meta.url));
function fixture(t, files, manifest = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'sentinel-js-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  write(root, 'package.json', JSON.stringify({ name: 'fixture', ...manifest }));
  for (const [name, contents] of Object.entries(files)) write(root, name, contents);
  return fs.realpathSync(root);
}
function write(root, name, contents) {
  const file = path.join(root, name); fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, contents);
}
function read(root, mode = 'imports') {
  const evidence = new Evidence(); const project = new Project(root, evidence, { useConfigs: mode === 'imports' });
  if (mode === 'workspaces') measureWorkspaces(project, evidence); else measureImports(project, evidence);
  return evidence.result('fixture');
}
const edges = result => result.observations.filter(item => item.key.startsWith('edge:1:'));
const targets = result => edges(result).map(item => item.attrs.target);
const gapCodes = result => result.coverage.gaps.map(gap => gap.code);

// Fixtures contain executable traps as source text. Readers must never run them.
test('stable AST edges detect a count-preserving rewire and ignore source-like text', t => {
  const root = fixture(t, { 'main.ts': `import { a, b } from './one';\nconst misleading = "import './not-real'"; // require('./also-not-real')\n`, 'one.ts': 'export const a=1,b=2;', 'two.ts': 'export const a=1,b=2;' });
  const before = read(root); assert.equal(before.coverage.status, 'complete');
  assert.equal(edges(before).length, 1); assert.equal(edges(before)[0].value, 1);
  assert.deepEqual(read(root), before);
  write(root, 'main.ts', "import { a, b } from './two';\n");
  const after = read(root); assert.equal(edges(after).length, 1);
  assert.notEqual(edges(before)[0].key, edges(after)[0].key);
  const tuple = ['npm','file:main.ts','file:two.ts','import','runtime','local'];
  assert.equal(edges(after)[0].key, `edge:1:${digest(tuple)}`);
});

test('mixed and type-only declarations preserve lexical phases and occurrence counts', t => {
  const root = fixture(t, { 'main.ts': `import { type A, value } from './types';\nimport type { A as B } from './types';\nexport { type A, value } from './types';\nexport type * from './types';\ntype C = import('./types').A;`, 'types.ts': 'export type A=string; export const value=1;' });
  const result = read(root); assert.equal(result.coverage.status, 'complete');
  const counts = Object.fromEntries(edges(result).map(edge => [`${edge.attrs.kind}:${edge.attrs.phase}`, edge.value]));
  assert.deepEqual(counts, { 'reexport:type': 2, 'import:type': 3, 'reexport:runtime': 1, 'import:runtime': 1 });
});

test('workspace names, aliases and JSX are static source relationships', t => {
  const root = fixture(t, {
    'tsconfig.json': JSON.stringify({ compilerOptions: { baseUrl: '.', paths: { '@local/*': ['packages/ui/src/*'] } }, include: ['packages/**/*'] }),
    'packages/ui/package.json': JSON.stringify({ name: '@fixture/ui' }),
    'packages/ui/src/main.tsx': `import { value } from '@fixture/api'; import helper from '@local/helper'; export const UI=()=> <div>{value}{helper}</div>;`,
    'packages/ui/src/helper.ts': 'export default 1;',
    'packages/api/package.json': JSON.stringify({ name: '@fixture/api', exports: './src/index.ts' }),
    'packages/api/src/index.ts': 'export const value=1;',
    'unselected/package.json': JSON.stringify({ name: 'not-selected' }),
    'unselected/trap.js': "import './missing'; throw new Error('not selected');",
  }, { workspaces: ['packages/*'] });
  const result = read(root); assert.equal(result.coverage.status, 'complete');
  assert.ok(edges(result).some(edge => edge.attrs.resolution === 'workspace' && edge.attrs.target === 'file:packages/api/src/index.ts'));
  assert.ok(targets(result).includes('file:packages/ui/src/helper.ts'));
  assert.ok(!edges(result).some(edge => edge.attrs.source.startsWith('file:unselected/')));
});

test('explicit tsconfig files survive exclude and inheritance keeps each field base', t => {
  const root = fixture(t, {
    'base.json': JSON.stringify({ include: ['src/**/*.ts'] }),
    'tsconfig.json': JSON.stringify({ extends: './base.json', files: ['src/always.ts'], exclude: ['src/**'] }),
    'src/always.ts': "import './target';", 'src/target.ts': 'export {};',
    'src/ignored.ts': "import './missing';",
  });
  const result = read(root); assert.equal(result.coverage.status, 'complete');
  assert.deepEqual(targets(result), ['file:src/target.ts']);
  const other = fixture(t, {
    'config/base.json': JSON.stringify({ include: ['../src/**/*.ts'] }),
    'tsconfig.json': JSON.stringify({ extends: './config/base.json', exclude: ['src/ignored.ts'] }),
    'src/yes.ts': "import './target';", 'src/target.ts': 'export {};', 'src/ignored.ts': "import './missing';",
  });
  assert.equal(read(other).coverage.status, 'complete');
  assert.deepEqual(targets(read(other)), ['file:src/target.ts']);
});

test('workspace measurement is independent of unsupported compiler configuration', t => {
  const root = fixture(t, { 'main.ts': 'export {};', 'tsconfig.json': JSON.stringify({ compilerOptions: { plugins: [{ name: './never-run.js' }] } }), 'never-run.js': "throw new Error('configuration is data');" });
  assert.equal(read(root, 'workspaces').coverage.status, 'complete');
  assert.ok(gapCodes(read(root)).includes('unsupported-compiler-option'));
});

test('node prefix-only builtins never collapse into declared external package names', t => {
  const root = fixture(t, { 'main.mjs': "import 'test'; import 'node:test'; import 'fs'; import 'node:fs';" }, { dependencies: { test: '*' } });
  const result = read(root); assert.equal(result.coverage.status, 'complete');
  assert.ok(targets(result).includes('package:test')); assert.ok(targets(result).includes('builtin:test'));
  assert.equal(edges(result).find(edge => edge.attrs.target === 'builtin:fs').value, 2);
});

test('computed imports and unresolved targets are explicit gaps with no source snippets', t => {
  const root = fixture(t, { 'main.js': `const privateText = 'not-output-source'; import(privateText); require('./missing');` });
  const result = read(root); assert.equal(result.coverage.status, 'incomplete');
  assert.ok(gapCodes(result).includes('dynamic-specifier')); assert.ok(gapCodes(result).includes('unresolved-import'));
  assert.ok(!JSON.stringify(result).includes('not-output-source'));
  const run = spawnSync(process.execPath, [bin, 'imports', '--root', root], { encoding: 'utf8', timeout: 10000 });
  assert.equal(run.status, 2); assert.equal(JSON.parse(run.stdout).coverage.status, 'incomplete');
  assert.match(run.stderr, /dynamic-specifier|unresolved-import/); assert.ok(run.stderr.length < 500);
});

test('shadowed or reassigned require never becomes a false dependency', t => {
  const root = fixture(t, { 'main.cjs': "function use(require) { return require('./not-loader'); }" });
  const result = read(root); assert.ok(gapCodes(result).includes('shadowed-require')); assert.equal(edges(result).length, 0);
  write(root, 'main.cjs', "require = other; require('./not-loader');");
  assert.ok(gapCodes(read(root)).includes('shadowed-require'));
});

test('literal dynamic and CommonJS imports resolve without executing code', t => {
  const root = fixture(t, { 'main.cjs': "const x=require('./trap'); import('./trap.js');", 'trap.js': "throw new Error('never execute app');" });
  const result = read(root); assert.equal(result.coverage.status, 'complete');
  assert.deepEqual(new Set(edges(result).map(edge => edge.attrs.kind)), new Set(['require','dynamic-import']));
});

test('conditional exports and package imports fail explicitly rather than selecting a guessed branch', t => {
  const root = fixture(t, {
    'main.cts': "import '#util'; import '@fixture/pkg';",
    'tsconfig.json': JSON.stringify({ compilerOptions: { module: 'NodeNext', moduleResolution: 'NodeNext' } }),
    'esm.ts': 'export {};', 'cjs.cts': 'export {};',
    'packages/pkg/package.json': JSON.stringify({ name: '@fixture/pkg', exports: { import: './index.js', require: './index.cjs' } }),
    'packages/pkg/index.js': 'export {};', 'packages/pkg/index.cjs': 'module.exports={};',
  }, { workspaces: ['packages/*'], imports: { '#util': { import: './esm.ts', require: './cjs.cts' } } });
  const result = read(root); assert.ok(gapCodes(result).includes('unsupported-package-imports'));
  assert.ok(gapCodes(result).includes('unsupported-workspace-exports'));
});

test('URL imports with authentication or query data never echo those strings', t => {
  const privateValue = 'throwaway-' + Math.random().toString(36).slice(2);
  const root = fixture(t, { 'main.js': `import 'https://user:${privateValue}@example.invalid/a.js?token=${privateValue}';` });
  const result = read(root); assert.ok(gapCodes(result).includes('unsupported-url-import'));
  assert.ok(!JSON.stringify(result).includes(privateValue)); assert.ok(!JSON.stringify(result).includes('example.invalid'));
});

test('symlinked source and outside configs are not read; installed trees stay excluded', t => {
  const outside = fixture(t, { 'outside.ts': "throw new Error('outside source must never be imported');" });
  const root = fixture(t, { 'main.ts': 'export {};', 'node_modules/trap/index.ts': "import './missing';", '.venv/trap.ts': "import './missing';", 'tsconfig.json': JSON.stringify({ extends: path.join(outside, 'base.json') }) });
  fs.symlinkSync(path.join(outside, 'outside.ts'), path.join(root, 'linked.ts'));
  const result = read(root); assert.ok(gapCodes(result).includes('symlink-not-followed'));
  assert.ok(gapCodes(result).includes('unsupported-config-extends'));
  assert.ok(!JSON.stringify(result).includes('outside source must never'));
  assert.ok(!edges(result).some(edge => /node_modules|\.venv/.test(edge.attrs.source)));
});

test('invalid UTF8 and parser errors are coverage failures, never empty success', t => {
  const root = fixture(t, { 'main.ts': Buffer.from([0xc3, 0x28]) });
  assert.equal(read(root).coverage.status, 'incomplete');
  write(root, 'main.ts', "import { from './broken';");
  assert.ok(gapCodes(read(root)).includes('parse-error'));
});

test('JSDoc import types are type relationships', t => {
  const root = fixture(t, { 'main.js': "/** @type {import('./types').A} */\nlet value;", 'types.d.ts': 'export type A=string;' });
  const result = read(root); assert.equal(result.coverage.status, 'complete');
  assert.ok(edges(result).some(edge => edge.attrs.phase === 'type' && edge.attrs.target === 'file:types.d.ts'));
});

test('resolved imports traverse source excluded from the initial tsconfig roots', t => {
  const root = fixture(t, { 'tsconfig.json': JSON.stringify({ files: ['entry.ts'], exclude: ['hidden/**'] }),
    'entry.ts': "import './hidden/one';", 'hidden/one.ts': "import './two';", 'hidden/two.ts': 'export {};' });
  const result = read(root); assert.equal(result.coverage.status, 'complete');
  assert.deepEqual(new Set(targets(result)), new Set(['file:hidden/one.ts', 'file:hidden/two.ts']));
});

test('empty root cannot conceal configured missing or outside sources', t => {
  const root = fixture(t, { 'tsconfig.json': JSON.stringify({ files: ['../outside.ts'] }) });
  assert.equal(read(root).coverage.status, 'incomplete');
  assert.ok(gapCodes(read(root)).includes('unreadable-config-file'));
  assert.equal(read(root, 'workspaces').coverage.status, 'complete');
});

test('help is available without inspecting a project', () => {
  const result = spawnSync(process.execPath, [bin, '--help'], { encoding: 'utf8', timeout: 10000 });
  assert.equal(result.status, 0); assert.match(result.stdout, /--root PATH/);
});

test('workspace self pattern never expands to undeclared nested packages', t => {
  const root = fixture(t, { 'nested/package.json': JSON.stringify({ name: 'undeclared' }), 'nested/trap.js': "import './missing';" }, { workspaces: ['.'] });
  const result = read(root, 'workspaces');
  assert.ok(gapCodes(result).includes('unsupported-workspace-pattern'));
  assert.ok(!result.observations.some(item => item.attrs?.name === 'undeclared'));
});

test('invalid dependency declarations are explicit and outside import paths stay private', t => {
  const root = fixture(t, { 'main.js': "import '/private/outside/location.js';" }, { dependencies: ['not-a-map'] });
  const result = read(root); assert.ok(gapCodes(result).includes('invalid-dependency-declarations'));
  assert.ok(gapCodes(result).includes('outside-import')); assert.ok(!JSON.stringify(result).includes('/private/outside/location'));
});
