import type {NextRequest} from 'next/server';

// Fixed server upstream, no URL supplied by the browser is fetch authority.
const REQUEST_TIMEOUT_MS = 15000;
// The first OIDC code exchange on a cold identity provider can exceed the ordinary bound.
const AUTH_TIMEOUT_MS = 60000;
const AUTH_EXCHANGE = /^\/api\/auth\/(?:login|callback)$/;

async function proxy(request: NextRequest) {
  const incoming = new URL(request.url);
  const base = process.env.ACO_API_INTERNAL_ORIGIN ?? 'http://127.0.0.1:58000';
  const target = new URL(incoming.pathname + incoming.search, base);
  const uuid = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
  const streaming = request.method === 'GET' && new RegExp(
    `^/api/workspaces/${uuid}/agent-runs/${uuid}/events$`, 'i',
  ).test(incoming.pathname);
  const signedIngress = request.method === 'POST' && new RegExp(
    `^/api/(?:email-ingress|email-webhooks)/${uuid}$`, 'i',
  ).test(incoming.pathname);
  const headers = new Headers();
  for (const key of ['cookie', 'content-type', 'origin', 'x-csrf-token',
    ...(streaming ? ['accept', 'last-event-id'] : []),
    ...(signedIngress ? ['svix-id', 'svix-timestamp', 'svix-signature'] : [])]) {
    const value = request.headers.get(key);
    if (value) headers.set(key, value);
  }
  // Bound stream connection establishment, then retain the browser's lifetime.
  // Ordinary requests keep the existing timeout for the entire upstream body.
  const headerDeadline = new AbortController();
  const headerTimer = streaming ? setTimeout(() => headerDeadline.abort(), 15000) : undefined;
  const bound = AUTH_EXCHANGE.test(incoming.pathname) ? AUTH_TIMEOUT_MS : REQUEST_TIMEOUT_MS;
  const signal = AbortSignal.any([request.signal,
    streaming ? headerDeadline.signal : AbortSignal.timeout(bound)]);
  try {
    const upstream = await fetch(target, {method:request.method, headers,
      body: ['GET','HEAD'].includes(request.method) ? undefined : await request.arrayBuffer(),
      redirect:'manual', cache:'no-store', signal});
    const output = new Headers({'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer'});
    if (streaming && upstream.headers.get('content-type')?.startsWith('text/event-stream')) {
      output.set('X-Accel-Buffering', 'no');
    }
    for (const key of ['content-type','location','content-disposition','x-content-type-options','content-security-policy','x-content-sha256']) {
      const value = upstream.headers.get(key);
      if (value) output.set(key,value);
    }
    for (const cookie of upstream.headers.getSetCookie()) output.append('set-cookie',cookie);
    return new Response(upstream.body,{status:upstream.status,headers:output});
  } catch {
    // A top-level browser navigation (sign-in redirects) gets a readable page, never raw JSON.
    if (request.method === 'GET' && request.headers.get('accept')?.includes('text/html')) {
      return new Response(null, {status:303, headers:{location:'/login?error=unavailable', 'Cache-Control':'no-store'}});
    }
    return Response.json({detail:'Service unavailable. Try again.'},{status:503});
  } finally {
    if (headerTimer !== undefined) clearTimeout(headerTimer);
  }
}
export {proxy as GET, proxy as POST, proxy as PUT, proxy as PATCH, proxy as DELETE};
