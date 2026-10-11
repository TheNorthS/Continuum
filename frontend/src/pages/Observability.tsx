import { useEffect, useState } from 'react';
import { api } from '../services/api';

interface Profile {
  count: number;
  errors: number;
  p50_ms: number;
  p95_ms: number;
  p99_ms: number;
  average_ms: number;
}
interface Alert {
  name: string;
  state: string;
  value: number;
  threshold: number;
}
interface Span {
  trace_id: string;
  span_id: string;
  parent_id: string | null;
  name: string;
  duration_ms: number;
  status: string;
  at: number;
}
interface Snapshot {
  moderation?: {
    backend: string;
    policy_version: string;
    thresholds: Record<string, number>;
    failure_mode: string;
  };
  window_seconds: number;
  profiler: Record<string, Profile>;
  alerts: Alert[];
  history: { name: string; from: string; to: string; at: number }[];
  cache: { l1_size: number; l1_capacity: number; events: Record<string, number>; l2_enabled: boolean };
  tracing: { enabled: boolean; capture_content: boolean; instrumentation_errors: number };
  pool: { connections: number; max_concurrency: number; batch_size: number };
  dependencies: Record<string, boolean>;
  spans: Span[];
}
export function Observability() {
  const [data, setData] = useState<Snapshot | null>(null),
    [error, setError] = useState(''),
    [trace, setTrace] = useState('');
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    async function poll() {
      try {
        const value = await api<Snapshot>('/admin/observability', { signal: controller.signal });
        if (!stopped) {
          setData(value);
          setError('');
        }
      } catch (e) {
        if (!stopped) setError((e as Error).message);
      } finally {
        if (!stopped) timer = setTimeout(poll, 5000);
      }
    }
    void poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, []);
  const spans = data?.spans.filter((s) => !trace || s.trace_id === trace) || [];
  return (
    <div className="content-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">OBSERVABILITY</span>
          <h1>看清每一次请求。</h1>
          <p className="subtle">
            每 5 秒刷新 · 当前进程 · 最近 {data?.window_seconds || 300} 秒 · 多副本汇总使用 Prometheus。
          </p>
        </div>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {data && (
        <>
          <div className="system-grid">
            <section>
              <h3>LLM 连接与并发</h3>
              <strong>
                {data.pool.connections} / {data.pool.max_concurrency}
              </strong>
              <p>连接池 / 最大并发 · 批量上限 {data.pool.batch_size}</p>
            </section>
            <section>
              <h3>多级缓存</h3>
              <strong>
                {data.cache.l1_size} / {data.cache.l1_capacity}
              </strong>
              <p>L1 LRU 条目 · L2 Redis {data.cache.l2_enabled ? '已配置' : '未配置'}</p>
              <p>
                {Object.entries(data.cache.events)
                  .map(([k, v]) => `${k}: ${v}`)
                  .join(' · ')}
              </p>
            </section>
            <section>
              <h3>Langfuse</h3>
              <strong>{data.tracing.enabled ? '追踪已启用' : '本地追踪'}</strong>
              <p>
                内容采集：{data.tracing.capture_content ? '开启' : '关闭，仅元数据'} · 埋点错误{' '}
                {data.tracing.instrumentation_errors}
              </p>
              {Object.entries(data.dependencies).map(([k, v]) => (
                <p key={k}>
                  {k}：{v ? '正常' : '不可用'}
                </p>
              ))}
            </section>
          </div>
          {data.moderation && (
            <section className="workflow">
              <h2>内容安全策略</h2>
              <p>
                {data.moderation.backend === 'local' ? '本地规则基线' : '本地规则 + 语义分类'} ·{' '}
                {data.moderation.policy_version} · 故障时停止问答
              </p>
              <p className="subtle">
                {Object.entries(data.moderation.thresholds)
                  .map(([category, score]) => `${category}: ${score}`)
                  .join(' · ')}
              </p>
            </section>
          )}
          <h2>性能分位数</h2>
          <div className="document-table">
            <table>
              <thead>
                <tr>
                  <th>操作</th>
                  <th>样本 / 失败</th>
                  <th>p50</th>
                  <th>p95</th>
                  <th>p99</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(data.profiler).map(([k, v]) => (
                  <tr key={k}>
                    <td>{k}</td>
                    <td>
                      {v.count} / {v.errors}
                    </td>
                    <td>{v.p50_ms.toFixed(1)} ms</td>
                    <td>{v.p95_ms.toFixed(1)} ms</td>
                    <td>{v.p99_ms.toFixed(1)} ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <h2>告警状态</h2>
          <p className="subtle">normal → pending → firing；持续超过阈值后触发，恢复后回到 normal。</p>
          <div className="alert-grid">
            {data.alerts.map((a) => (
              <div key={a.name} className={`alert-card ${a.state}`}>
                <strong>{a.name}</strong>
                <span>{a.state}</span>
                <small>
                  当前 {a.value.toFixed(3)} · 阈值 {a.threshold}
                </small>
              </div>
            ))}
          </div>
          <details>
            <summary>告警状态变化记录（{data.history.length}）</summary>
            {[...data.history].reverse().map((h, i) => (
              <p key={i}>
                {new Date(h.at * 1000).toLocaleTimeString()} · {h.name} · {h.from} → {h.to}
              </p>
            ))}
          </details>
          <div className="section-title">
            <h2>请求追踪</h2>
            <select aria-label="追踪筛选" value={trace} onChange={(e) => setTrace(e.target.value)}>
              <option value="">全部近期请求</option>
              {[...new Set(data.spans.map((s) => s.trace_id))].map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </div>
          <div className="document-table">
            <table>
              <thead>
                <tr>
                  <th>Trace ID / Span</th>
                  <th>操作</th>
                  <th>耗时</th>
                  <th>结果</th>
                </tr>
              </thead>
              <tbody>
                {[...spans].reverse().map((s) => (
                  <tr key={s.span_id}>
                    <td>
                      <button onClick={() => setTrace(s.trace_id)}>{s.trace_id.slice(0, 12)}…</button>
                      <small>
                        {s.span_id} {s.parent_id ? `← ${s.parent_id}` : '根节点'}
                      </small>
                    </td>
                    <td>{s.name}</td>
                    <td>{s.duration_ms.toFixed(1)} ms</td>
                    <td>{s.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
