/** Build an unsigned JWT-shaped token for tests. */
export function fakeJwt(payload: Record<string, unknown>): string {
  const enc = (o: unknown) =>
    btoa(JSON.stringify(o)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  return `${enc({ alg: 'none' })}.${enc(payload)}.sig`
}
