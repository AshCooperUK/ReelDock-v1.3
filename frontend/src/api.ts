import { cleanDisplay } from './plainNames';
import { notify } from './notifications-state';

type ReadResult = { ok: boolean; status: number; result: { error?: string } };
type ReadFlight = { promise: Promise<ReadResult>; controller?: AbortController; readers: number; settled: boolean };
const pendingReads = new Map<string, ReadFlight>();
const reportedReadErrors = new Map<string, string>();
const RETRY_BUDGET_MS = 30_000;

function aborted() {
  const error = new Error('Request cancelled.');
  error.name = 'AbortError';
  return error;
}

function pause(delay: number, signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) { reject(aborted()); return; }
    const cancel = () => { clearTimeout(timer); signal?.removeEventListener('abort', cancel); reject(aborted()); };
    const timer = setTimeout(() => { signal?.removeEventListener('abort', cancel); resolve(); }, delay);
    signal?.addEventListener('abort', cancel, { once: true });
  });
}

async function read(url: string, signal?: AbortSignal): Promise<ReadResult> {
  const started = Date.now();
  for (let attempt = 0; ; attempt++) {
    if (signal?.aborted) throw aborted();
    const response = await fetch(url, { signal, credentials: 'same-origin', cache: 'no-store' });
    const result = await response.json().catch(() => ({ error: 'ReelDock did not return a valid response.' }));
    if ((response.status === 429 || response.status === 503) && attempt < 2) {
      const retryAfter = response.headers?.get('Retry-After');
      let delay = (response.status === 429 ? 1000 : 250) * 2 ** attempt;
      if (retryAfter) {
        const seconds = Number(retryAfter);
        const parsed = Number.isFinite(seconds) ? seconds * 1000 : Date.parse(retryAfter) - Date.now();
        if (Number.isFinite(parsed)) delay = Math.max(0, parsed);
      }
      // Never turn a provider's long Retry-After into an earlier request.
      if (delay <= RETRY_BUDGET_MS - (Date.now() - started)) {
        await pause(delay, signal);
        continue;
      }
    }
    return { ok: response.ok, status: response.status, result };
  }
}

function sharedRead(url: string, signal?: AbortSignal): Promise<ReadResult> {
  if (signal?.aborted) return Promise.reject(aborted());
  let flight = pendingReads.get(url);
  if (!flight) {
    const controller = typeof AbortController === 'function' ? new AbortController() : undefined;
    flight = { promise: read(url, controller?.signal), controller, readers: 0, settled: false };
    pendingReads.set(url, flight);
    const current = flight;
    const finish = () => { current.settled = true; if (pendingReads.get(url) === current) pendingReads.delete(url); };
    void current.promise.then(finish, finish);
  }
  const current = flight;
  current.readers++;
  return new Promise<ReadResult>((resolve, reject) => {
    let finished = false;
    const release = () => {
      if (finished) return false;
      finished = true;
      signal?.removeEventListener('abort', cancel);
      current.readers--;
      return true;
    };
    const cancel = () => {
      if (!release()) return;
      reject(aborted());
      if (!current.readers && !current.settled) {
        if (pendingReads.get(url) === current) pendingReads.delete(url);
        current.controller?.abort();
      }
    };
    signal?.addEventListener('abort', cancel, { once: true });
    void current.promise.then(value => { if (release()) resolve(value); }, error => { if (release()) reject(error); });
  });
}

export async function api<T>(url: string, signal?: AbortSignal, body?: object, csrf?: string, options?: { expectedReadStatuses?: readonly number[]; feedback?: 'notification' | 'inline' }): Promise<T> {
  let expectedReadError = false;
  try {
    const multipart = typeof FormData !== 'undefined' && body instanceof FormData;
    // Writes are sent exactly once. Only concurrent reads share a transport.
    const response = body ? await fetch(url, {
      signal, credentials: 'same-origin', cache: 'no-store', method: 'POST',
      headers: multipart ? { 'X-CSRF-Token': csrf || '' } : { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf || '' }, body: multipart ? body : JSON.stringify(body),
    }) : undefined;
    const { ok, status, result } = response ? {
      ok: response.ok, status: response.status,
      result: await response.json().catch(() => ({ error: 'ReelDock did not return a valid response.' })),
    } : await sharedRead(url, signal);
    if (!ok) {
      expectedReadError = !body && !!options?.expectedReadStatuses?.includes(status);
      throw new Error(result.error || 'Could not load your library. Try again.');
    }
    // A subsequent refresh must not reuse a read that started before this write.
    if (body) pendingReads.clear();
    reportedReadErrors.delete(url);
    return cleanDisplay(result) as T;
  } catch (error) {
    if (options?.feedback !== 'inline' && !expectedReadError && error instanceof Error && error.name !== 'AbortError') {
      if (body || reportedReadErrors.get(url) !== error.message) notify({ kind: 'error', message: error.message });
      if (!body) reportedReadErrors.set(url, error.message);
    }
    throw error;
  }
}
