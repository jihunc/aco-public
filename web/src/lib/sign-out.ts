// Sign out of the current session. Only a confirmed server sign-out leaves the page,
// so a refused or failed request never pretends the session ended.
export async function signOut(
  csrfToken: string,
  request: typeof fetch = fetch,
  navigate: (to: string) => void = (to) => window.location.assign(to),
): Promise<boolean> {
  if (!csrfToken) return false;
  try {
    const response = await request("/api/auth/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: "{}",
    });
    if (!response.ok) return false;
  } catch {
    return false;
  }
  navigate("/login");
  return true;
}
