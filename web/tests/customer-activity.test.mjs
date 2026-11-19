import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import test from 'node:test';
const req=createRequire(new URL('../package.json',import.meta.url));
const ts=req('typescript'),React=req('react'),{renderToStaticMarkup}=req('react-dom/server');
function load(){
 const js=ts.transpileModule(readFileSync(new URL('../src/features/receivables/customer-contact.tsx',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 const module={exports:{}};new Function('require','module','exports',js)(name=>name==='./shared'?{stamp:x=>x}:req(name),module,module.exports);return module.exports;
}
test('customer view distinguishes retained originals, exposure uncertainty and recorded state',()=>{
 const {CustomerContactActivity}=load();
 const html=renderToStaticMarkup(React.createElement(CustomerContactActivity,{interactions:{original_count:3,incoming_count:1,outgoing_count:2,window_days:30,as_of_at:'2026-11-19T01:00:00Z',window_start:'2026-10-20T01:00:00Z',groups:[{channel:'email',direction:'outgoing',state:'unknown_outcome',synthetic:true,count:1}]},frequency:{as_of_at:'2026-11-19T01:00:00Z',window_start:'2026-11-12T01:00:00Z',last_24_hours:2,last_7_days:4,states:{reserved:1,inflight:1,accepted:1,connected:0,unknown_outcome:1}}}));
 assert.match(html,/3 recorded originals/);assert.match(html,/1 incoming/);assert.match(html,/unknown outcome/);assert.match(html,/not proof of delivery/i);assert.match(html,/2026-11-19T01:00:00Z/);assert.doesNotMatch(html,/unavailable until/);
});
test('saved pending action is labeled as stored state, and no pending row is explicit',()=>{
 const {NextSavedAction}=load();const html=renderToStaticMarkup(React.createElement(NextSavedAction,{action:{availability:'saved_pending',id:'actual-id',due_at:'2026-11-20T01:00:00Z',kind:'agent_email',status:'awaiting_approval',channel:'email',eligibility:'not_evaluated'}}));
 assert.match(html,/awaiting approval/);assert.match(html,/eligibility is rechecked/i);assert.match(html,/actual-id/);
 const empty=renderToStaticMarkup(React.createElement(NextSavedAction,{action:{availability:'none_scheduled',id:null,due_at:null,kind:null,status:null,channel:null,eligibility:'not_evaluated'}}));assert.match(empty,/No saved pending action/);assert.doesNotMatch(empty,/unavailable/);
});
