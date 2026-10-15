import { useEffect, useState } from 'react';
import { Activity, BookOpen, Layers3, LogOut, Menu, MessageSquare, Plus, Trash2, X } from 'lucide-react';
import { Login } from './pages/Login';
import { Chat } from './pages/Chat';
import { Knowledge } from './pages/Knowledge';
import { Space } from './pages/Space';
import { Memory } from './pages/Memory';
import { System } from './pages/System';
import { KnowledgeGraph } from './pages/KnowledgeGraph';
import { Observability } from './pages/Observability';
import { AccountSecurity, SecurityAdmin } from './pages/Security';
import { api } from './services/api';
import { useSession } from './stores/session';
import type { Conversation, SystemInfo, User } from './types';

export default function App() {
  const { token, user } = useSession();
  const [page, setPage] = useState('chat');
  const [selected, setSelected] = useState('');
  const [initialBaseIds, setInitialBaseIds] = useState<string[] | undefined>();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [busy, setBusy] = useState(false);
  const [menu, setMenu] = useState(false);
  const [info, setInfo] = useState<SystemInfo | null>(null);
  const [error, setError] = useState('');
  const [deleteId, setDeleteId] = useState('');
  function rememberConversation(id: string) {
    setSelected(id);
    // 这里只记住当前页面要打开哪个会话；附件选择本身存于服务端数据库。
    const uid = useSession.getState().user?.id;
    if (uid)
      try {
        const key = `rag-e:conversation:${uid}`;
        if (id) sessionStorage.setItem(key, id);
        else sessionStorage.removeItem(key);
      } catch {
        /* 浏览器禁用存储时，仍可从侧栏重新打开会话恢复附件。 */
      }
  }
  const refresh = () =>
    api<Conversation[]>('/conversations')
      .then(setConversations)
      .catch((e) => setError(e.message));
  useEffect(() => {
    if (!token) {
      setConversations([]);
      setSelected('');
      setInitialBaseIds(undefined);
      setInfo(null);
      setPage('chat');
      setBusy(false);
      return;
    }
    let active = true;
    const controller = new AbortController();
    const identity = api<User>('/auth/me', { signal: controller.signal })
      .then((u) => {
        if (active) useSession.getState().login(useSession.getState().token, u);
        return u;
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    Promise.all([identity, api<Conversation[]>('/conversations', { signal: controller.signal })])
      .then(([currentUser, data]) => {
        if (active) {
          setConversations(data);
          // 刷新时 user 初始为空，必须等待 /auth/me，不能与会话列表的返回顺序竞争。
          const uid = currentUser?.id;
          if (uid)
            try {
              const saved = sessionStorage.getItem(`rag-e:conversation:${uid}`);
              if (saved && data.some((c) => c.id === saved)) setSelected((current) => current || saved);
              else if (saved) sessionStorage.removeItem(`rag-e:conversation:${uid}`);
            } catch {
              /* 会话列表仍可正常使用。 */
            }
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    api<SystemInfo>('/system', { signal: controller.signal })
      .then((data) => {
        if (active) setInfo(data);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [token]);
  if (!token) return <Login />;
  if (!user)
    return (
      <div className="loading">
        正在加载工作台…
        {error && (
          <p role="alert">
            {error}
            <button onClick={() => useSession.getState().logout()}>返回登录</button>
          </p>
        )}
      </div>
    );
  if (user.must_change_password) return <AccountSecurity />;
  const can = (permission: string) => user.permissions?.includes(permission) ?? user.role === 'admin';
  function navigate(next: string) {
    if (busy) return;
    setPage(next);
    setMenu(false);
    setError('');
  }
  function select(id: string) {
    setInitialBaseIds(undefined);
    rememberConversation(id);
    setPage('chat');
    setMenu(false);
  }
  async function remove(id: string) {
    try {
      await api(`/conversations/${id}`, { method: 'DELETE' });
      if (selected === id) rememberConversation('');
      setDeleteId('');
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <div className="app-shell">
      {menu && <button aria-label="关闭侧栏" className="sidebar-overlay" onClick={() => setMenu(false)} />}
      <aside className={`sidebar ${menu ? 'open' : ''}`}>
        <div className="brand" aria-label="知恒 · Continuum">
          <Layers3 size={27} />
          <strong>知恒</strong>
          <span>CONTINUUM</span>
        </div>
        <button disabled={busy} className="new-chat" onClick={() => select('')}>
          <Plus size={18} />
          开启新对话
        </button>
        <nav>
          {[
            { id: 'chat', label: '知识问答', icon: MessageSquare },
            { id: 'knowledge', label: '知识库', icon: BookOpen },
            { id: 'space', label: '空间管理', icon: Layers3 },
            { id: 'memory', label: '记忆管理', icon: Layers3 },
            { id: 'system', label: '系统概览', icon: Activity },
            { id: 'graph', label: '知识图谱', icon: Layers3 },
            ...(can('monitor.read') ? [{ id: 'observability', label: '监控与追踪', icon: Activity }] : []),
            ...(['users.manage', 'roles.manage', 'audit.read'].some(can)
              ? [{ id: 'security', label: '账号与权限', icon: Layers3 }]
              : []),
            { id: 'account', label: '账号安全', icon: Layers3 },
          ].map(({ id, label, icon: Icon }) => (
            <button
              disabled={busy}
              className={page === id ? 'active' : ''}
              key={id}
              onClick={() => navigate(id)}
            >
              <Icon size={18} />
              {label}
              {page === id && <span className="nav-dot" />}
            </button>
          ))}
        </nav>
        <div className="history-heading">
          最近会话<span>{conversations.length}</span>
        </div>
        <div className="history-list">
          {conversations.map((c) => (
            <div
              key={c.id}
              className={`history-item ${selected === c.id && page === 'chat' ? 'selected' : ''}`}
            >
              <button disabled={busy} onClick={() => select(c.id)} title={c.title}>
                <MessageSquare size={14} />
                <span>{c.title}</span>
              </button>
              <button
                disabled={busy}
                className="history-delete"
                aria-label={`删除会话 ${c.title}`}
                onClick={() => setDeleteId(c.id)}
              >
                <Trash2 size={13} />
              </button>
            </div>
          ))}
          {!conversations.length && <p>你的探索将记录在这里</p>}
        </div>
        <div className="sidebar-footer">
          <div className="avatar">{user.username[0].toUpperCase()}</div>
          <div>
            <strong>{user.username}</strong>
            <span>{user.role}</span>
          </div>
          <button
            aria-label="退出登录"
            disabled={busy}
            onClick={() => {
              void api('/auth/logout', { method: 'POST' }).finally(() => useSession.getState().logout());
            }}
          >
            <LogOut size={17} />
          </button>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <div>
            <button className="mobile-menu icon-button" aria-label="打开导航" onClick={() => setMenu(true)}>
              <Menu size={20} />
            </button>
            <span>工作台</span>
            <b>/</b>
            <strong>
              {
                {
                  chat: '知识问答',
                  knowledge: '知识库',
                  space: '空间管理',
                  memory: '记忆管理',
                  system: '系统概览',
                  graph: '知识图谱',
                  observability: '监控与追踪',
                  security: '账号与权限',
                  account: '账号安全',
                }[page]
              }
            </strong>
          </div>
          <div className="runtime-badge">
            <span />
            {info?.mode === 'demo' ? '本地演示模式' : info?.mode === 'live' ? '真实模型模式' : '连接中'}
          </div>
        </header>
        {error && (
          <div role="alert" className="global-error">
            {error}
            <button aria-label="关闭错误提示" onClick={() => setError('')}>
              <X size={15} />
            </button>
          </div>
        )}
        {page === 'chat' ? (
          <Chat
            selected={selected}
            onSelect={rememberConversation}
            onUpdated={() => void refresh()}
            onBusy={setBusy}
            initialBaseIds={initialBaseIds}
            onManage={() => navigate('knowledge')}
          />
        ) : page === 'space' ? (
          <Space />
        ) : page === 'memory' ? (
          <Memory />
        ) : page === 'security' ? (
          <SecurityAdmin />
        ) : page === 'account' ? (
          <AccountSecurity />
        ) : page === 'graph' ? (
          <KnowledgeGraph />
        ) : page === 'observability' ? (
          <Observability />
        ) : page === 'knowledge' ? (
          <Knowledge
            onAsk={(id) => {
              setInitialBaseIds([id]);
              rememberConversation('');
              navigate('chat');
            }}
          />
        ) : (
          <System />
        )}
      </main>
      {deleteId && (
        <div className="modal-backdrop">
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="delete-title">
            <h2 id="delete-title">删除这段会话？</h2>
            <p>会话中的全部消息将被删除。</p>
            <div>
              <button onClick={() => setDeleteId('')}>取消</button>
              <button className="danger" onClick={() => void remove(deleteId)}>
                删除会话
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
