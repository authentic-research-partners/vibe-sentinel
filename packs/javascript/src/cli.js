import { Evidence } from './protocol.js';
import { Project, measureWorkspaces } from './project.js';
import { measureImports } from './imports.js';

export function main(args) {
  if (args.length === 1 && ['--help', '-h'].includes(args[0])) {
    process.stdout.write('Usage: vibe-sentinel-javascript workspaces|imports --root PATH\n\nworkspaces  Measure declared workspace and source membership.\nimports     Measure static JS/TS relationships with explicit coverage.\n--root PATH Project directory containing package.json.\n--help      Show this help without inspecting a project.\n\nExit 0: supported static scope measured. Exit 2: failed/incomplete coverage.\n');
    return;
  }
  const evidence = new Evidence();
  try {
    if (Number(process.versions.node.split('.')[0]) < 22) throw new Error('Node 22 or newer is required');
    if (args.length !== 3 || !['workspaces','imports'].includes(args[0]) || args[1] !== '--root' || !args[2]) throw new Error('Usage: vibe-sentinel-javascript workspaces|imports --root PATH');
    const project = new Project(args[2], evidence, { useConfigs: args[0] === 'imports' });
    if (args[0] === 'workspaces') measureWorkspaces(project, evidence);
    else measureImports(project, evidence);
    const result = evidence.result(`${project.packages.length} workspaces, ${project.sources.length} source roots`);
    process.stdout.write(JSON.stringify(result) + '\n');
    if (result.coverage.status !== 'complete') {
      const sample = result.coverage.gaps.slice(0, 2).map(gap => ({ code: gap.code, scope: gap.scope.slice(0, 70) }));
      process.stderr.write(`Incomplete JavaScript coverage: ${JSON.stringify(sample)}. From the measured project run vibe-sentinel-javascript ${args[0]} --root . for full JSON coverage.\n`);
      process.exitCode = 2;
    }
  } catch {
    process.stderr.write('JavaScript reader failed. Use workspaces|imports --root PATH with a readable project package.json and Node 22 or newer.\n');
    process.exitCode = 2;
  }
}
