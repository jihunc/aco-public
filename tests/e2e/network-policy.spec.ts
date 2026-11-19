import { expect, test } from '@playwright/test';
import { createServer } from 'node:http';
import { connect } from 'node:net';

test('health UI reads real API state and never fabricates worker readiness', async ({page, request}) => {
  const live = await request.get('http://127.0.0.1:58000/health/live');
  expect(await live.json()).toEqual({status: 'alive'});
  const ready = await request.get('http://127.0.0.1:58000/health/ready');
  expect(ready.status()).toBe(503);
  expect((await ready.json()).checks).toEqual({database: 'READY', storage: 'READY', identity: 'READY', worker: 'UNAVAILABLE'});
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Not ready', exact: true})).toBeVisible();
  await expect(page.getByText('UNAVAILABLE', {exact: true})).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', {name: 'Check again'})).toBeFocused();
  const refreshed = page.waitForResponse(response => response.url().endsWith('/api/health'));
  await page.keyboard.press('Enter');
  const refreshResponse = await refreshed;
  expect(refreshResponse.status()).toBe(503);
  expect((await refreshResponse.json()).checks.worker).toBe('UNAVAILABLE');
  await expect(page.getByRole('heading', {name: 'Not ready', exact: true})).toBeVisible();
});

test('actual browser and Node cannot reach an unapproved listening loopback port', async ({page, browser}) => {
  let requests = 0;
  const server = createServer((_req, res) => {requests++; res.end('Synthetic forbidden destination');});
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Missing listener');
  try {
    const nodeCode = await new Promise<string>(resolve => {
      const socket = connect({host: '127.0.0.1', port: address.port});
      socket.on('error', error => resolve((error as NodeJS.ErrnoException).code ?? 'UNKNOWN'));
      socket.on('connect', () => {socket.destroy(); resolve('ESCAPED');});
    });
    expect(['EPERM', 'EACCES', 'EHOSTUNREACH']).toContain(nodeCode);
    // Deliberately no page.route: Chromium's native network process must be denied.
    await expect(page.goto(`http://127.0.0.1:${address.port}/synthetic`, {timeout: 5000})).rejects.toThrow(/net::ERR_/);
    expect(browser.isConnected()).toBe(true);
    expect(requests).toBe(0);
  } finally {
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  }
});
