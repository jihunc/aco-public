import { NextResponse } from 'next/server';
export const dynamic = 'force-dynamic';
export async function GET() {
  // Server-only address; never accept a user supplied proxy destination.
  const origin = process.env.ACO_API_INTERNAL_ORIGIN ?? 'http://127.0.0.1:58000';
  try {
    const response = await fetch(`${origin}/health/ready`, {cache: 'no-store', signal: AbortSignal.timeout(5000)});
    return NextResponse.json(await response.json(), {status: response.status});
  } catch {
    return NextResponse.json({status: 'not_ready', checks: {api: 'UNAVAILABLE'}}, {status: 503});
  }
}
