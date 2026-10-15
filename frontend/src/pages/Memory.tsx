import { useEffect, useState } from 'react';
import { Brain, FileText, Plus, RefreshCw, Trash2, X } from 'lucide-react';
import { api } from '../services/api';

interface MemoryItem {
  id: string;
  category: string;
  status: string;
  version: number;
  base_id: string | null;
  payload: { subject?: string; predicate?: string; value?: string };
  evidence_ids: string[];
  updated_at: number;
  expires_at: number | null;
}
interface MemoryView {
  items: MemoryItem[];
  preferences: { enabled: boolean; auto_extract: boolean };
  jobs: { id: string; status: string; stage: string; attempts: number; error: string | null }[];
}
const statuses: Record<string, string> = {
  active: '有效',
  disputed: '待确认冲突',
  superseded: '已被更正',
  deleted: '已遗忘',
  expired: '已过期',
};

export function Memory() {
  const [data, setData] = useState<MemoryView | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState('');
  const [editing, setEditing] = useState<MemoryItem | 'new' | null>(null);
  const [form, setForm] = useState({ subject: '', predicate: '', value: '', category: 'fact', base_id: '' });
  const [bases, setBases] = useState<{ id: string; name: string }[]>([]);
  const [evidence, setEvidence] = useState<{ id: string; content: { text: string } }[] | null>(null);
  const [deleting, setDeleting] = useState<MemoryItem | 'all' | null>(null);
  const reload = () => api<MemoryView>('/memory').then(setData);
  useEffect(() => {
    let active = true;
    api<MemoryView>('/memory')
      .then((r) => {
        if (active) setData(r);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    api<{ id: string; name: string }[]>('/knowledge/bases')
      .then((r) => {
        if (active) setBases(r);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, []);
  async function mutate(action: () => Promise<unknown>) {
    setBusy(true);
    setError('');
    try {
      await action();
      await reload();
      setEditing(null);
      setDeleting(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function edit(item: MemoryItem | 'new') {
    setEditing(item);
    setForm(
      item === 'new'
        ? { subject: '', predicate: '', value: '', category: 'fact', base_id: '' }
        : {
            subject: item.payload.subject || '',
            predicate: item.payload.predicate || '',
            value: item.payload.value || '',
            category: item.category,
            base_id: item.base_id || '',
          },
    );
  }
  const visible = (data?.items ?? []).filter(
    (m) => m.status !== 'deleted' && JSON.stringify(m.payload).toLowerCase().includes(query.toLowerCase()),
  );
  return (
    <div className="content-page memory-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">PERSONAL MEMORY</span>
          <h1>记忆管理</h1>
          <p className="subtle">跨会话保留有依据的偏好、事实和经验。只有你能访问这些记忆。</p>
        </div>
        <button className="primary" onClick={() => edit('new')} disabled={busy}>
          <Plus size={16} />
          记住一条信息
        </button>
      </div>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {data && (
        <section className="memory-settings">
          <label>
            <input
              type="checkbox"
              checked={data.preferences.enabled}
              disabled={busy}
              onChange={(e) =>
                void mutate(() =>
                  api('/memory/preferences', {
                    method: 'PATCH',
                    body: JSON.stringify({ enabled: e.target.checked }),
                  }),
                )
              }
            />
            使用个人记忆
          </label>
          <label>
            <input
              type="checkbox"
              checked={data.preferences.auto_extract}
              disabled={busy}
              onChange={(e) =>
                void mutate(() =>
                  api('/memory/preferences', {
                    method: 'PATCH',
                    body: JSON.stringify({ auto_extract: e.target.checked }),
                  }),
                )
              }
            />
            空闲后自动提炼
          </label>
          <p>
            关闭后不再注入或自动提炼，已有记录保留。知识库相关记忆仍受当前库范围和权限限制；争议、过期及失效来源不会用于回答。
          </p>
        </section>
      )}
      <div className="space-toolbar">
        <input
          aria-label="搜索记忆"
          placeholder="搜索主体、属性或内容…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button onClick={() => void reload().catch((e) => setError(e.message))}>
          <RefreshCw size={16} />
          刷新
        </button>
        <button className="danger" disabled={busy || !data?.items.length} onClick={() => setDeleting('all')}>
          遗忘全部
        </button>
      </div>
      {!data ? (
        <p role="status">正在读取记忆…</p>
      ) : !visible.length ? (
        <div className="empty">
          <Brain size={32} />
          <h3>暂无可展示的记忆</h3>
          <p>可以主动添加，或在多轮对话后让后台提炼有长期价值的用户陈述。</p>
        </div>
      ) : (
        <div className="memory-list">
          {visible.map((m) => (
            <article className="memory-card" key={m.id}>
              <div>
                <strong>
                  {m.payload.subject} · {m.payload.predicate}
                </strong>
                <span className={`memory-status ${m.status}`}>
                  {m.expires_at && m.expires_at < Date.now() / 1000
                    ? '已过期'
                    : statuses[m.status] || m.status}{' '}
                  · v{m.version}
                </span>
              </div>
              <p>{m.payload.value}</p>
              <small>
                {m.base_id
                  ? `知识库：${bases.find((b) => b.id === m.base_id)?.name || m.base_id}`
                  : '个人范围'}{' '}
                · {new Date(m.updated_at * 1000).toLocaleString()} · {m.evidence_ids.length} 条证据
              </small>
              <footer>
                <button
                  onClick={() =>
                    void api<{ events: { id: string; content: { text: string } }[] }>(
                      `/memory/${m.id}/evidence`,
                    )
                      .then((r) => setEvidence(r.events))
                      .catch((e) => setError(e.message))
                  }
                >
                  <FileText size={15} />
                  查看依据
                </button>
                <button disabled={busy} onClick={() => edit(m)}>
                  更正
                </button>
                <button disabled={busy} onClick={() => setDeleting(m)}>
                  <Trash2 size={15} />
                  遗忘
                </button>
              </footer>
            </article>
          ))}
        </div>
      )}
      {!!data?.jobs.length && (
        <details className="memory-jobs">
          <summary>后台提炼任务 · {data.jobs.length}</summary>
          {data.jobs.map((j) => (
            <p key={j.id}>
              {j.stage} · {j.status} · 尝试 {j.attempts}
              {j.error ? ` · ${j.error}` : ''}
            </p>
          ))}
        </details>
      )}
      {editing && (
        <div className="modal-backdrop">
          <form
            className="modal memory-form"
            role="dialog"
            aria-modal="true"
            aria-label="编辑记忆"
            onSubmit={(e) => {
              e.preventDefault();
              const existing = editing === 'new' ? null : editing;
              void mutate(() =>
                api(existing ? `/memory/${existing.id}` : '/memory', {
                  method: existing ? 'PATCH' : 'POST',
                  body: JSON.stringify({
                    ...form,
                    base_id: form.base_id || null,
                    ...(existing ? { expected_version: existing.version } : {}),
                  }),
                }),
              );
            }}
          >
            <h2>{editing === 'new' ? '记住一条信息' : '明确更正记忆'}</h2>
            <label>
              主体
              <input
                aria-label="记忆主体"
                maxLength={120}
                required
                value={form.subject}
                disabled={editing !== 'new'}
                onChange={(e) => setForm({ ...form, subject: e.target.value })}
              />
            </label>
            <label>
              属性
              <input
                aria-label="记忆属性"
                maxLength={120}
                required
                value={form.predicate}
                disabled={editing !== 'new'}
                onChange={(e) => setForm({ ...form, predicate: e.target.value })}
              />
            </label>
            <label>
              内容
              <textarea
                aria-label="记忆内容"
                maxLength={1200}
                required
                value={form.value}
                onChange={(e) => setForm({ ...form, value: e.target.value })}
              />
            </label>
            <label>
              类别
              <select value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })}>
                <option value="fact">事实</option>
                <option value="preference">偏好</option>
                <option value="decision">决策</option>
                <option value="episode">经验</option>
              </select>
            </label>
            <label>
              适用范围
              <select
                value={form.base_id}
                disabled={editing !== 'new'}
                onChange={(e) => setForm({ ...form, base_id: e.target.value })}
              >
                <option value="">个人</option>
                {bases.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                  </option>
                ))}
              </select>
            </label>
            <p>更正会创建新版本并替代旧事实。不保存密码、API Key 或身份敏感信息。</p>
            <button type="button" onClick={() => setEditing(null)} disabled={busy}>
              取消
            </button>
            <button className="primary" disabled={busy}>
              保存
            </button>
          </form>
        </div>
      )}
      {deleting && (
        <div className="modal-backdrop">
          <section className="modal" role="dialog" aria-modal="true" aria-label="确认遗忘">
            <h2>{deleting === 'all' ? '遗忘全部记忆？' : '遗忘这条记忆？'}</h2>
            <p>停止使用这些记忆并清除派生内容，后台任务不会自动复活已遗忘事实。原始聊天记录仍按会话管理。</p>
            <button onClick={() => setDeleting(null)} disabled={busy}>
              取消
            </button>
            <button
              className="danger"
              disabled={busy}
              onClick={() =>
                void mutate(() =>
                  api(deleting === 'all' ? '/memory' : `/memory/${deleting.id}`, { method: 'DELETE' }),
                )
              }
            >
              确认遗忘
            </button>
          </section>
        </div>
      )}
      {evidence && (
        <div className="modal-backdrop">
          <section className="modal attachment-preview" role="dialog" aria-modal="true" aria-label="记忆依据">
            <button aria-label="关闭依据" onClick={() => setEvidence(null)}>
              <X size={18} />
            </button>
            <h2>原始依据</h2>
            {evidence.map((e) => (
              <div key={e.id}>
                <small>{e.id}</small>
                <pre>{e.content.text}</pre>
              </div>
            ))}
          </section>
        </div>
      )}
    </div>
  );
}
