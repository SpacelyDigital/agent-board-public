// This mod observes cached provider figures. It never submits a prompt or
// accesses account credentials, transcript content, or the network.

const EARLIEST = 946684800;
const LATEST = 4102444800;
const MAX_RESET_DISTANCE = 8 * 86400;

function safeDirectory(value) {
  if (typeof value !== 'string' || value.length > 4096 || !value.startsWith('/') ||
      value.startsWith('//') || /[\u0000-\u001f\u007f\\]/.test(value)) return null;
  const trimmed = value.replace(/\/+$/, '');
  if (!trimmed || trimmed.split('/').some(part => part === '.' || part === '..')) return null;
  return trimmed;
}

function resetSeconds(value) {
  if (typeof value !== 'string' || value.length > 64) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(?:Z|([+-])(\d{2}):(\d{2}))$/.exec(value);
  if (!match) return null;
  const year = Number(match[1]), month = Number(match[2]), day = Number(match[3]);
  const hour = Number(match[4]), minute = Number(match[5]), second = Number(match[6]);
  if (month < 1 || month > 12 || day < 1 || day > new Date(Date.UTC(year, month, 0)).getUTCDate() ||
      hour > 23 || minute > 59 || second > 59 ||
      (match[7] && (Number(match[8]) > 14 || Number(match[9]) > 59 ||
        (Number(match[8]) === 14 && Number(match[9]) !== 0)))) return null;
  const milliseconds = Date.parse(value);
  return Number.isFinite(milliseconds) ? Math.floor(milliseconds / 1000) : null;
}

function quotaWindows(raw, observedAt) {
  if (!Array.isArray(raw) || raw.length > 16) return [];
  const windows = [];
  for (const [kind, id] of [['five_hour', 'five_hour'], ['seven_day', 'weekly']]) {
    const matches = raw.filter(item => item && typeof item === 'object' && item.kind === kind);
    if (matches.length !== 1) continue;
    const item = matches[0];
    const used = item.percentUsed;
    // The provider may report a percentage before it knows the reset time.
    const hasReset = item.resetsAt !== undefined;
    const reset = hasReset ? resetSeconds(item.resetsAt) : null;
    if (typeof used !== 'number' || !Number.isFinite(used) || used < 0 || used > 100 ||
        (hasReset && (reset === null || reset < EARLIEST || reset >= LATEST ||
          Math.abs(reset - observedAt) > MAX_RESET_DISTANCE))) continue;
    windows.push({id, used_basis_points: Math.round(used * 100), reset_at: reset});
  }
  return windows;
}

async function refuseLink($, path) {
  if (!(await $.fs.exists(path))) return false;
  const info = await $.fs.stat(path);
  return info.isLink || !['file', 'dir'].includes(info.kind);
}

async function publish($, raw, configuredDirectory, state) {
  const base = configuredDirectory || `${await $.env.get('HOME')}/.claude`;
  const root = safeDirectory(base);
  if (!root) return;
  const observedAtMs = Math.max(Math.floor(await $.clock.now()), state.lastMs + 1);
  const observedAt = observedAtMs / 1000;
  if (!Number.isSafeInteger(observedAtMs) || observedAt < EARLIEST || observedAt >= LATEST) return;
  state.lastMs = observedAtMs;
  const payload = JSON.stringify({provider: 'claude', observed_at_ms: observedAtMs,
    windows: quotaWindows(raw, observedAt)});
  const bytes = new TextEncoder().encode(payload);
  const hash = await crypto.subtle.digest('SHA-256', bytes);
  const sha256 = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
  const directory = `${root}/agent-board/usage`;
  const path = `${directory}/quota-v1-${state.slot++ % 2 === 0 ? 'a' : 'b'}.json`;
  // fs.write itself is not atomic. The reader verifies the complete payload's
  // checksum before accepting it and retains the other last verified slot.
  for (const candidate of [root, `${root}/agent-board`, directory, path]) {
    if (await refuseLink($, candidate)) return;
  }
  await $.fs.write(path, JSON.stringify({version: 1, payload, sha256}) + '\n');
}

async function queuedPublish($, raw, configuredDirectory, state) {
  const prior = state.pending;
  let complete;
  state.pending = new Promise(resolve => { complete = resolve; });
  try {
    await prior;
    await publish($, raw, configuredDirectory, state);
  } finally {
    complete();
  }
}

export function register(on, options) {
  const configuredDirectory = options?.claude_directory || '';
  const state = {slot: 0, lastMs: 0, pending: Promise.resolve()};

  on('session.start', async ($, e, next) => {
    const result = await next(e);
    try {
      const usage = await $.session.usage();
      await queuedPublish($, usage.rateLimits, configuredDirectory, state);
    } catch {
      // No raw provider error is written to logs or to the export.
    }
    return result;
  });

  on('session.measure', async ($, e, next) => {
    const result = await next(e);
    try {
      await queuedPublish($, e.rateLimits, configuredDirectory, state);
    } catch {
      // Incomplete writes are discarded by the reader, without blocking Claude.
    }
    return result;
  });

  on('session.end', async ($, e, next) => {
    const result = await next(e);
    if (e.reason === 'logout') {
      try {
        await queuedPublish($, [], configuredDirectory, state);
      } catch {
        // A missing logout write must never interfere with signing out.
      }
    }
    return result;
  });
}
