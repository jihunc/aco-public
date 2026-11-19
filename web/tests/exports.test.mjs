import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {test} from 'node:test';
const req=createRequire(new URL('../package.json',import.meta.url)), ts=req('typescript');
function load(file,deps={}){const mod={exports:{}};const code=ts.transpileModule(readFileSync(new URL('../src/features/exports/'+file,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText;new Function('require','module','exports',code)(name=>name in deps?deps[name]:req(name),mod,mod.exports);return mod.exports;}
test('download sends exact selected window, current CSRF and same-origin, no retry or secret URL',async()=>{
 const api=load('api.ts'), previous=globalThis.fetch,calls=[];
 try {globalThis.fetch=async(url,options)=>{calls.push({url,options});return new Response('{"raw_originals_included":false}',{headers:{'content-type':'application/json','x-content-sha256':'a'.repeat(64)}});};
 const command={resources:['invoices'],created_from:'2026-11-01T00:00:00Z',created_before:'2026-11-20T00:00:00Z'};
 const file=await api.downloadExport('workspace',command,'csrf');
 assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/workspaces/workspace/exports');assert.equal(calls[0].options.headers['x-csrf-token'],'csrf');assert.equal(calls[0].options.credentials,'same-origin');assert.equal(calls[0].options.cache,'no-store');assert.deepEqual(JSON.parse(calls[0].options.body),command);assert.equal(file.sha256,'a'.repeat(64));assert.match(await file.blob.text(),/raw_originals_included/);
 }finally{globalThis.fetch=previous;}
});
test('overflow and revoked permission never return a partial file or retry',async()=>{
 const api=load('api.ts'),previous=globalThis.fetch;
 try{for(const [status,pattern] of [[413,/narrow.*no file/i],[403,/current.*Owner.*Finance/i]]){let calls=0;globalThis.fetch=async()=>{calls++;return new Response('{}',{status});};await assert.rejects(api.downloadExport('workspace',{resources:['invoices'],created_from:'a',created_before:'b'},'csrf'),pattern);assert.equal(calls,1);}}
 finally{globalThis.fetch=previous;}
});
test('panel presents all categories, explicit download and original privacy exclusions without mount request',()=>{
 const React=req('react'),{renderToStaticMarkup}=req('react-dom/server');let calls=0;
 const {ExportPanel}=load('panel.tsx',{'./api':{...load('api.ts'),downloadExport:()=>{calls++;throw Error('mount fetch');}}});
 const html=renderToStaticMarkup(React.createElement(ExportPanel,{workspace:'id',csrfToken:'csrf'}));
 assert.equal(calls,0);assert.equal((html.match(/type="checkbox"/g)||[]).length,16);assert.match(html,/Download business export/);assert.match(html,/Original bytes.*separately authorized private downloads/);assert.match(html,/5,000/);assert.match(html,/Current projections/);assert.doesNotMatch(html,/csrf|signed_url|api_key/);
});

test('workspace-keyed export and accounting siblings have distinct identities during company switches',()=>{
 const React=req('react');
 const workspace={id:'selected-workspace',name:'Company',role:'owner',permissions:['settings','export'],timezone:'UTC'};
 const {Setup}=load('../onboarding/setup.tsx',{
  react:{...React,useState:value=>[value,()=>{}]},
  '@tanstack/react-query':{useQueryClient:()=>({}),useQuery:()=>({data:{email:'owner@example.test',csrf_token:'private',workspaces:[workspace]}})},
  './demo-accounting':{DemoAccountingSetup:()=>null},'./members':{MembersPanel:()=>null},'../exports/panel':{ExportPanel:()=>null},
 });
 const rendered=Setup();const section=rendered.props.children.find(child=>child?.type==='section');
 const keys=section.props.children.filter(child=>child?.key!=null).map(child=>child.key);
 assert.ok(keys.length>=2);assert.equal(new Set(keys).size,keys.length,'Company-specific panels must not share sibling keys');
});
