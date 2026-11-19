'use client';
import { useQuery } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
type Health = {status: 'ready' | 'not_ready'; checks: Record<string, string>};
export default function Home() {
  const health = useQuery({queryKey: ['health'], queryFn: async (): Promise<Health> => {
    const response = await fetch('/api/health');
    if (response.status !== 200 && response.status !== 503) throw new Error('Health request failed');
    return response.json();
  }});
  return <main className="mx-auto max-w-3xl p-8 sm:p-16">
    <p className="text-sm font-semibold uppercase tracking-widest text-slate-500">ACO · Synthetic offline environment</p>
    <h1 className="mt-6 text-4xl font-semibold">Runtime status</h1>
    <p className="mt-4 text-slate-600">External providers are fake. Outbound communication and capture are disabled.</p>
    <section className="mt-8 rounded-xl border border-slate-200 bg-white p-6" aria-label="Dependency status">
      <h2 className="text-xl font-semibold">{health.isPending ? 'Checking dependencies…' : health.isError ? 'Status unavailable' : health.data.status === 'ready' ? 'Ready' : 'Not ready'}</h2>
      {health.data && <dl className="my-6 grid grid-cols-2 gap-3">{Object.entries(health.data.checks).map(([name, status]) =>
        <div key={name}><dt className="capitalize text-slate-500">{name}</dt><dd className="font-mono text-sm">{status}</dd></div>)}</dl>}
      <Button onClick={() => {void health.refetch();}} disabled={health.isFetching}>Check again</Button>
    </section>
    <section className="mt-6 rounded-xl border border-slate-200 bg-white p-6" aria-labelledby="continue-heading">
      <h2 id="continue-heading" className="text-xl font-semibold">Open the collections workspace</h2>
      <p className="mt-2 text-slate-600">Sign in with a synthetic identity to create or join a workspace.</p>
      <a className="btn btn-primary mt-4" href="/login">Sign in</a>
    </section>
    <p className="mt-6 text-sm text-slate-500">This health check does not implement authentication, collection workflows or worker recovery.</p>
  </main>;
}
