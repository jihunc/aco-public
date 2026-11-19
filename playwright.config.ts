import { defineConfig } from '@playwright/test';
import { execFileSync } from 'node:child_process';
const profile = process.env.ACO_TEST_PROFILE ?? 'mock';
if (!['mock', 'synthetic-reminders', 'synthetic-agents', 'synthetic-inbound', 'synthetic-voice', 'voice-live-code', 'synthetic-agent-email', 'deployed'].includes(profile)) throw new Error('Unknown test profile; paid-provider activation is forbidden');
const deployed = profile === 'deployed';
const syntheticReminders = profile === 'synthetic-reminders';
const syntheticAgents = profile === 'synthetic-agents';
const syntheticInbound = profile === 'synthetic-inbound';
const syntheticAgentEmail = profile === 'synthetic-agent-email';
const syntheticVoice = profile === 'synthetic-voice';
const voiceLiveCode = profile === 'voice-live-code';
const runtimeEnv = {
  // Every local browser lane remains fake even when the operator selected a live grant.
  ACO_AGENT_LIVE_GRANT_ID: "",
  ACO_AGENT_LIVE_CONFIG_PATH: "",
  ACO_VOICE_LIVE_CONFIG_PATH: "",
  ACO_LIVE_AUTHORIZATION_REF: "",
  ACO_LIVE_CAMPAIGN_FILE: "",
  ACO_LIVE_ALLOCATION_ID: "",
  ACO_EMAIL_LIVE_CONFIG_PATH: "",
  SYNTHETIC_VOICE_ENABLED: syntheticVoice ? "true" : "false",
  SYNTHETIC_EMAIL_INGRESS_ENABLED: (syntheticInbound || syntheticAgentEmail) ? "true" : "false",
  SYNTHETIC_EMAIL_INGRESS_WEBHOOK_SECRET: (syntheticInbound || syntheticAgentEmail) ? "whsec_c3ludGhldGljLWluYm91bmQtb2ZmbGluZS10ZXN0LWtleS1vbmx5" : "",
  SYNTHETIC_EMAIL_ENABLED: (syntheticReminders || syntheticAgentEmail) ? 'true' : 'false',
  OUTBOUND_ENABLED: (syntheticReminders || syntheticAgentEmail) ? 'true' : 'false',
  AUTOMATION_KILL_SWITCH: (syntheticReminders || syntheticAgentEmail) ? 'false' : 'true',
};
if (!deployed) {
  // Safe loopback-only native check. A wrapper flag cannot establish containment.
  // This is a fail-closed check; scripts/test-e2e/test-portable establish policy
  // before Node/plugin startup, while bare CLI is never claimed equivalent.
  try {
    execFileSync(process.execPath, ['-e', `
      const s = require('node:net').connect({host:'127.0.0.1',port:9});
      s.on('error', e => process.exit(['EPERM','EACCES','EHOSTUNREACH'].includes(e.code) ? 0 : 2));
      s.on('connect', () => {s.destroy();process.exit(3);});
      setTimeout(() => process.exit(4), 2000);
    `], {stdio: 'pipe', timeout: 3000});
  } catch {
    throw new Error('Mock Playwright requires inherited kernel containment; use scripts/test-e2e or scripts/test-portable');
  }
}

const origin = process.env.ACO_DEPLOYED_ORIGIN;
if (deployed && (!origin || !origin.startsWith('https://'))) throw new Error('Deployed smoke requires explicit HTTPS origin');
export default defineConfig({
  testDir: './tests/e2e', fullyParallel: false, workers: 1, retries: 0,
  use: {baseURL: deployed ? origin : 'http://127.0.0.1:53000',
    launchOptions: {executablePath: process.platform === 'darwin'
      ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : '/usr/bin/chromium',
      args: process.platform === 'linux' ? ['--no-sandbox'] : []}},
  projects: deployed ? [{name: 'deployed-smoke', testMatch: '**/deployed-smoke.spec.ts'}]
                    : syntheticAgentEmail
                      ? [{name: 'synthetic-agent-email', testMatch: '**/agent-email.spec.ts'}]
                    : syntheticReminders
                      ? [{name: 'synthetic-reminders', testMatch: '**/reminder-execution.spec.ts'}]
                      : syntheticAgents
                        ? [{name: 'synthetic-agents', testMatch: ['**/agent-stream.spec.ts', '**/agent-approval.spec.ts', '**/demo-reset.spec.ts', '**/provider-connections.spec.ts']}]
                      : syntheticVoice
                          ? [{name: 'synthetic-voice', testMatch: '**/voice.spec.ts'}]
                      : voiceLiveCode
                          ? [{name: 'voice-live-code', testMatch: '**/voice-live-code.spec.ts'}]
                      : syntheticInbound
                          ? [{name: 'synthetic-inbound', testMatch: ['**/inbound.spec.ts', '**/promise-performance.spec.ts', '**/promise-multi.spec.ts', '**/promise-installments.spec.ts', '**/cases.spec.ts', '**/case-approval.spec.ts', '**/contact-stop.spec.ts', '**/three-intent.spec.ts']}]
                          : [{name: 'mock', testMatch: ['**/network-policy.spec.ts', '**/onboarding.spec.ts', '**/demo-onboarding.spec.ts', '**/business-export.spec.ts', '**/receivables.spec.ts', '**/usage.spec.ts', '**/policy.spec.ts', '**/review.spec.ts', '**/reminder.spec.ts', '**/approvals.spec.ts', '**/payment-source.spec.ts', '**/impact.spec.ts', '**/cohorts.spec.ts', '**/economics.spec.ts', '**/observed-cohorts.spec.ts']}],
  webServer: deployed ? undefined : [
    {command: voiceLiveCode ? '.venv/bin/python tests/serve_voice_live_browser.py' : '.venv/bin/python scripts/serve-health.py', env: runtimeEnv, url: 'http://127.0.0.1:58000/health/live', reuseExistingServer: false,
      ...(voiceLiveCode ? {gracefulShutdown: {signal: 'SIGTERM' as const, timeout: 10000}} : {})},
    ...(syntheticReminders || syntheticAgents || syntheticInbound || syntheticVoice || syntheticAgentEmail ? [{command: '.venv/bin/python scripts/run-worker.py', env: runtimeEnv,
      url: 'http://127.0.0.1:58000/health/ready', reuseExistingServer: false,
      gracefulShutdown: {signal: 'SIGTERM' as const, timeout: 5000}}] : []),
    {command: voiceLiveCode ? 'node ../tests/serve_voice_live_web.mjs' : 'node node_modules/next/dist/bin/next dev --webpack --hostname 127.0.0.1 --port 53000',
      cwd: './web', env: {NEXT_TELEMETRY_DISABLED: '1'}, url: 'http://127.0.0.1:53000', reuseExistingServer: false, timeout: 120000,
      ...(voiceLiveCode ? {gracefulShutdown: {signal: 'SIGTERM' as const, timeout: 10000}} : {})},
  ],
});
