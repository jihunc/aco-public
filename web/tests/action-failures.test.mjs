import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import test from 'node:test';
import { libDouble } from "./lib-support.mjs";
const req=createRequire(new URL('../package.json',import.meta.url));
const ts=req('typescript'), React=req('react');
const {renderToStaticMarkup}=req('react-dom/server');
const {QueryClient,QueryClientProvider}=req('@tanstack/react-query');
function load(relative) {
  const js=ts.transpileModule(readFileSync(new URL(`../src/${relative}`,import.meta.url),'utf8'),{
    compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true},
  }).outputText;
  const mod={exports:{}};
  const resolve=name=> name==='next/link' ? ({href,children,...props})=>React.createElement('a',{href,...props},children)
    : name==='@/features/receivables/shared' ? {stamp:value=>value,ErrorState:({label})=>React.createElement('p',null,label)}
    : name==='@/lib/api' ? load('lib/api.ts')
    : ['./ingress','./provider-settings','./connections'].includes(name) ? {SyntheticInboundPanel:()=>null,ProviderEmailSettings:()=>null,CompanyConnections:()=>null}
    : name.startsWith('./') ? load(`features/communications/${name.slice(2)}.${name==='./action-failures'?'tsx':'ts'}`)
    : req(name);
  new Function('require','module','exports',js)((n)=>libDouble(n)??resolve(n),mod,mod.exports);
  return mod.exports;
}
const workspace={id:'company/a',permissions:['invoice_read'],outbound_paused:true};
const action=(status,error)=>({id:`action-${status}`,kind:'policy_due',invoice_id:'invoice/a',version:3,status,
  attempts:status==='dead_letter'?3:1,due_at:'2026-11-19T00:00:00Z',last_error:error,automatic_resend:false,
  operation_key:'retained-operation',job_id:19,executions:[{attempt:1,status:'failed',started_at:'2026-11-19T00:00:00Z',
    finished_at:'2026-11-19T00:00:01Z',retry_delay_seconds:5,provider_operation_id:null}]});
function render(rows){
 const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 client.setQueryData(['email-execution',workspace.id],{system_enabled:false,activation:null,mode:null,provider:null});
 for(const status of ['queued','unknown_outcome','failed','dead_letter'])
   client.setQueryData(['scheduled-actions',workspace.id,status],rows.filter(row=>row.status===status));
 const {EmailExecutionPanel}=load('features/communications/settings.tsx');
 return renderToStaticMarkup(React.createElement(QueryClientProvider,{client},React.createElement(EmailExecutionPanel,{workspace,csrfToken:'test-only'})));
}
test('stopped action evidence is visible without providing an automatic resend control',()=>{
 const html=render([action('failed','AUTH_EXPIRED'),action('dead_letter','RATE_LIMITED')]);
 assert.match(html,/AUTH_EXPIRED/);
 assert.match(html,/RATE_LIMITED/);
 assert.match(html,/Attempts: 3/);
 assert.match(html,/href="\/receivables\/invoice%2Fa\?workspace=company%2Fa"/);
 assert.match(html,/href="\/settings\/policies\?workspace=company%2Fa"/);
 assert.doesNotMatch(html,/<button[^>]*>[^<]*(Retry action|Resend|Run again)/i);
 assert.match(html,/No automatic resend/);
});
test('failure reads preserve workspace scope and status across action kinds and never mutate',async()=>{
 const {readActionFailures}=load('features/communications/action-failures.tsx');
 const original=globalThis.fetch, requests=[];
 globalThis.fetch=async(url,options)=>{requests.push({url,options});return {ok:true,status:200,json:async()=>[action('failed','AUTH_EXPIRED')]};};
 try {
  const result=await readActionFailures('company/a','failed');
  assert.equal(result[0].last_error,'AUTH_EXPIRED');
  await readActionFailures('company/a','dead_letter');
  assert.deepEqual(requests.map(r=>r.url),[
   '/api/workspaces/company%2Fa/scheduled-actions?status=failed',
   '/api/workspaces/company%2Fa/scheduled-actions?status=dead_letter',
  ]);
  assert.ok(requests.every(r=>r.options.credentials==='same-origin'&&r.options.cache==='no-store'&&!r.options.body&&!r.options.method));
  globalThis.fetch=async()=>({status:403,ok:false});
  await assert.rejects(readActionFailures('company/a','failed'),/current membership/);
 } finally{globalThis.fetch=original;}
});
test('empty failures and retained untrusted error text render honestly and inertly',()=>{
 const empty=render([]);
 assert.match(empty,/No failed actions/);assert.match(empty,/No exhausted retries/);
 assert.match(empty,/Latest 100 actions per status/);
 const html=render([action('failed','<script>bad()</script>')]);
 assert.doesNotMatch(html,/<script>/);assert.match(html,/&lt;script&gt;bad\(\)&lt;\/script&gt;/);
 assert.doesNotMatch(html,/<button[^>]*>[^<]*(Retry action|Resend|Run again)/i);
});
