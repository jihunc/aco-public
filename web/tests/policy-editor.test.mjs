import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import test from 'node:test';
const req=createRequire(new URL('../package.json',import.meta.url));
const React=req('react'),ts=req('typescript');
const {renderToStaticMarkup}=req('react-dom/server');
const {QueryClient,QueryClientProvider}=req('@tanstack/react-query');
function loadEditor(){
 const js=ts.transpileModule(readFileSync(new URL('../src/features/policies/editor.tsx',import.meta.url),'utf8'),{
  compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true},
 }).outputText;
 const mod={exports:{}};
 const local=name=>name==='@/components/ui/button'?{Button:({children,...props})=>{delete props.variant;return React.createElement('button',props,children);}}
  :name==='@/features/receivables/shared'?{ErrorState:({label})=>React.createElement('p',null,label)}
  :name==='@/lib/api'?{readApi:()=>{throw Error('Copy render must not fetch or authorize dispatch');}}
  :req(name);
 new Function('require','module','exports',js)(local,mod,mod.exports);
 return mod.exports.PolicyEditor;
}
for(const automation_enabled of [false,true])for(const outbound_paused of [false,true]){
 test(`policy copy reports saved workspace state ${automation_enabled}/${outbound_paused} without inventing global execution state`,()=>{
  const workspace={id:'current-workspace',permissions:['settings','pause','resume'],automation_enabled,outbound_paused};
  const client=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  client.setQueryData(['policies',workspace.id],[]);
  client.setQueryData(['policy-recipients',workspace.id],[]);
  const html=renderToStaticMarkup(React.createElement(QueryClientProvider,{client},React.createElement(loadEditor(),{workspace,csrfToken:'test-only'})));
  assert.match(html,/Activating a reviewed policy schedules source checks/);
  assert.match(html,/Every dispatch still requires current provider, source, recipient, limits and global guards/);
  assert.match(html,/any required approval/);
  assert.match(html,new RegExp(automation_enabled?'Policy automation enabled':'Policy automation off'));
  assert.match(html,new RegExp(outbound_paused?'Tenant outbound paused':'Tenant outbound resumed'));
  assert.doesNotMatch(html,/External outreach is unavailable|source checks only|Global outbound disabled/);
  // The two actual workspace switches still govern pause/resume button state.
  assert.equal(/<button[^>]*disabled=""[^>]*>Pause outbound<\/button>/.test(html),outbound_paused);
  assert.equal(/<button[^>]*disabled=""[^>]*>Resume outbound<\/button>/.test(html),!outbound_paused);
  client.clear();
 });
}
