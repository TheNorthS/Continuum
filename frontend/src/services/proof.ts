const encoder = new TextEncoder();
let pendingKey: Promise<CryptoKeyPair> | null = null;

function database(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('rag-e-proof-keys', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('keys');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new Error('无法打开浏览器安全密钥存储'));
  });
}

async function loadKey(): Promise<CryptoKeyPair> {
  let id = sessionStorage.getItem('rag-e-key-id');
  if (!id) {
    id = crypto.randomUUID();
    sessionStorage.setItem('rag-e-key-id', id);
  }
  const db = await database();
  try {
    const existing = await new Promise<CryptoKeyPair | undefined>((resolve, reject) => {
      const tx = db.transaction('keys', 'readonly'),
        request = tx.objectStore('keys').get(id!);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    if (existing) return existing;
    const key = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, [
      'sign',
      'verify',
    ]);
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction('keys', 'readwrite');
      tx.objectStore('keys').put(key, id!);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
    return key;
  } finally {
    db.close();
  }
}

export function proofKey() {
  return (pendingKey ||= loadKey().catch((error) => {
    pendingKey = null;
    throw error;
  }));
}
function b64(bytes: Uint8Array) {
  return btoa(String.fromCharCode(...bytes))
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replace(/=+$/, '');
}
export async function sha256(bytes: BufferSource) {
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), (b) =>
    b.toString(16).padStart(2, '0'),
  ).join('');
}

export async function signedRequest(request: Request, token = ''): Promise<Request> {
  const key = await proofKey();
  const publicKey = await crypto.subtle.exportKey('jwk', key.publicKey);
  const url = new URL(request.url);
  const header = b64(encoder.encode(JSON.stringify({ alg: 'ES256', typ: 'rage-proof+jwt', jwk: publicKey })));
  const body = await request.clone().arrayBuffer();
  const claims = b64(
    encoder.encode(
      JSON.stringify({
        jti: crypto.randomUUID().replaceAll('-', ''),
        iat: Math.floor(Date.now() / 1000),
        htm: request.method,
        htu: url.pathname + url.search,
        bht: await sha256(body),
        ath: await sha256(encoder.encode(token)),
      }),
    ),
  );
  const input = `${header}.${claims}`;
  const signature = await crypto.subtle.sign(
    { name: 'ECDSA', hash: 'SHA-256' },
    key.privateKey,
    encoder.encode(input),
  );
  const headers = new Headers(request.headers);
  headers.set('X-Request-Proof', `${input}.${b64(new Uint8Array(signature))}`);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  return new Request(request, { headers });
}
