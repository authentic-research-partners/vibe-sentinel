import fs from 'node:fs';
import path from 'node:path';
import ts from 'typescript';
const UTF8 = new TextDecoder('utf-8', { fatal: true });
import { order } from './protocol.js';

// Versioned pack source policy, independent of the Python installation.
export const EXCLUDED = new Set(['.git','.hg','.svn','.venv','venv','env','site-packages','.tox','.nox',
  'node_modules','bower_components','vendor','__pycache__','.mypy_cache','.ruff_cache','.pytest_cache',
  '.ipynb_checkpoints','.gradle','.terraform','build','dist','.eggs','.next','.nuxt','.vibe-sentinel','coverage']);
const SOURCE = /\.(?:[cm]?[jt]sx?)$/i;
const MAX_FILE_BYTES = 2 * 1024 * 1024;
const MAX_ENTRIES = 30000;
export const slash = value => value.split(path.sep).join('/');

export class Project {
  constructor(root, evidence, { useConfigs = true } = {}) {
    this.root = fs.realpathSync(path.resolve(root));
    if (!fs.statSync(this.root).isDirectory()) throw new Error('Root must be a project directory');
    this.evidence = evidence; this.files = []; this.dirs = []; this.packages = [];
    this.configs = new Map(); this.configPaths = new Map(); this.configRaw = new Map();
    this.entries = 0;
    this.walk(this.root);
    this.fileSet = new Set(this.files); this.dirSet = new Set(this.dirs);
    this.directoryEntries = new Map(this.dirs.map(dir => [dir, { files: [], directories: [] }]));
    for (const file of this.files) this.directoryEntries.get(path.dirname(file))?.files.push(path.basename(file));
    for (const dir of this.dirs) if (dir !== this.root) this.directoryEntries.get(path.dirname(dir))?.directories.push(path.basename(dir));
    this.discoverWorkspaces();
    this.sources = this.files.filter(file => SOURCE.test(file) && this.owner(file));
    if (useConfigs) {
      // Declared configs can be invalid even when no source file was discovered.
      for (const file of this.files) if (['tsconfig.json', 'jsconfig.json'].includes(path.basename(file)) && this.owner(file)) this.config(file);
      this.sources = this.sources.filter(file => this.selectedByConfig(file));
    }
  }
  rel(file) { return slash(path.relative(this.root, file)) || '.'; }
  contained(file) {
    const rel = path.relative(this.root, path.resolve(file));
    return rel === '' || (!rel.startsWith(`..${path.sep}`) && rel !== '..' && !path.isAbsolute(rel));
  }
  readable(file) {
    if (!this.contained(file)) return false;
    const rel = path.relative(this.root, file);
    let current = this.root;
    try {
      for (const part of rel.split(path.sep).filter(Boolean)) {
        if (EXCLUDED.has(part)) return false;
        current = path.join(current, part);
        if (fs.lstatSync(current).isSymbolicLink()) return false;
      }
      return fs.statSync(file).isFile();
    } catch { return false; }
  }
  read(file) {
    if (!this.readable(file)) { this.evidence.gap('unreadable-or-outside-input', this.contained(file) ? this.rel(file) : 'outside-project'); return null; }
    try {
      if (fs.statSync(file).size > MAX_FILE_BYTES) { this.evidence.gap('oversized-input', this.rel(file)); return null; }
      return UTF8.decode(fs.readFileSync(file));
    } catch { this.evidence.gap('unreadable-input', this.rel(file)); return null; }
  }
  json(file, comments = false) {
    const text = this.read(file); if (text === null) return null;
    try {
      const parsed = comments ? ts.parseConfigFileTextToJson(file, text) : { config: JSON.parse(text) };
      if (parsed.error || !parsed.config || typeof parsed.config !== 'object' || Array.isArray(parsed.config)) throw new Error();
      return parsed.config;
    } catch { this.evidence.gap('invalid-json', this.rel(file)); return null; }
  }
  walk(directory) {
    this.dirs.push(directory);
    let entries;
    try { entries = fs.readdirSync(directory, { withFileTypes: true }).sort((a,b) => order(a.name,b.name)); }
    catch { this.evidence.gap('unreadable-directory', this.rel(directory)); return; }
    for (const entry of entries) {
      if (EXCLUDED.has(entry.name)) continue;
      if (++this.entries > MAX_ENTRIES) { this.evidence.gap('source-limit', '.'); return; }
      const file = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) {
        this.evidence.gap('symlink-not-followed', this.rel(file)); continue;
      }
      if (entry.isDirectory()) this.walk(file);
      else if (entry.isFile()) this.files.push(file);
    }
  }
  discoverWorkspaces() {
    const manifest = this.json(path.join(this.root, 'package.json'));
    const rootPackage = { root: this.root, manifest: manifest ?? {}, name: typeof manifest?.name === 'string' ? manifest.name : '(root)' };
    this.packages = [rootPackage];
    const candidates = this.files.filter(file => path.basename(file) === 'package.json' && path.dirname(file) !== this.root);
    this.packageRoots = new Set(candidates.map(file => path.dirname(file))); this.packageRoots.add(this.root);
    if (!manifest) return;
    this.validateManifest(manifest, 'package.json');
    if (this.files.includes(path.join(this.root, 'pnpm-workspace.yaml')) && !manifest.workspaces)
      this.evidence.gap('unsupported-workspace-format', 'pnpm-workspace.yaml');
    const configured = manifest.workspaces?.packages ?? manifest.workspaces ?? [];
    if (!Array.isArray(configured) || !configured.every(item => typeof item === 'string')) {
      this.evidence.gap('invalid-workspaces', 'package.json'); return;
    }
    const selected = new Set();
    for (const pattern of configured) {
      const matcher = glob(pattern, false);
      if (!matcher) { this.evidence.gap('unsupported-workspace-pattern', 'package.json'); continue; }
      const found = candidates.filter(file => matcher.test(this.rel(path.dirname(file))));
      if (!found.length) this.evidence.gap('missing-workspace', pattern);
      for (const file of found) selected.add(file);
    }
    for (const file of [...selected].sort(order)) {
      const data = this.json(file);
      if (!data || typeof data.name !== 'string' || !data.name.trim()) { this.evidence.gap('unnamed-workspace', this.rel(file)); continue; }
      this.validateManifest(data, this.rel(file));
      this.packages.push({ root: path.dirname(file), manifest: data, name: data.name });
    }
    const names = new Set();
    for (const pkg of this.packages) {
      if (names.has(pkg.name)) this.evidence.gap('duplicate-workspace-name', this.rel(pkg.root));
      names.add(pkg.name);
    }

  }
  validateManifest(manifest, scope) {
    if (manifest.name !== undefined && (typeof manifest.name !== 'string' || !manifest.name.trim() || manifest.name.length > 500)) this.evidence.gap('invalid-package-name', scope);
    if (manifest.type !== undefined && !['module', 'commonjs'].includes(manifest.type)) this.evidence.gap('invalid-package-type', scope);
    for (const field of ['dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies']) {
      const value = manifest[field];
      if (value !== undefined && (!value || typeof value !== 'object' || Array.isArray(value) || !Object.entries(value).every(([name, spec]) => name.trim() && typeof spec === 'string' && spec.trim()))) this.evidence.gap('invalid-dependency-declarations', scope);
    }
  }
  owner(file) {
    let current = path.dirname(file);
    while (this.contained(current)) {
      if (current === this.root || this.packageRoots?.has(current)) return this.packages.find(pkg => pkg.root === current) ?? null;
      current = path.dirname(current);
    }
    return null;
  }
  nearestConfig(file) {
    let current = path.dirname(file);
    while (this.contained(current)) {
      for (const name of ['tsconfig.json', 'jsconfig.json']) {
        const candidate = path.join(current, name);
        if (this.files.includes(candidate)) return candidate;
      }
      if (current === this.root) break;
      current = path.dirname(current);
    }
    return null;
  }
  config(file) {
    const fallback = { options: { moduleResolution: ts.ModuleResolutionKind.Bundler, module: ts.ModuleKind.ESNext, allowJs: true }, fileNames: [] };
    if (!file) return fallback;
    if (this.configs.has(file)) return this.configs.get(file);
    const inspect = (candidate, seen = new Set()) => {
      if (seen.has(candidate)) return;
      seen.add(candidate);
      const raw = this.json(candidate, true);
      if (!raw) return;
      for (const key of ['rootDirs','moduleSuffixes','customConditions','plugins']) if (raw.compilerOptions?.[key] !== undefined) this.evidence.gap('unsupported-compiler-option', `${this.rel(candidate)}:${key}`);
      if (raw.references !== undefined) this.evidence.gap('unsupported-project-references', this.rel(candidate));
      const parents = raw.extends === undefined ? [] : Array.isArray(raw.extends) ? raw.extends : [raw.extends];
      for (const declared of parents) {
        if (typeof declared !== 'string' || !declared.startsWith('.')) { this.evidence.gap('unsupported-config-extends', this.rel(candidate)); continue; }
        let parent = path.resolve(path.dirname(candidate), declared);
        if (!path.extname(parent)) parent += '.json';
        if (!this.contained(parent)) this.evidence.gap('outside-config', this.rel(candidate));
        else inspect(parent, seen);
      }
    };
    inspect(file);
    const host = {
      useCaseSensitiveFileNames: true,
      getCurrentDirectory: () => this.root,
      fileExists: candidate => this.readable(candidate),
      readFile: candidate => this.readable(candidate) ? this.read(candidate) ?? undefined : undefined,
      readDirectory: (directory, extensions, excludes, includes, depth) => {
        if (!this.contained(directory)) { this.evidence.gap('outside-config', this.rel(file)); return []; }
        // TypeScript's own matcher over a pre-enumerated, symlink-free host.
        return ts.matchFiles(directory, extensions, excludes, includes, true, this.root, depth,
          candidate => this.directoryEntries.get(path.resolve(candidate)) ?? { files: [], directories: [] },
          candidate => candidate);
      },
      onUnRecoverableConfigFileDiagnostic: () => this.evidence.gap('invalid-config', this.rel(file)),
    };
    const parsed = ts.getParsedCommandLineOfConfigFile(file, {}, host);
    if (!parsed || parsed.errors.length) this.evidence.gap('invalid-config', this.rel(file));
    const config = parsed ?? fallback;
    if (config.options.moduleResolution === undefined) config.options.moduleResolution = ts.ModuleResolutionKind.Bundler;
    if (config.options.module === undefined) config.options.module = ts.ModuleKind.ESNext;
    for (const selected of config.fileNames) if (!this.readable(selected)) this.evidence.gap('unreadable-config-file', this.rel(file));
    this.configs.set(file, config); return config;
  }
  selectedByConfig(file) {
    const configPath = this.nearestConfig(file);
    const config = this.config(configPath);
    this.configPaths.set(file, configPath); this.configRaw.set(file, config);
    return !configPath || config.fileNames.includes(file);
  }

}

// This parses bounded filesystem patterns, never JavaScript source.
export function glob(pattern, directoryPrefix) {
  if (pattern === '.' || pattern === './') return directoryPrefix ? /^.*$/ : null;
  if (!pattern || pattern.includes(':') || pattern.includes('\x00') || pattern.startsWith('/') || pattern.split('/').includes('..') || /[!{}[\]\\]/.test(pattern)) return null;
  pattern = pattern.replace(/^\.\//, '').replace(/\/$/, '');
  let regex = '';
  for (let index = 0; index < pattern.length; index++) {
    const char = pattern[index];
    if (char === '*' && pattern[index + 1] === '*') {
      if (pattern[index + 2] === '/') { regex += '(?:.*/)?'; index += 2; }
      else { regex += '.*'; index++; }
    } else if (char === '*') regex += '[^/]*';
    else if (char === '?') regex += '[^/]';
    else regex += char.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }
  if (directoryPrefix && !/[.*?]/.test(path.basename(pattern))) regex += '(?:/.*)?';
  return new RegExp(`^${regex}$`);
}

export function measureWorkspaces(project, evidence) {
  for (const pkg of project.packages) evidence.component(`workspace:${project.rel(pkg.root)}`, 'workspace',
    { name: String(pkg.name), path: project.rel(pkg.root) }, JSON.stringify([pkg.name, pkg.manifest.private === true]));
  for (const file of project.sources) evidence.component(`file:${project.rel(file)}`, 'source',
    { workspace: project.rel(project.owner(file).root), language: /\.[cm]?tsx?$/i.test(file) ? 'typescript' : 'javascript' });
}
