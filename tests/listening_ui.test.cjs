const { test } = require('node:test');
const assert = require('node:assert/strict');
const { newSession, complete, exportData } = require('../src/recollect/listening/app.js');

const kit = { id: 'kit-12', comparisons: [
  { id: 'pair-1', stage: 'Voice', profiles: ['base', 'voice'] },
  { id: 'pair-2', stage: 'Pace', profiles: ['base', 'slow'] },
  { id: 'pair-3', stage: 'Long listen', profiles: ['base', 'grouped'] },
] };

test('sessions retain every pair and conceal identities behind a reversible A/B mapping', () => {
  const first = newSession(kit, () => 0);
  const second = newSession(kit, () => 0.999);
  assert.notEqual(first.id, second.id);
  for (const session of [first, second]) {
    assert.equal(session.kit_id, kit.id);
    assert.equal(session.trials.at(-1).pair_id, 'pair-3');
    assert.deepEqual(session.trials.map(t => t.pair_id).sort(), ['pair-1', 'pair-2', 'pair-3']);
    for (const trial of session.trials) {
      const pair = kit.comparisons.find(p => p.id === trial.pair_id);
      assert.deepEqual([...trial.order].sort(), [...pair.profiles].sort());
      assert.equal(trial.saved_at, null);
    }
  }
  assert.notDeepEqual(first.trials.map(t => t.order), second.trials.map(t => t.order));
});

test('completion requires both clips, six valid ratings and an explicit preference', () => {
  const trial = newSession(kit).trials[0];
  assert.equal(complete(trial), false);
  trial.listened = { A: 1, B: 1 };
  trial.ratings = { A: { Naturalness: 4, Clarity: 5, Comfort: 3 },
    B: { Naturalness: 3, Clarity: 4, Comfort: 5 } };
  assert.equal(complete(trial), false);
  trial.preference = 'tie';
  assert.equal(complete(trial), true);
  trial.ratings.A.Comfort = 0;
  assert.equal(complete(trial), false);
  trial.ratings.A.Comfort = 6;
  assert.equal(complete(trial), false);
});

test('export preserves earlier sessions, hidden assignments, reveals and partial feedback', () => {
  const sessions = [newSession(kit), newSession(kit)];
  sessions[0].trials[0].revealed_at = '2026-09-09T00:00:00Z';
  sessions[0].trials[0].notes = 'Less tiring';
  const result = JSON.parse(JSON.stringify(exportData(kit, sessions)));
  assert.deepEqual(result.kit, kit);
  assert.deepEqual(result.sessions, sessions);
  assert.equal(result.schema, 1);
});
