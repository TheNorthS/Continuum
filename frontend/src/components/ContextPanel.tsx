import { useEffect, useState } from 'react';
import { api } from '../services/api';

interface ContextStatus {
  revision: number;
  meter: string;
  turns: number;
  pins: string[];
  limits: { window: number; trigger: number; target: number };
  checkpoint: {
    id: string;
    tokens_before: number;
    tokens_after: number;
    summary: Record<string, unknown>;
  } | null;
  events: { id: string; preview: string }[];
  checkpoints: { id: string; tokens_before: number; tokens_after: number }[];
}

export function ContextPanel({
  conversationId,
  onClose,
  onBusy,
}: {
  conversationId: string;
  onClose: () => void;
  onBusy: (value: boolean) => void;
}) {
  const [data, setData] = useState<ContextStatus | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const reload = () => api<ContextStatus>(`/conversations/${conversationId}/context`).then(setData);
  useEffect(() => {
    let active = true;
    api<ContextStatus>(`/conversations/${conversationId}/context`)
      .then((r) => {
        if (active) setData(r);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [conversationId]);
  async function action(path: string, method = 'POST', body?: object) {
    setBusy(true);
    onBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await api<{ committed?: boolean }>(`/conversations/${conversationId}/${path}`, {
        method,
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      setNotice(
        path === 'context/compact'
          ? result.committed
            ? '压缩检查点已保存，原始消息仍可查看。'
            : '没有可安全压缩且有收益的区间，原始上下文保持不变。'
          : path === 'memory/extract'
            ? '已加入后台提炼队列。'
            : path === 'context/restore'
              ? '上下文投影已恢复，原始历史保持完整。'
              : '固定消息已更新。',
      );
      await reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      onBusy(false);
    }
  }
  return (
    <div className="modal-backdrop">
      <section className="modal context-panel" role="dialog" aria-modal="true" aria-label="会话上下文">
        <div className="space-toolbar">
          <h2>会话上下文</h2>
          <button disabled={busy} onClick={onClose}>
            关闭
          </button>
        </div>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {notice && (
          <p className="notice" role="status">
            {notice}
          </p>
        )}
        {data ? (
          <>
            <p>
              历史 {data.turns} 个完整回合 · 版本 {data.revision}
            </p>
            <p>
              窗口 {data.limits.window.toLocaleString()} · 触发 {data.limits.trigger.toLocaleString()} · 目标{' '}
              {data.limits.target.toLocaleString()}
            </p>
            <p className="subtle">
              计量：{data.meter === 'conservative' ? '保守字节预算（不是精确 Token）' : data.meter}
              。压缩摘要仅用于模型上下文，不删除聊天记录。
            </p>
            <div className="space-toolbar">
              <button disabled={busy} onClick={() => void action('context/compact')}>
                {busy ? '处理中…' : '现在压缩'}
              </button>
              <button disabled={busy} onClick={() => void action('memory/extract')}>
                提炼长期记忆
              </button>
            </div>
            {data.checkpoint && (
              <details>
                <summary>
                  当前摘要 · {data.checkpoint.tokens_before.toLocaleString()} →{' '}
                  {data.checkpoint.tokens_after.toLocaleString()}
                </summary>
                <pre>{JSON.stringify(data.checkpoint.summary, null, 2)}</pre>
              </details>
            )}
            <h3>保留重要原文</h3>
            {!!data.checkpoints.length && (
              <details>
                <summary>历史检查点与恢复 · {data.checkpoints.length}</summary>
                <button
                  disabled={busy}
                  onClick={() => void action('context/restore', 'POST', { checkpoint_id: null })}
                >
                  恢复原始历史投影
                </button>
                {data.checkpoints.map((cp) => (
                  <p key={cp.id}>
                    <small>
                      {cp.id.slice(0, 10)} · {cp.tokens_before} → {cp.tokens_after}
                    </small>{' '}
                    <button
                      disabled={busy || cp.id === data.checkpoint?.id}
                      onClick={() => void action('context/restore', 'POST', { checkpoint_id: cp.id })}
                    >
                      恢复此检查点
                    </button>
                  </p>
                ))}
              </details>
            )}
            <p className="subtle">固定的用户消息不会跨过压缩切点；过多固定内容可能导致预算不足。</p>
            <div className="context-pins">
              {data.events.map((e) => (
                <label key={e.id}>
                  <input
                    type="checkbox"
                    aria-label={`固定消息 ${e.preview}`}
                    disabled={busy}
                    checked={data.pins.includes(e.id)}
                    onChange={() =>
                      void action('context/pins', 'PUT', {
                        event_ids: data.pins.includes(e.id)
                          ? data.pins.filter((id) => id !== e.id)
                          : [...data.pins, e.id],
                      })
                    }
                  />
                  <span>{e.preview}</span>
                </label>
              ))}
            </div>
          </>
        ) : (
          <p>正在读取上下文…</p>
        )}
      </section>
    </div>
  );
}
