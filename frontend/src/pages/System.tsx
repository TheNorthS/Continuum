import { useEffect, useState } from 'react';
import { Activity, Database, GitBranch, ShieldCheck } from 'lucide-react';
import { api } from '../services/api';
import { useSession } from '../stores/session';
import type { SystemInfo } from '../types';

export function System() {
  const [info, setInfo] = useState<SystemInfo | null>(null);
  const [health, setHealth] = useState<Record<string, string>>({});
  const [stats, setStats] = useState<Record<string, number>>({});
  const [workflow, setWorkflow] = useState('');
  const [error, setError] = useState('');
  const admin = useSession((s) => s.user?.permissions?.includes('monitor.read') ?? s.user?.role === 'admin');
  useEffect(() => {
    Promise.all([
      api<SystemInfo>('/system').then(setInfo),
      api<{ checks: Record<string, string> }>('/health/ready').then((r) => setHealth(r.checks)),
      api<{ mermaid: string }>('/agents/workflow').then((r) => setWorkflow(r.mermaid)),
      ...(admin ? [api<Record<string, number>>('/admin/stats').then(setStats)] : []),
    ]).catch((e) => setError(e.message));
  }, [admin]);
  return (
    <div className="content-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">SYSTEM OVERVIEW</span>
          <h1>每一次回答，路径清晰。</h1>
          <p className="subtle">查看运行模式、基础服务与问答工作流。</p>
        </div>
      </div>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <div className="system-grid">
        <section>
          <Activity />
          <h3>运行模式</h3>
          <strong>
            {info?.mode === 'demo' ? '本地演示' : info?.mode === 'live' ? '真实模型' : '读取中'}
          </strong>
          <p>
            {info?.mode === 'demo'
              ? '使用确定性哈希向量与资料摘录，不执行模型质量评分。'
              : '调用配置的 LLM 与 Embedding 服务。'}
          </p>
        </section>
        <section>
          <Database />
          <h3>检索引擎</h3>
          <strong>{info?.vector_backend || '—'}</strong>
          <p>
            向量模型：{info?.embedding_provider} · 联网搜索：{info?.web_search_enabled ? '开启' : '关闭'}
          </p>
        </section>
        <section>
          <ShieldCheck />
          <h3>服务就绪</h3>
          {Object.entries(health).map(([key, value]) => (
            <p key={key}>
              {key}
              <span className="health-value">{value === 'ok' ? '正常' : value}</span>
            </p>
          ))}
        </section>
      </div>
      {Object.keys(stats).length > 0 && (
        <div className="library-stats">
          {Object.entries(stats).map(([k, v]) => (
            <div key={k}>
              <strong>{v}</strong>
              <span>
                {{ users: '用户', conversations: '会话', documents: '文档', chunks: '知识片段' }[k] || k}
              </span>
            </div>
          ))}
        </div>
      )}
      {info?.moderation && (
        <section className="workflow">
          <h2>
            <ShieldCheck size={20} /> 内容安全审查
          </h2>
          <p>
            当前：{info.moderation.backend === 'local' ? '本地规则基线' : '本地规则 + 语义分类'} · 策略{' '}
            {info.moderation.policy_version}
          </p>
          <p className="subtle">
            检查输入、检索资料和最终输出；审查异常时停止问答。本地规则覆盖有限，真实模式采用语义分类，仍可能出现漏判或误判。
          </p>
        </section>
      )}
      {info?.reranker && (
        <section className="workflow">
          <h2>
            <Activity size={20} /> 模型精排
          </h2>
          <p>
            当前：
            {!info.reranker.enabled
              ? '未启用 · RRF 融合排序'
              : info.reranker.provider === 'huggingface'
                ? '本地 Cross-Encoder'
                : '精排 API'}
            {info.reranker.model && <> · {info.reranker.model}</>}
          </p>
          {info.reranker.enabled && (
            <p>
              状态：
              {{
                ready: '已加载',
                not_loaded: '等待首次加载',
                configured: '已配置，调用时验证',
                error: '最近调用失败',
              }[info.reranker.state] || info.reranker.state}{' '}
              · 候选池：至少 {info.reranker.candidates} 条（不小于本次 Top-K） · 分数阈值：
              {info.reranker.min_score ?? '不设阈值'}
            </p>
          )}
          <p className="subtle">
            混合召回后对问题与候选片段逐对评分，再选取 Top-K。
            {info.reranker.failure_mode === 'rrf'
              ? '失败时回退 RRF，并在来源中标记。'
              : '启用后若精排失败，将返回错误。'}
            配置变化后重启后端生效。
          </p>
        </section>
      )}
      <section className="workflow">
        <h2>
          <GitBranch size={20} /> 自适应问答流程
        </h2>
        <div className="flow-row">
          {[
            '理解问题',
            info?.reranker?.enabled ? '混合检索 + 模型精排' : '混合检索',
            '相关性过滤',
            '生成回答',
            '质量检查',
          ].map((v, i) => (
            <div key={v}>
              <span>0{i + 1}</span>
              {v}
            </div>
          ))}
        </div>
        <p className="subtle">
          选库问答始终限定所选资料，资料不足时有限次改写或明确提示；未限定范围的兼容流程可按配置补充网络资料。
        </p>
        <details>
          <summary>查看实际工作流定义（Mermaid）</summary>
          <pre>{workflow}</pre>
        </details>
      </section>
    </div>
  );
}
