import path from 'node:path';
import { isBuiltin } from 'node:module';
import ts from 'typescript';

const isLiteral = node => ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node);
const packageName = specifier => specifier.startsWith('@') ? specifier.split('/').slice(0,2).join('/') : specifier.split('/')[0];

export function measureImports(project, evidence) {
  const pending = [...project.sources];
  const queued = new Set(pending);
  for (let index = 0; index < pending.length; index++) {
    const file = pending[index];
    const text = project.read(file); if (text === null) continue;
    const ast = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
    const source = `file:${project.rel(file)}`;
    if (ast.parseDiagnostics.length) { evidence.gap('parse-error', project.rel(file)); continue; }
    const owner = project.owner(file);
    const config = project.configRaw.get(file) ?? project.config(project.nearestConfig(file));
    const scopes = new Set();
    const bindingContainsRequire = name => {
      if (!name) return false;
      if (ts.isIdentifier(name)) return name.text === 'require';
      return (ts.isObjectBindingPattern(name) || ts.isArrayBindingPattern(name)) && name.elements.some(item => ts.isBindingElement(item) && bindingContainsRequire(item.name));
    };
    const bindings = node => {
      if ((ts.isVariableDeclaration(node) || ts.isParameter(node) || ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node) || ts.isClassDeclaration(node) || ts.isClassExpression(node) || ts.isImportClause(node) || ts.isNamespaceImport(node) || ts.isImportSpecifier(node) || ts.isImportEqualsDeclaration(node)) && bindingContainsRequire(node.name)) scopes.add('require');
      if (ts.isBinaryExpression(node) && ts.isIdentifier(node.left) && node.left.text === 'require' && node.operatorToken.kind >= ts.SyntaxKind.FirstAssignment && node.operatorToken.kind <= ts.SyntaxKind.LastAssignment) scopes.add('require');
      ts.forEachChild(node, bindings);
    };
    bindings(ast);
    const emit = (specifier, kind, phase) => {
      if (/\.d\.[cm]?ts$/i.test(file)) phase = 'type';
      const resolved = resolveImport(project, file, owner, config.options, specifier, kind, phase, evidence);
      evidence.edge(source, resolved.target, kind, phase, resolved.resolution);
      // Config excludes select roots; statically reached source still belongs to
      // the measured graph, matching TypeScript's imported-file behavior.
      if (resolved.file && /\.(?:[cm]?[jt]sx?)$/i.test(resolved.file) && !queued.has(resolved.file)) {
        queued.add(resolved.file); pending.push(resolved.file);
      }
    };
    const dynamic = kind => {
      evidence.edge(source, `dynamic:${kind}`, kind, 'runtime', 'dynamic');
      evidence.gap('dynamic-specifier', project.rel(file));
    };
    const phases = (clause, exportClause = false) => {
      if (clause?.isTypeOnly) return ['type'];
      const named = exportClause ? clause?.exportClause : clause?.namedBindings;
      const elements = named && (ts.isNamedImports(named) || ts.isNamedExports(named)) ? named.elements : null;
      if (!elements) return ['runtime'];
      const result = [];
      if (elements.some(item => item.isTypeOnly)) result.push('type');
      if ((!exportClause && clause?.name) || !elements.length || elements.some(item => !item.isTypeOnly)) result.push('runtime');
      return result;
    };
    const seen = new WeakSet();
    const visit = node => {
      if (seen.has(node)) return; seen.add(node);
      if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
        for (const phase of phases(node.importClause)) emit(node.moduleSpecifier.text, 'import', phase);
      } else if (ts.isExportDeclaration(node) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
        for (const phase of phases(node, true)) emit(node.moduleSpecifier.text, 'reexport', phase);
      } else if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference)) {
        const expression = node.moduleReference.expression;
        if (expression && isLiteral(expression)) emit(expression.text, 'require', node.isTypeOnly ? 'type' : 'runtime');
        else dynamic('require');
      } else if (ts.isImportTypeNode(node)) {
        if (ts.isLiteralTypeNode(node.argument) && isLiteral(node.argument.literal)) emit(node.argument.literal.text, 'import', 'type');
        else evidence.gap('dynamic-type-import', project.rel(file));
      } else if (ts.isJSDocImportTag?.(node)) {
        if (ts.isStringLiteral(node.moduleSpecifier)) emit(node.moduleSpecifier.text, 'import', 'type');
      } else if (ts.isCallExpression(node)) {
        const isDynamicImport = node.expression.kind === ts.SyntaxKind.ImportKeyword;
        const isRequire = ts.isIdentifier(node.expression) && node.expression.text === 'require';
        if (isDynamicImport || isRequire) {
          if (isRequire && scopes.has('require')) evidence.gap('shadowed-require', project.rel(file));
          else if (node.arguments.length >= 1 && isLiteral(node.arguments[0])) emit(node.arguments[0].text, isDynamicImport ? 'dynamic-import' : 'require', 'runtime');
          else dynamic(isDynamicImport ? 'dynamic-import' : 'require');
        } else if (ts.isPropertyAccessExpression(node.expression) && ((ts.isIdentifier(node.expression.expression) && ['require','module'].includes(node.expression.expression.text)) && ['resolve','require'].includes(node.expression.name.text))) {
          evidence.gap('unsupported-loader-call', project.rel(file));
        }
      }
      ts.forEachChild(node, visit);
      for (const doc of node.jsDoc ?? []) visit(doc);
    };
    visit(ast);
  }
}

function resolveImport(project, file, owner, options, specifier, kind, phase, evidence) {
  const unresolved = (code = 'unresolved-import') => {
    evidence.gap(code, project.rel(file));
    return { target: `unresolved:${specifier}`, resolution: 'unresolved' };
  };
  if (!specifier || specifier.length > 1000 || /[\x00-\x1f]/.test(specifier)) {
    evidence.gap('invalid-specifier', project.rel(file)); return { target: 'unresolved:invalid-specifier', resolution: 'unresolved' };
  }
  if (/^[a-z][a-z0-9+.-]*:/i.test(specifier) && !specifier.startsWith('node:')) {
    evidence.gap('unsupported-url-import', project.rel(file)); return { target: 'unresolved:url', resolution: 'unresolved' };
  }
  if ((specifier.startsWith('.') || path.isAbsolute(specifier)) && !project.contained(path.resolve(path.dirname(file), specifier))) {
    evidence.gap('outside-import', project.rel(file)); return { target: 'unresolved:outside-project', resolution: 'unresolved' };
  }
  const builtin = specifier.replace(/^node:/, '');
  if (isBuiltin(specifier)) return { target: `builtin:${builtin}`, resolution: 'builtin' };
  if (specifier.startsWith('node:')) return unresolved('unknown-builtin');
  const host = {
    fileExists: candidate => project.readable(candidate),
    readFile: candidate => project.readable(candidate) ? project.read(candidate) ?? undefined : undefined,
    directoryExists: candidate => project.contained(candidate) && project.dirSet.has(path.resolve(candidate)),
    getCurrentDirectory: () => project.root,
    realpath: candidate => candidate,
    useCaseSensitiveFileNames: () => true,
  };
  if (specifier.startsWith('#')) return unresolved('unsupported-package-imports');
  const namedWorkspace = project.packages.find(pkg => pkg.name === packageName(specifier));
  const exports = namedWorkspace?.manifest.exports;
  if (exports && typeof exports === 'object') {
    const subpath = specifier.slice(namedWorkspace.name.length);
    const selected = exports[subpath ? `.${subpath}` : '.'];
    if (typeof selected !== 'string') return unresolved('unsupported-workspace-exports');
  }
  const nodeMode = [ts.ModuleResolutionKind.Node16, ts.ModuleResolutionKind.NodeNext].includes(options.moduleResolution);
  const commonJSFile = /\.[cm][jt]s$/i.test(file) ? /\.c[jt]s$/i.test(file) : owner.manifest.type !== 'module';
  const mode = kind === 'require' || (kind !== 'dynamic-import' && nodeMode && commonJSFile) ? ts.ModuleKind.CommonJS : ts.ModuleKind.ESNext;
  const resolved = ts.resolveModuleName(specifier, file, options, host, undefined, undefined, mode).resolvedModule;
  const local = candidate => {
    if (!project.readable(candidate) || !project.owner(candidate)) return unresolved('outside-selected-source');
    return { file: candidate, target: `file:${project.rel(candidate)}`, resolution: project.owner(candidate).root === owner.root ? 'local' : 'workspace' };
  };
  if (resolved) return local(resolved.resolvedFileName);
  if (specifier.startsWith('.') || specifier.startsWith('/') || specifier.startsWith('#')) return unresolved();
  const name = packageName(specifier);
  const workspaces = project.packages.filter(pkg => pkg.name === name);
  if (workspaces.length > 1) return unresolved('duplicate-workspace-name');
  if (workspaces.length) {
    const workspace = workspaces[0];
    const subpath = specifier.slice(name.length);
    const exports = workspace.manifest.exports;
    let entry;
    if (exports !== undefined) {
      if (typeof exports === 'string' && !subpath) entry = exports;
      else if (exports && typeof exports === 'object' && !Array.isArray(exports)) entry = exports[subpath ? `.${subpath}` : '.'];
      if (typeof entry !== 'string' || !entry.startsWith('./')) return unresolved('unsupported-workspace-exports');
    } else if (subpath) entry = `.${subpath}`;
    else if (phase === 'type' && (workspace.manifest.types || workspace.manifest.typings)) entry = workspace.manifest.types ?? workspace.manifest.typings;
    else if (workspace.manifest.browser !== undefined || workspace.manifest.module !== undefined) return unresolved('unsupported-workspace-entry');
    else entry = workspace.manifest.main ?? './index';
    if (typeof entry !== 'string') return unresolved('unsupported-workspace-entry');
    const target = path.resolve(workspace.root, entry);
    if (!target.startsWith(workspace.root + path.sep) || !project.contained(target)) return unresolved('outside-workspace-entry');
    const relative = './' + path.relative(path.dirname(file), target).split(path.sep).join('/');
    const found = ts.resolveModuleName(relative, file, options, host, undefined, undefined, mode).resolvedModule;
    return found ? local(found.resolvedFileName) : unresolved('unresolved-workspace-entry');
  }
  // Source declarations identify an external relationship, not an installed version.
  const declarations = ['dependencies','devDependencies','peerDependencies','optionalDependencies'];
  if (declarations.some(key => owner.manifest[key] && Object.hasOwn(owner.manifest[key], name))) return { target: `package:${specifier}`, resolution: 'external' };
  return unresolved('undeclared-external-import');
}
