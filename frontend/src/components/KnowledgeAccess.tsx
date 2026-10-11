import { useEffect, useState } from 'react';
import { api } from '../services/api';
import { useSession } from '../stores/session';

export interface KnowledgeBase {
  id: string;
  name: string;
  owner_id: string;
  access: 'read' | 'write' | 'manage';
}
interface Grant {
  subject_type: 'user' | 'role';
  subject_id: string;
  access: 'read' | 'write' | 'manage';
}
export function KnowledgeAccess({
  bases,
  selected,
  onSelect,
  onRefresh,
  hideSelect = false,
}: {
  bases: KnowledgeBase[];
  selected: string;
  onSelect: (id: string) => void;
  onRefresh: () => Promise<void>;
  hideSelect?: boolean;
}) {
  const user = useSession((s) => s.user);
  const permissions = user?.permissions || [];
  const [name, setName] = useState(''),
    [grants, setGrants] = useState<Grant[] | null>(null),
    [subject, setSubject] = useState(''),
    [subjectType, setSubjectType] = useState<'user' | 'role'>('user'),
    [access, setAccess] = useState('read'),
    [error, setError] = useState('');
  const current = bases.find((b) => b.id === selected);
  useEffect(() => {
    setGrants(null);
    setError('');
  }, [selected]);
  async function edit(subject_type: 'user' | 'role', subject_id: string, level: string | null) {
    try {
      await api(`/knowledge/bases/${selected}/grants`, {
        method: 'PUT',
        body: JSON.stringify({ subject_type, subject_id, access: level }),
      });
      setGrants(await api<Grant[]>(`/knowledge/bases/${selected}/grants`));
      await onRefresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <section className="security-form">
      <h2>知识库与访问授权</h2>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!hideSelect && (
        <label>
          当前知识库
          <select
            aria-label="当前知识库"
            value={selected}
            onChange={(e) => {
              onSelect(e.target.value);
              setGrants(null);
              setError('');
            }}
          >
            <option value="">选择知识库</option>
            {bases.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name} · {b.access}
              </option>
            ))}
          </select>
        </label>
      )}
      {permissions.includes('knowledge.create') && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setError('');
            void api<KnowledgeBase>('/knowledge/bases', { method: 'POST', body: JSON.stringify({ name }) })
              .then(async (b) => {
                await onRefresh();
                onSelect(b.id);
                setName('');
              })
              .catch((e) => setError(e.message));
          }}
        >
          <input
            aria-label="新知识库名称"
            placeholder="新知识库名称（默认私有）"
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <button>创建私有知识库</button>
        </form>
      )}
      {current?.access === 'manage' && permissions.includes('knowledge.share') && (
        <>
          <button
            onClick={() =>
              void api<Grant[]>(`/knowledge/bases/${selected}/grants`)
                .then(setGrants)
                .catch((e) => setError(e.message))
            }
          >
            管理授权
          </button>
          {grants && (
            <>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void edit(subjectType, subject, access);
                }}
              >
                <select
                  aria-label="授权对象类型"
                  value={subjectType}
                  onChange={(e) => setSubjectType(e.target.value as 'user' | 'role')}
                >
                  <option value="user">用户 ID</option>
                  <option value="role">角色标识</option>
                </select>
                <input
                  aria-label="授权对象"
                  required
                  value={subject}
                  onChange={(e) => setSubject(e.target.value)}
                  placeholder="从账号管理复制用户 ID，或填写角色标识"
                />
                <select aria-label="知识库权限" value={access} onChange={(e) => setAccess(e.target.value)}>
                  <option value="read">可读</option>
                  <option value="write">可编辑</option>
                  <option value="manage">可管理授权</option>
                </select>
                <button>保存授权</button>
              </form>
              {grants.map((g) => (
                <p key={g.subject_type + g.subject_id}>
                  {g.subject_type} / {g.subject_id} / {g.access}{' '}
                  <button onClick={() => void edit(g.subject_type, g.subject_id, null)}>撤销</button>
                </p>
              ))}
            </>
          )}
        </>
      )}
      <p className="subtle">检索和图谱仅使用你有权读取的资料；编辑权限还需角色允许。会话始终归个人所有。</p>
    </section>
  );
}
