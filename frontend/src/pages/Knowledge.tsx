import { useEffect, useRef, useState } from 'react';
import { BookOpen, FileText, MessageSquare, Search, Trash2, Upload } from 'lucide-react';
import { api } from '../services/api';
import { useSession } from '../stores/session';
import { Sources } from '../components/AnswerCard';
import type { KnowledgeDocument, Source } from '../types';
import { KnowledgeAccess } from '../components/KnowledgeAccess';
import type { KnowledgeBase } from '../components/KnowledgeAccess';

export function Knowledge({ onAsk }: { onAsk?: (id: string) => void }) {
  const [allDocuments, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [filter, setFilter] = useState('');
  const [baseFilter, setBaseFilter] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<Source[] | null>(null);
  const [pendingDelete, setPendingDelete] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const canWrite = useSession(
    (s) => s.user?.permissions?.includes('knowledge.write') ?? s.user?.role === 'admin',
  );
  const [bases, setBases] = useState<KnowledgeBase[]>([]),
    [baseId, setBaseId] = useState('');
  const admin = canWrite && bases.some((b) => b.id === baseId && b.access !== 'read');
  const current = bases.find((b) => b.id === baseId);
  const documents = allDocuments.filter((d) => d.base_id === baseId);
  const visibleDocuments = documents.filter((d) =>
    d.filename.toLocaleLowerCase().includes(filter.trim().toLocaleLowerCase()),
  );
  const loadBases = async () => {
    const values = await api<KnowledgeBase[]>('/knowledge/bases');
    setBases(values);
    setBaseId((id) => (values.some((b) => b.id === id) ? id : values[0]?.id || ''));
  };
  const refresh = () => api<KnowledgeDocument[]>('/knowledge/documents').then(setDocuments);
  useEffect(() => {
    let active = true;
    Promise.all([api<KnowledgeDocument[]>('/knowledge/documents'), api<KnowledgeBase[]>('/knowledge/bases')])
      .then(([docs, values]) => {
        if (active) {
          setDocuments(docs);
          setBases(values);
          setBaseId(values[0]?.id || '');
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);
  function selectBase(id: string) {
    if (busy) return;
    setBaseId(id);
    setResults(null);
    setPendingDelete('');
    setNotice('');
    setFilter('');
    setError('');
  }
  async function upload(files: FileList | null) {
    if (!files?.length || !baseId || busy) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      let added = 0,
        duplicates = 0;
      for (const file of Array.from(files)) {
        const form = new FormData();
        form.append('file', file);
        form.append('base_id', baseId);
        const doc = await api<KnowledgeDocument>('/knowledge/documents', { method: 'POST', body: form });
        if (doc.duplicate) duplicates++;
        else added++;
      }
      setNotice(`已导入 ${added} 个文件，跳过 ${duplicates} 个重复文件。`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      await refresh().catch(() => {});
      setBusy(false);
      if (input.current) input.current.value = '';
    }
  }
  async function remove(id: string) {
    setBusy(true);
    setError('');
    try {
      await api(`/knowledge/documents/${id}`, { method: 'DELETE' });
      setPendingDelete('');
      setResults(null);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function search(e: React.FormEvent) {
    e.preventDefault();
    if (!baseId || !query.trim() || busy) return;
    setBusy(true);
    setError('');
    setResults(null);
    try {
      setResults(
        (
          await api<{ results: Source[] }>('/knowledge/search', {
            method: 'POST',
            body: JSON.stringify({ query, top_k: 4, base_ids: [baseId] }),
          })
        ).results,
      );
    } catch (error) {
      setError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="content-page library-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">KNOWLEDGE LIBRARY</span>
          <h1>每一份知识，都有归处。</h1>
          <p className="subtle">按主题组织资料，选择一个或多个知识库开始问答。</p>
        </div>
        <input
          type="file"
          ref={input}
          hidden
          multiple
          accept=".md,.markdown,.txt"
          onChange={(e) => void upload(e.target.files)}
        />
      </div>
      <div className="library-layout">
        <aside className="base-sidebar">
          <div className="section-title">
            <h2>知识库</h2>
            <span>{bases.length} 个可用</span>
          </div>
          <label className="scope-search">
            <Search size={15} />
            <input
              aria-label="筛选知识库"
              value={baseFilter}
              onChange={(e) => setBaseFilter(e.target.value)}
              placeholder="查找知识库"
            />
          </label>
          <nav aria-label="知识库列表" className="base-list">
            {bases
              .filter((b) => b.name.toLocaleLowerCase().includes(baseFilter.trim().toLocaleLowerCase()))
              .map((base) => (
                <button
                  key={base.id}
                  disabled={busy}
                  aria-pressed={base.id === baseId}
                  className={base.id === baseId ? 'selected' : ''}
                  onClick={() => selectBase(base.id)}
                >
                  <BookOpen size={18} />
                  <span>
                    <strong>{base.name}</strong>
                    <small>
                      {allDocuments.filter((d) => d.base_id === base.id).length} 份文档 ·{' '}
                      {base.access === 'read' ? '只读' : base.access === 'manage' ? '可管理' : '可编辑'}
                    </small>
                  </span>
                </button>
              ))}
          </nav>
          {!loading && !bases.length && (
            <p className="scope-empty">还没有可访问的知识库。可在下方创建，或联系管理员授权。</p>
          )}
          {!!bases.length &&
            !bases.some((b) =>
              b.name.toLocaleLowerCase().includes(baseFilter.trim().toLocaleLowerCase()),
            ) && <p className="scope-empty">没有匹配的知识库。</p>}
          <details className="library-settings" open={!loading && !bases.length}>
            <summary>新建知识库 / 访问授权</summary>
            <KnowledgeAccess
              key={baseId}
              bases={bases}
              selected={baseId}
              onSelect={selectBase}
              onRefresh={loadBases}
              hideSelect
            />
          </details>
        </aside>
        <section className="base-content" aria-label="当前知识库资料" aria-busy={loading || busy}>
          <div className="base-heading">
            <div>
              <span className="eyebrow">CURRENT LIBRARY</span>
              <h2>{loading ? '正在加载…' : current?.name || '选择或创建知识库'}</h2>
              <p>上传、文档列表与检索测试均限定在当前知识库。</p>
            </div>
            <div className="base-actions">
              {onAsk && current && (
                <button className="secondary" disabled={busy} onClick={() => onAsk(baseId)}>
                  <MessageSquare size={16} />
                  向此库提问
                </button>
              )}
              {admin && (
                <button className="primary" disabled={busy} onClick={() => input.current?.click()}>
                  <Upload size={17} />
                  {busy ? '处理中…' : '上传文档'}
                </button>
              )}
            </div>
          </div>
          <div className="library-stats">
            <div>
              <strong>{documents.length}</strong>
              <span>当前库文档</span>
            </div>
            <div>
              <strong>{documents.reduce((n, d) => n + d.chunk_count, 0)}</strong>
              <span>知识片段</span>
            </div>
            <div>
              <strong>{documents.filter((d) => d.status === 'ready').length}</strong>
              <span>已就绪文档</span>
            </div>
          </div>
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          {notice && (
            <p role="status" className="notice">
              {notice}
            </p>
          )}
          <div className="section-title">
            <h2>文档 · {documents.length}</h2>
            <label className="document-filter">
              <Search size={15} />
              <input
                aria-label="筛选文档"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="搜索文件名"
              />
            </label>
          </div>
          <div className="document-table">
            {loading ? (
              <div className="empty" role="status">
                正在读取资料…
              </div>
            ) : !visibleDocuments.length ? (
              <div className="empty">
                <FileText size={34} />
                <h3>{filter ? '没有匹配的文档' : current ? '从第一份资料开始' : '先选择一个知识库'}</h3>
                <p>
                  {filter
                    ? '换一个文件名关键词试试。'
                    : admin
                      ? '支持 UTF-8 编码的 Markdown / TXT 文件，可一次上传多份。'
                      : '当前库暂无资料，请联系有编辑权限的成员上传。'}
                </p>
                {!filter && admin && (
                  <button className="secondary" disabled={busy} onClick={() => input.current?.click()}>
                    上传到此知识库
                  </button>
                )}
              </div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>文档名称</th>
                    <th>状态</th>
                    <th>片段</th>
                    <th>导入日期</th>
                    {admin && <th>操作</th>}
                  </tr>
                </thead>
                <tbody>
                  {visibleDocuments.map((doc) => (
                    <tr key={doc.id}>
                      <td>
                        <FileText size={17} />
                        <span title={doc.filename}>{doc.filename}</span>
                      </td>
                      <td>
                        <span className={`status ${doc.status}`}>
                          {(
                            {
                              ready: '已就绪',
                              indexing: '索引中',
                              failed: '失败',
                              deleting: '待清理',
                            } as Record<string, string>
                          )[doc.status] || doc.status}
                        </span>
                        {doc.error && <small>{doc.error}</small>}
                      </td>
                      <td>{doc.chunk_count}</td>
                      <td>{new Date(doc.created_at * 1000).toLocaleDateString('zh-CN')}</td>
                      {admin && (
                        <td>
                          {pendingDelete === doc.id ? (
                            <div className="confirm-delete">
                              <button disabled={busy} onClick={() => void remove(doc.id)}>
                                确认删除
                              </button>
                              <button onClick={() => setPendingDelete('')}>取消</button>
                            </div>
                          ) : (
                            <button
                              className="icon-button"
                              aria-label={`删除 ${doc.filename}`}
                              disabled={busy || doc.can_write === false}
                              onClick={() => setPendingDelete(doc.id)}
                            >
                              <Trash2 size={16} />
                            </button>
                          )}
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          <div className="retrieval-test">
            <h2>检索试验台</h2>
            <p className="subtle">只检索「{current?.name || '未选择知识库'}」中的资料，查看片段与来源。</p>
            <form onSubmit={search}>
              <Search size={18} />
              <input
                aria-label="检索关键词"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="输入专业术语或完整问题"
                required
                maxLength={4000}
              />
              <button className="primary" disabled={busy || loading || !baseId || !query.trim()}>
                检索
              </button>
            </form>
            {results &&
              (results.length ? <Sources sources={results} /> : <p className="subtle">没有找到相关资料。</p>)}
          </div>
        </section>
      </div>
    </div>
  );
}
