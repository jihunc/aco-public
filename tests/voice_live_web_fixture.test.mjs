import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, access } from 'node:fs/promises';
import { resolve } from 'node:path';
import { prepareProject } from './serve_voice_live_web.mjs';
test('standard test Next project retains actual source and owns only its temporary root', async () => {
  process.env.ACO_TEST_PROFILE = 'voice-live-code';
  const owned = await prepareProject();
  try {
    assert.match(owned.project, /\.tooling\/voice-live-web-[A-Za-z0-9]+$/);
    assert.equal(await readFile(resolve(owned.project, 'src/features/conversations/voice-browser.ts'), 'utf8'),
      await readFile('web/src/features/conversations/voice-browser.ts', 'utf8'));
    assert.match(await readFile(resolve(owned.project, 'next.config.mjs'), 'utf8'), /@daily-co\/daily-js/);
    assert.doesNotMatch(await readFile('web/next.config.mjs', 'utf8'), /browser_voice_daily/);
  } finally { await owned.cleanup(); }
  await assert.rejects(access(owned.project));
  process.env.ACO_TEST_PROFILE = 'mock';
  await assert.rejects(prepareProject(), /Explicit contained/);
});
