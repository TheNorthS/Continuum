import { useEffect, useState } from 'react';
import { api } from '../services/api';
import { useSession } from '../stores/session';
import { GraphCanvas } from '../components/GraphCanvas';
import type { GraphData } from '../components/GraphCanvas';

interface GraphDocument {
  can_write?: boolean;
  id: string;
  filename: string;
  status: string;
  graph_status: string;
  graph_error?: string;
}
interface Status {
  enabled: boolean;
  mode: string;
  documents: GraphDocument[];
}
interface Inference {
  source: string;
  predicate: string;
  target: string;
  rule: string;
}
export function KnowledgeGraph() {
  const [status, setStatus] = useState<Status | null>(null),
    [data, setData] = useState<GraphData>({ nodes: [], links: [] });
  const [query, setQuery] = useState(''),
    [selected, setSelected] = useState(''),
    [target, setTarget] = useState('');
  const [paths, setPaths] = useState<GraphData[]>([]),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false);
  const admin = useSession((s) => s.user?.permissions?.includes('graph.write') ?? s.user?.role === 'admin');
  const [inferences, setInferences] = useState<Inference[]>([]);
  async function refresh(search = query) {
    const s = await api<Status>('/graph/status');
    setStatus(s);
    if (s.enabled) {
      setData(await api<GraphData>(`/graph/network?query=${encodeURIComponent(search)}&limit=150`));
      setPaths([]);
      setInferences([]);
    }
  }
  useEffect(() => {
    void refresh('').catch((e) => setError(e.message));
  }, []);
  async function rebuild(id: string) {
    setBusy(true);
    setError('');
    try {
      await api(`/graph/documents/${id}/rebuild`, { method: 'POST' });
      await refresh();
    } catch (e) {
      setError((e as Error).message);
      await refresh().catch(() => {});
    } finally {
      setBusy(false);
    }
  }
  async function reason() {
    setBusy(true);
    setError('');
    try {
      const result = await api<{ paths: GraphData[]; inferences: Inference[] }>('/graph/paths', {
        method: 'POST',
        body: JSON.stringify({ source: selected, target, hops: 3 }),
      });
      setPaths(result.paths);
      setInferences(result.inferences || []);
      if (!result.paths.length) setError('三跳以内没有找到资料支持的关联路径。');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const node = data.nodes.find((n) => n.id === selected);
  const evidence = data.links.filter((l) => l.source === selected || l.target === selected);
  return (
    <div className="content-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">KNOWLEDGE GRAPH</span>
          <h1>让知识之间的关系可见。</h1>
          <p className="subtle">实体识别、证据溯源与多跳关联探索，适用于你的各类专业资料。</p>
        </div>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {status && !status.enabled && (
        <p className="notice">图谱服务尚未启用。按项目图谱指南配置 Neo4j 后，即可构建已有文档的实体关系。</p>
      )}
      {status?.mode === 'demo' && (
        <p className="notice">
          演示模式仅识别“实体 --关系--&gt;
          实体”形式的明确关系；真实模式使用模型识别实体与关系，并核对原文证据。
        </p>
      )}
      {inferences.map((item, index) => (
        <div className="notice" key={index}>
          规则推理：{item.source} —{item.predicate}→ {item.target}（{item.rule}，证据见下方路径）
        </div>
      ))}
      {status?.enabled && (
        <>
          <form
            className="graph-search"
            onSubmit={(e) => {
              e.preventDefault();
              void refresh().catch((e) => setError(e.message));
            }}
          >
            <input
              aria-label="搜索图谱实体"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索实体，留空查看全图"
              maxLength={200}
            />
            <button className="primary" disabled={busy}>
              搜索图谱
            </button>
          </form>
          <GraphCanvas data={data} selected={selected} onSelect={setSelected} />
          {node && (
            <section className="graph-evidence" aria-label="选中实体的关系证据">
              <h2>
                {node.name} <small>{node.kind}</small>
              </h2>
              {!evidence.length && <p className="subtle">当前显示的子图中没有该实体的关系证据。</p>}
              {evidence.map((l) => (
                <blockquote key={l.id}>
                  <strong>{l.predicate}</strong>
                  <p>{l.evidence}</p>
                  <small>
                    {l.filename} · {l.extraction === 'demo' ? '明确三元组' : '模型抽取，需结合原文核实'}
                  </small>
                </blockquote>
              ))}
            </section>
          )}
          <p className="subtle">
            显示 {data.nodes.length} 个实体、{data.links.length} 条证据关系；单次最多 150
            个实体。无结果时可上传资料或重建图谱。
          </p>
          <div className="graph-path-controls">
            <label>
              起点
              <select aria-label="路径起点" value={selected} onChange={(e) => setSelected(e.target.value)}>
                <option value="">选择实体</option>
                {data.nodes.map((n) => (
                  <option key={n.id} value={n.id}>
                    {n.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              终点
              <select aria-label="路径终点" value={target} onChange={(e) => setTarget(e.target.value)}>
                <option value="">选择实体</option>
                {data.nodes.map((n) => (
                  <option key={n.id} value={n.id}>
                    {n.name}
                  </option>
                ))}
              </select>
            </label>
            <button
              className="primary"
              disabled={!selected || !target || selected === target || busy}
              onClick={() => void reason()}
            >
              探索三跳关系
            </button>
          </div>
          <p className="subtle">关联链保留每条边的原文证据与方向，不自动推断因果或传递关系。</p>
          {paths.map((path, i) => (
            <div className="graph-evidence" key={i}>
              <strong>{path.nodes.map((n) => n.name).join(' ↔ ')}</strong>
              {path.links.map((l) => (
                <p key={l.id}>
                  {l.predicate}：{l.evidence}
                  <small>{l.filename}</small>
                </p>
              ))}
            </div>
          ))}
        </>
      )}
      <h2>文档图谱状态</h2>
      <div className="document-table">
        <table>
          <thead>
            <tr>
              <th>文档</th>
              <th>图谱状态</th>
              {admin && <th>操作</th>}
            </tr>
          </thead>
          <tbody>
            {status?.documents.map((d) => (
              <tr key={d.id}>
                <td>{d.filename}</td>
                <td>
                  {
                    (
                      {
                        ready: '已构建',
                        indexing: '抽取中',
                        failed: '构建失败',
                        disabled: '未构建',
                      } as Record<string, string>
                    )[d.graph_status]
                  }
                  {d.graph_error && <small>{d.graph_error}</small>}
                </td>
                {admin && (
                  <td>
                    <button
                      disabled={!status.enabled || busy || d.status !== 'ready' || d.can_write === false}
                      onClick={() => void rebuild(d.id)}
                    >
                      {busy ? '处理中…' : '重建图谱'}
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
