'use client';
import {useState} from 'react';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import type {SessionView} from '@/lib/api';
import {DemoAccountingSetup} from './demo-accounting';
import {MembersPanel} from './members';
import {ExportPanel} from '../exports/panel';

type Me=SessionView;
export function Setup(){
  const cache=useQueryClient();
  const [error,setError]=useState('');
  const [pending,setPending]=useState(false);
  const [selected,setSelected]=useState('');
  const [invite,setInvite]=useState('');
  const session=useQuery<Me>({queryKey:['me'],retry:false,queryFn:async()=>{
    const res=await fetch('/api/me');
    if(res.status===401){window.location.assign('/login');throw new Error('Sign in required');}
    if(!res.ok)throw new Error('Unable to load your workspace. Try again.');
    return res.json();
  }});
  async function mutate(path:string,body:object,method='POST'){
    setPending(true);setError('');
    try{
      const res=await fetch(path,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':session.data?.csrf_token??''},body:JSON.stringify(body)});
      if(!res.ok){const payload=await res.json();throw new Error(typeof payload.detail==='string'?payload.detail:'Check the supplied details.');}
      const data=await res.json();await cache.invalidateQueries({queryKey:['me']});return data;
    }catch(err){setError(err instanceof Error?err.message:'Request failed.');return null;}
    finally{setPending(false);}
  }
  if(session.isPending)return <main className="p-8" role="status">Loading your workspace…</main>;
  if(session.isError)return <main className="p-8"><p role="alert">{session.error.message}</p><button onClick={()=>session.refetch()}>Try again</button><a href="/login">Sign in</a></main>;
  const workspace=session.data.workspaces.find(w=>w.id===selected)??session.data.workspaces.at(-1);
  return <main className="mx-auto max-w-3xl space-y-8 p-8">
    <header className="flex flex-wrap items-center justify-between gap-4"><p>ACO · {session.data.email}</p><button disabled={pending} onClick={async()=>{if(await mutate('/api/auth/logout',{})){cache.clear();window.location.assign('/login');}}}>Sign out</button></header>
    <h1 className="text-3xl font-semibold">Set up your workspace</h1>
    <p>Synthetic assessment data only. Signup never enables paid services or outreach.</p>
    {error&&<p role="alert" className="text-red-700">{error}</p>}
    {pending&&<p role="status">Saving…</p>}
    {workspace&&<section className="space-y-3 rounded border p-5">
      <label htmlFor="onboarding-workspace">Workspace</label><select id="onboarding-workspace" className="ml-3 rounded border p-2 max-w-full" value={workspace.id} onChange={e=>{setSelected(e.target.value);setInvite('');}}>{session.data.workspaces.map(w=><option key={w.id} value={w.id}>{w.name}</option>)}</select>
      <h2 className="text-2xl font-semibold break-words">{workspace.name}</h2>
      <a href={`/overview?workspace=${workspace.id}`} className="underline">Open collections workspace →</a>
      <p>{workspace.timezone} · {workspace.role}</p>
      <p>{workspace.outbound_paused?"Outbound paused":"Tenant outbound resumed"} · {workspace.automation_enabled?"Policy automation enabled":"Automation off"}</p>
      <a href={`/settings/policies?workspace=${workspace.id}`} className="underline">Collection policies →</a>
      <DemoAccountingSetup key={workspace.id} workspace={workspace} csrfToken={session.data.csrf_token} disabled={pending}/>
      {workspace.permissions.includes('export')&&<ExportPanel key={`export-${workspace.id}`} workspace={workspace.id} csrfToken={session.data.csrf_token} disabled={pending}/>}
      {workspace.permissions.includes('settings')&&<>
        <MembersPanel key={workspace.id} workspace={workspace.id} csrfToken={session.data.csrf_token} disabled={pending}/>
        <form className="space-y-3" onSubmit={async e=>{e.preventDefault();const f=new FormData(e.currentTarget);await mutate(`/api/workspaces/${workspace.id}`,{name:f.get('name'),timezone:f.get('timezone')},'PATCH');}}>
          <h3 className="font-semibold">Workspace settings</h3><label className="block">Saved name<input className="ml-3 border p-2" name="name" required defaultValue={workspace.name} key={workspace.id+workspace.name}/></label>
          <label className="block">Saved timezone<input className="ml-3 border p-2" name="timezone" required defaultValue={workspace.timezone} key={workspace.id+workspace.timezone}/></label><button disabled={pending}>Save settings</button>
        </form>
        <form className="space-y-3" onSubmit={async e=>{e.preventDefault();const f=new FormData(e.currentTarget);const result=await mutate(`/api/workspaces/${workspace.id}/invitations`,{email:f.get('email'),role:f.get('role')});if(result)setInvite(result.invite_code);}}>
          <h3 className="font-semibold">Invite a teammate</h3><p>No email is sent. Share the code securely with the verified email owner. Expires after 24 hours.</p>
          <label className="block">Teammate email<input className="ml-3 border p-2" name="email" type="email" required/></label>
          <div className="block"><label htmlFor="invitation-role">Role</label><select id="invitation-role" className="ml-3 border p-2" name="role" defaultValue="viewer">{['viewer','operator','finance_manager','owner'].map(r=><option key={r} value={r}>{r}</option>)}</select></div><button disabled={pending}>Create invitation</button>
          {invite&&<label className="block">Invitation code<input className="w-full border p-2" readOnly value={invite}/></label>}
        </form>
      </>}
    </section>}
    <form className="space-y-4 rounded border p-5" onSubmit={async e=>{e.preventDefault();const f=new FormData(e.currentTarget);const result=await mutate('/api/workspaces',{name:f.get('name'),timezone:f.get('timezone')});if(result)setSelected(result.id);}}>
      <h2 className="text-xl font-semibold">Create a workspace</h2>
      <label className="block">Workspace name<input className="ml-3 border p-2" name="name" required maxLength={120}/></label>
      <label className="block">Timezone<input className="ml-3 border p-2" name="timezone" required defaultValue="Australia/Melbourne"/></label>
      <p>Accounting source: Demo (synthetic), pending setup</p><button className="rounded bg-slate-900 px-4 py-2 text-white" disabled={pending}>Create demo workspace</button>
    </form>
    <form className="space-y-3 rounded border p-5" onSubmit={async e=>{e.preventDefault();const f=new FormData(e.currentTarget);const result=await mutate('/api/invitations/accept',{invite_code:f.get('code')});if(result)setSelected(result.id);}}>
      <h2 className="text-xl font-semibold">Join a workspace</h2><p>Your verified identity email must match the invitation.</p>
      <label className="block">Join code<input className="ml-3 border p-2" name="code" required/></label><button disabled={pending}>Accept invitation</button>
    </form>
  </main>;
}
