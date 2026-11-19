export default async function Login({searchParams}: {searchParams: Promise<{error?: string}>}) {
  const {error} = await searchParams;
  return <main className="mx-auto max-w-lg space-y-6 p-8">
    <p className="text-sm">ACO · Synthetic assessment workspace</p>
    <h1 className="text-3xl font-semibold">Welcome to ACO</h1>
    {error === 'unavailable' && <p role="alert" className="notice notice-warning">
      Sign-in could not finish because the identity service is still starting. Please try again.
    </p>}
    <p>Sign in or register with the identity provider to create or join a workspace.</p>
    <a className="btn btn-primary" href="/api/auth/login">Sign in</a>
    <p>Creating a workspace keeps automation off and outbound paused.</p>
  </main>;
}
