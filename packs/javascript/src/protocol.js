import { createHash } from 'node:crypto';

export const order = (a, b) => a < b ? -1 : a > b ? 1 : 0;
export const digest = value => createHash('sha256').update(JSON.stringify(value), 'utf8').digest('hex');

export class Evidence {
  constructor() { this.observations = new Map(); this.gaps = new Map(); }
  gap(code, scope) { this.gaps.set(JSON.stringify([code, scope]), { code, scope }); }
  add(observation) { this.observations.set(observation.key, observation); }
  edge(source, target, kind, phase, resolution) {
    const identity = ['npm', source, target, kind, phase, resolution];
    const key = `edge:1:${digest(identity)}`;
    const prior = this.observations.get(key);
    this.add({ key, value: (prior?.value ?? 0) + 1,
      label: `${source} ${kind} ${target} (${phase})`,
      attrs: { edge_schema: '1', ecosystem: 'npm', source, target, kind, phase, resolution } });
  }
  component(identity, kind, attrs = {}, state = '') {
    this.add({ key: `component:1:${digest(['npm', identity, kind])}`, value: 1,
      state, label: identity, attrs: { component_schema: '1', ecosystem: 'npm', identity, kind, ...attrs } });
  }
  result(subject) {
    const gaps = [...this.gaps.values()].sort((a,b) => order(JSON.stringify(a), JSON.stringify(b)));
    for (const gap of gaps) this.add({ key: `coverage:1:${digest([gap.code, gap.scope])}`,
      state: 'not-measured', label: `${gap.code}: ${gap.scope}`,
      attrs: { coverage_schema: '1', code: gap.code, scope: gap.scope } });
    return { observations: [...this.observations.values()].sort((a,b) => order(a.key, b.key)),
      summary: `${subject}; ${gaps.length ? `${gaps.length} coverage gaps, incomplete` : 'declared static scope measured'}`,
      coverage: { status: gaps.length ? 'incomplete' : 'complete', gaps } };
  }
}
