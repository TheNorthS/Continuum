// @vitest-environment node
import 'fake-indexeddb/auto';
import { webcrypto } from 'node:crypto';
import { beforeEach, expect, test, vi } from 'vitest';
import { TextEncoder, TextDecoder } from 'node:util';

beforeEach(() => {
  vi.resetModules();
  const values = new Map<string, string>();
  vi.stubGlobal('sessionStorage', {
    getItem: (key: string) => values.get(key) || null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
    clear: () => values.clear(),
  });
  vi.stubGlobal('crypto', webcrypto);
  vi.stubGlobal('TextEncoder', TextEncoder);
  vi.stubGlobal('TextDecoder', TextDecoder);
});
function decode(value: string) {
  return JSON.parse(Buffer.from(value, 'base64url').toString('utf8'));
}

test('proof signs exact body, path, query, method and token using a non-exportable private key', async () => {
  const { signedRequest, sha256, proofKey } = await import('./proof');
  const request = new Request('http://localhost/api/v1/knowledge/search?top=3', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query: '私有文档' }),
  });
  const signed = await signedRequest(request, 'access-token');
  const [header, body, signature] = signed.headers.get('X-Request-Proof')!.split('.');
  const h = decode(header),
    payload = decode(body);
  expect(h.typ).toBe('rage-proof+jwt');
  expect(h.jwk.d).toBeUndefined();
  expect(payload.htm).toBe('POST');
  expect(payload.htu).toBe('/api/v1/knowledge/search?top=3');
  expect(payload.bht).toBe(await sha256(await signed.clone().arrayBuffer()));
  expect(payload.ath).toBe(await sha256(new TextEncoder().encode('access-token')));
  const key = await webcrypto.subtle.importKey('jwk', h.jwk, { name: 'ECDSA', namedCurve: 'P-256' }, false, [
    'verify',
  ]);
  expect(
    await webcrypto.subtle.verify(
      { name: 'ECDSA', hash: 'SHA-256' },
      key,
      Buffer.from(signature, 'base64url'),
      new TextEncoder().encode(header + '.' + body),
    ),
  ).toBe(true);
  expect((await proofKey()).privateKey.extractable).toBe(false);
  await expect(webcrypto.subtle.exportKey('jwk', (await proofKey()).privateKey)).rejects.toThrow();
  const second = await signedRequest(new Request(signed), 'access-token');
  expect(decode(second.headers.get('X-Request-Proof')!.split('.')[1]).jti).not.toBe(payload.jti);
});

test('private CryptoKey survives module reload through IndexedDB without exporting it', async () => {
  const first = await import('./proof');
  const key = await first.proofKey();
  const publicBefore = await webcrypto.subtle.exportKey('jwk', key.publicKey);
  vi.resetModules();
  const second = await import('./proof');
  const reloaded = await second.proofKey();
  expect(await webcrypto.subtle.exportKey('jwk', reloaded.publicKey)).toEqual(publicBefore);
  expect(reloaded.privateKey.extractable).toBe(false);
});

test('multipart uploads sign the serialized boundary and file bytes actually sent', async () => {
  const { signedRequest, sha256 } = await import('./proof');
  const body = new FormData();
  body.append('base_id', 'private');
  body.append('file', new Blob(['private text']), 'document.txt');
  const request = new Request('http://localhost/api/v1/knowledge/documents', { method: 'POST', body });
  const signed = await signedRequest(request, 'token');
  const payload = decode(signed.headers.get('X-Request-Proof')!.split('.')[1]);
  expect(payload.bht).toBe(await sha256(await signed.clone().arrayBuffer()));
  expect(signed.headers.get('Content-Type')).toContain('multipart/form-data; boundary=');
});

test.skipIf(!process.env.RAGE_TEST_HTTP_URL)(
  'browser WebCrypto proof interoperates with the running Python server',
  async () => {
    const { signedRequest } = await import('./proof');
    const base = process.env.RAGE_TEST_HTTP_URL!;
    const challenge = await (await fetch(base + '/auth/challenge')).json();
    const login = await fetch(
      await signedRequest(
        new Request(base + '/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            username: 'admin',
            password: 'rag-e-demo-2026',
            challenge: challenge.challenge,
          }),
        }),
      ),
    );
    expect(login.status).toBe(200);
    const data = await login.json();
    const request = await signedRequest(new Request(base + '/admin/users'), data.access_token);
    const first = await fetch(request.clone());
    expect(first.status).toBe(200);
    expect((await fetch(request.clone())).status).toBe(409);
    const unsigned = await fetch(base + '/admin/users', {
      headers: { Authorization: 'Bearer ' + data.access_token },
    });
    expect(unsigned.status).toBe(401);
    const logout = await fetch(
      await signedRequest(new Request(base + '/auth/logout', { method: 'POST' }), data.access_token),
    );
    expect(logout.status).toBe(204);
  },
);
