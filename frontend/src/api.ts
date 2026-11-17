export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      message =
        typeof body.detail === 'string'
          ? body.detail
          : JSON.stringify(body.detail || body.error || body);
    } catch {
      /* preserve HTTP error */
    }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) });
export const put = <T>(path: string, body: unknown) =>
  api<T>(path, { method: 'PUT', body: JSON.stringify(body) });
export function arrayOf<T>(value: unknown, key: string): T[] {
  if (Array.isArray(value)) return value as T[];
  if (value && typeof value === 'object' && Array.isArray((value as Record<string, unknown>)[key]))
    return (value as Record<string, unknown>)[key] as T[];
  return [];
}
export const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** Combine event lists, keeping one event per sequence number, in sequence order. */
export function mergeBySeq<T extends { seq: number }>(a: T[], b: T[]): T[] {
  return Array.from(new Map([...a, ...b].map((item) => [item.seq, item])).values()).sort(
    (x, y) => x.seq - y.seq,
  );
}
