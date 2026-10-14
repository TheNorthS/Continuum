import { useSession } from '../stores/session';
import type { Answer, Step } from '../types';
import { SSEParser } from './sse';
import { signedRequest } from './proof';

async function checked(response: Response): Promise<Response> {
  if (response.status === 401) useSession.getState().logout();
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.error?.message || `请求失败 (${response.status})`);
  }
  return response;
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  const token = useSession.getState().token;
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  let body = options.body;
  const anonymous = path === '/auth/login' || path === '/auth/register';
  if (anonymous) {
    const challenge = await (await checked(await fetch('/api/v1/auth/challenge'))).json();
    body = JSON.stringify({ ...JSON.parse(body as string), challenge: challenge.challenge });
  }
  const request = new Request(new URL(`/api/v1${path}`, location.origin), { ...options, body, headers });
  let response = await fetch(await signedRequest(request.clone(), anonymous ? '' : token));
  if (response.status === 401 && !anonymous && path !== '/auth/refresh' && useSession.getState().refresh) {
    await refreshSession();
    response = await fetch(await signedRequest(request, useSession.getState().token));
  }
  await checked(response);
  return response.status === 204 ? (undefined as T) : response.json();
}

let refreshing: Promise<void> | null = null;
async function refreshSession() {
  if (!refreshing)
    refreshing = (async () => {
      const refresh = useSession.getState().refresh;
      const req = new Request(new URL('/api/v1/auth/refresh', location.origin), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refresh }),
      });
      const response = await checked(await fetch(await signedRequest(req, refresh)));
      const data = await response.json();
      useSession.getState().login(data.access_token, data.user, data.refresh_token);
    })().finally(() => {
      refreshing = null;
    });
  return refreshing;
}

export async function streamAnswer(
  id: string,
  question: string,
  mode: string,
  onStep: (step: Step) => void,
  signal: AbortSignal,
  baseIds?: string[],
  options?: { use_knowledge?: boolean; attachment_ids?: string[] },
): Promise<Answer> {
  const request = new Request(new URL(`/api/v1/conversations/${id}/stream`, location.origin), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, mode, ...(baseIds ? { base_ids: baseIds } : {}), ...options }),
    signal,
  });
  let response = await fetch(await signedRequest(request.clone(), useSession.getState().token));
  if (response.status === 401 && useSession.getState().refresh) {
    await refreshSession();
    response = await fetch(await signedRequest(request, useSession.getState().token));
  }
  await checked(response);
  const reader = response.body?.getReader();
  if (!reader) throw new Error('浏览器不支持流式响应');
  const decoder = new TextDecoder();
  const parser = new SSEParser();
  try {
    while (true) {
      const { done, value } = await reader.read();
      const text = done ? decoder.decode() : decoder.decode(value, { stream: true });
      for (const event of parser.push(text)) {
        const data = JSON.parse(event.data);
        if (event.event === 'step') onStep(data);
        if (event.event === 'result') return data as Answer;
        if (event.event === 'error') throw new Error(data.message);
      }
      if (done) throw new Error('连接已结束，但未收到回答，请重试');
    }
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
}

export async function attachmentBlob(id: string): Promise<Blob> {
  const request = new Request(
    new URL(`/api/v1/attachments/${encodeURIComponent(id)}/content`, location.origin),
  );
  let response = await fetch(await signedRequest(request.clone(), useSession.getState().token));
  if (response.status === 401 && useSession.getState().refresh) {
    await refreshSession();
    response = await fetch(await signedRequest(request, useSession.getState().token));
  }
  return (await checked(response)).blob();
}
