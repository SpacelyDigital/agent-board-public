// Executes the actual mod in a fake provider host. It makes no provider calls.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const home = process.argv[2];
assert(home && path.isAbsolute(home));
const root = path.join(home, '.claude');
await fs.mkdir(root, {recursive: true});
const source = await fs.readFile(new URL('../integrations/claude-local-usage/hooks/register.js', import.meta.url), 'utf8');
const {register} = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const events = new Map();
const writes = [];
const now = 1793872800000;
let usageCalls = 0;
let failWrite = false;
const figures = [
  {kind: 'five_hour', percentUsed: 25, resetsAt: new Date(now + 3600000).toISOString(), token: 'fake-private-token'},
  {kind: 'seven_day', percentUsed: 40, resetsAt: new Date(now + 6 * 86400000).toISOString()},
  {kind: 'spend_limit', percentUsed: 11, resetsAt: new Date(now + 86400000).toISOString()}
];

const no = () => { throw new Error('An unapproved fake-host operation was attempted'); };
globalThis.fetch = no;
const confined = value => {
  assert(value === root || value.startsWith(root + '/'));
  return value;
};
const host = {
  env: {get: async name => { assert.equal(name, 'HOME'); return home; }},
  clock: {now: async () => now},
  session: {usage: async () => { usageCalls++; return {rateLimits: figures, chats: 'fixture-message', account: 'fixture-account'}; }},
  fs: {
    exists: async value => { try { await fs.lstat(confined(value)); return true; } catch (error) { if (error.code === 'ENOENT') return false; throw error; } },
    stat: async value => { const item = await fs.lstat(confined(value)); return {kind: item.isDirectory() ? 'dir' : item.isFile() ? 'file' : 'other', isLink: item.isSymbolicLink()}; },
    write: async (value, text) => {
      confined(value);
      if (failWrite) { failWrite = false; throw new Error('fixture-only write error'); }
      const envelope = JSON.parse(text);
      const payload = JSON.parse(envelope.payload);
      assert.deepEqual(Object.keys(payload), ['provider', 'observed_at_ms', 'windows']);
      assert(!/fixture-message|fixture-account|fake-private-token|spend_limit/.test(text));
      writes.push({path: value, payload, text});
      await fs.mkdir(path.dirname(value), {recursive: true});
      await fs.writeFile(value, text, {mode: 0o600});
    }
  }
};
const $ = new Proxy(host, {get: (target, key) => key in target ? target[key] : no()});
register((event, callback) => { assert(!events.has(event)); events.set(event, callback); }, {});
assert.deepEqual([...events.keys()], ['session.start', 'session.measure', 'session.end']);
let nextCalls = 0;
const next = async event => { nextCalls++; return {same: event}; };
const start = {session_id: 'not-exported'};
assert.equal((await events.get('session.start')($, start, next)).same, start);
assert.equal(usageCalls, 1);

const event = {rateLimits: figures, changed: ['rateLimits'], text: 'fixture-message', token: 'fake-private-token'};
const eventBefore = JSON.stringify(event);
await Promise.all(Array.from({length: 30}, () => events.get('session.measure')($, event, next)));
assert.equal(nextCalls, 31);
assert.equal(JSON.stringify(event), eventBefore);
for (let index = 1; index < writes.length; index++) {
  assert(writes[index].payload.observed_at_ms > writes[index - 1].payload.observed_at_ms);
}
assert.equal(new Set(writes.map(item => item.path)).size, 2);
failWrite = true;
await events.get('session.measure')($, event, next);
await events.get('session.end')($, {reason: 'logout'}, next);
assert.deepEqual(writes.at(-1).payload.windows, []);
await events.get('session.measure')($, event, next);
await events.get('session.measure')($, {rateLimits: []}, next);
assert.deepEqual(writes.at(-1).payload.windows, []);
for (const rateLimits of [null, {}, new Array(17).fill(figures[0]), [{...figures[0], percentUsed: Infinity}],
  [{...figures[0], resetsAt: null}], [{...figures[0], resetsAt: 1793876400}],
  [{...figures[0], resetsAt: '2026-11-06T00:00:00'}],
  [{...figures[0], resetsAt: '2026-11-31T00:00:00Z'}],
  [{...figures[0], resetsAt: '2026-11-06T24:00:00Z'}],
  [{...figures[0], resetsAt: '2026-11-06T00:00:00+14:01'}]]) {
  await events.get('session.measure')($, {rateLimits}, next);
  assert.deepEqual(writes.at(-1).payload.windows, []);
}
const withoutReset = {kind: 'five_hour', percentUsed: 68};
await events.get('session.measure')($, {rateLimits: [withoutReset, figures[1]]}, next);
assert.deepEqual(writes.at(-1).payload.windows, [
  {id: 'five_hour', used_basis_points: 6800, reset_at: null},
  {id: 'weekly', used_basis_points: 4000, reset_at: Math.floor((now + 6 * 86400000) / 1000)}
]);
await events.get('session.measure')($, {rateLimits: [{kind: 'seven_day', percentUsed: 68,
  resetsAt: new Date(now + 4 * 3600000).toISOString()}]}, next);
assert.deepEqual(writes.at(-1).payload.windows, [
  {id: 'weekly', used_basis_points: 6800, reset_at: Math.floor((now + 4 * 3600000) / 1000)}
]);
await events.get('session.measure')($, {rateLimits: [withoutReset, withoutReset]}, next);
assert.deepEqual(writes.at(-1).payload.windows, []);
await events.get('session.measure')($, event, next);
const contents = await fs.readdir(path.join(root, 'agent-board/usage'));
assert.deepEqual(contents.sort(), ['quota-v1-a.json', 'quota-v1-b.json']);
assert.equal(usageCalls, 1);
process.stdout.write(JSON.stringify({state: 'passed', writes: writes.length, nextCalls, providerCalls: 0, networkCalls: 0}));
