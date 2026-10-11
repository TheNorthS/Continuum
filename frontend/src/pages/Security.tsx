import { useEffect, useState } from 'react';
import { api } from '../services/api';
import { useSession } from '../stores/session';
import type { User } from '../types';

interface Role {
  id: string;
  permissions: string[];
  builtin: boolean;
}
interface Audit {
  id: number;
  at: number;
  actor_id: string;
  action: string;
  resource: string;
  outcome: string;
  request_id: string;
  details: Record<string, unknown>;
}
interface Session {
  id: string;
  created_at: number;
  expires_at: number;
  revoked: boolean;
  current: boolean;
}

export function AccountSecurity() {
  const [sessions, setSessions] = useState<Session[]>([]),
    [old, setOld] = useState(''),
    [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const user = useSession((s) => s.user);
  const mustChange = user?.must_change_password;
  const refresh = () =>
    api<Session[]>('/auth/sessions')
      .then(setSessions)
      .catch((e) => setError(e.message));
  useEffect(() => {
    if (user && !mustChange) void refresh();
  }, [user, mustChange]);
  async function change(e: React.FormEvent) {
    e.preventDefault();
    try {
      await api('/auth/password', {
        method: 'POST',
        body: JSON.stringify({ old_password: old, new_password: password }),
      });
      useSession.getState().logout();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function revoke(id: string, current: boolean) {
    try {
      await api(`/auth/sessions/${id}`, { method: 'DELETE' });
      if (current) useSession.getState().logout();
      else await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <div className="content-page">
      <h1>账号安全</h1>
      {mustChange && (
        <p className="notice">请先修改管理员提供的初始密码。修改后所有登录会话将失效，请重新登录。</p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <form className="security-form" onSubmit={change}>
        <h2>修改密码</h2>
        <label>
          原密码
          <input
            aria-label="原密码"
            type="password"
            autoComplete="current-password"
            required
            minLength={1}
            value={old}
            onChange={(e) => setOld(e.target.value)}
          />
        </label>
        <label>
          新密码（至少12字符）
          <input
            aria-label="新密码"
            type="password"
            autoComplete="new-password"
            required
            minLength={12}
            maxLength={128}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <button className="primary">修改并退出所有会话</button>
      </form>
      {!mustChange && (
        <>
          <h2>登录会话</h2>
          <div className="document-table">
            <table>
              <thead>
                <tr>
                  <th>会话</th>
                  <th>登录时间</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((s) => (
                  <tr key={s.id}>
                    <td>
                      {s.id.slice(0, 12)} {s.current ? '（当前）' : ''}
                    </td>
                    <td>{new Date(s.created_at * 1000).toLocaleString()}</td>
                    <td>{s.revoked ? '已撤销' : s.expires_at * 1000 < Date.now() ? '已过期' : '有效'}</td>
                    <td>
                      <button disabled={s.revoked} onClick={() => void revoke(s.id, s.current)}>
                        撤销会话
                      </button>
                    </td>
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

export function SecurityAdmin() {
  const user = useSession((s) => s.user);
  const permissions = user?.permissions || [];
  const can = (p: string) => permissions.includes(p);
  const [users, setUsers] = useState<User[]>([]),
    [roles, setRoles] = useState<Role[]>([]),
    [catalog, setCatalog] = useState<Record<string, string>>({});
  const [events, setEvents] = useState<Audit[]>([]),
    [error, setError] = useState(''),
    [notice, setNotice] = useState('');
  const [username, setUsername] = useState(''),
    [password, setPassword] = useState(''),
    [role, setRole] = useState('user');
  const [roleId, setRoleId] = useState(''),
    [selected, setSelected] = useState<string[]>([]),
    [resetUser, setResetUser] = useState(''),
    [resetPassword, setResetPassword] = useState('');
  const [savingRole, setSavingRole] = useState(false);
  const protectedRole = roleId === 'admin' || (roleId === 'user' && user?.role !== 'admin');
  async function refresh() {
    if (can('users.manage')) setUsers(await api<User[]>('/admin/users'));
    if (can('roles.manage')) {
      const r = await api<{ catalog: Record<string, string>; roles: Role[] }>('/admin/roles');
      setRoles(r.roles);
      setCatalog(r.catalog);
    }
    if (can('audit.read')) setEvents(await api<Audit[]>('/admin/audit?limit=100'));
  }
  useEffect(() => {
    void refresh().catch((e) => setError(e.message));
  }, []);
  async function action(work: () => Promise<unknown>) {
    setError('');
    setNotice('');
    try {
      await work();
      await refresh();
      setNotice('操作已完成并记录审计日志。');
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <div className="content-page">
      <h1>账号、权限与审计</h1>
      <p className="subtle">
        角色决定操作权限，知识库授权决定资料范围。角色修改和停用账号后，后端立即校验新权限。
      </p>
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
      {can('users.manage') && (
        <>
          <form
            className="security-form"
            onSubmit={(e) => {
              e.preventDefault();
              void action(async () => {
                await api('/admin/users', {
                  method: 'POST',
                  body: JSON.stringify({ username, password, role }),
                });
                setPassword('');
                setUsername('');
              });
            }}
          >
            <h2>创建账号</h2>
            <label>
              用户名
              <input
                aria-label="新账号用户名"
                required
                pattern="[a-zA-Z0-9_.-]{3,64}"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </label>
            <label>
              初始密码
              <input
                aria-label="初始密码"
                type="password"
                autoComplete="new-password"
                required
                minLength={12}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
            <label>
              角色
              <select aria-label="新账号角色" value={role} onChange={(e) => setRole(e.target.value)}>
                {(roles.length ? roles : [{ id: 'user' }, { id: 'admin' }]).map((r) => (
                  <option key={r.id}>{r.id}</option>
                ))}
              </select>
            </label>
            <button className="primary">创建（首次登录须改密）</button>
          </form>
          <div className="document-table">
            <table>
              <thead>
                <tr>
                  <th>用户 / ID</th>
                  <th>角色</th>
                  <th>状态</th>
                  <th>管理</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.id}>
                    <td>
                      {u.username}
                      <small>{u.id}</small>
                    </td>
                    <td>
                      <select
                        aria-label={`修改 ${u.username} 的角色`}
                        value={u.role}
                        onChange={(e) =>
                          void action(() =>
                            api(`/admin/users/${u.id}`, {
                              method: 'PATCH',
                              body: JSON.stringify({ role: e.target.value }),
                            }),
                          )
                        }
                      >
                        {(roles.length ? roles : [{ id: u.role }]).map((r) => (
                          <option key={r.id}>{r.id}</option>
                        ))}
                      </select>
                    </td>
                    <td>{u.active ? '正常' : '已停用'}</td>
                    <td>
                      <button
                        onClick={() =>
                          void action(() =>
                            api(`/admin/users/${u.id}`, {
                              method: 'PATCH',
                              body: JSON.stringify({ active: !u.active }),
                            }),
                          )
                        }
                      >
                        {u.active ? '停用' : '启用'}
                      </button>{' '}
                      <button
                        onClick={() => {
                          setResetUser(u.id);
                          setResetPassword('');
                        }}
                      >
                        重置密码
                      </button>{' '}
                      {can('security.manage') && (
                        <button
                          onClick={() =>
                            void action(() => api(`/admin/users/${u.id}/revoke`, { method: 'POST' }))
                          }
                        >
                          撤销全部会话
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {resetUser && (
            <form
              className="security-form"
              onSubmit={(e) => {
                e.preventDefault();
                void action(async () => {
                  await api(`/admin/users/${resetUser}`, {
                    method: 'PATCH',
                    body: JSON.stringify({ password: resetPassword }),
                  });
                  setResetUser('');
                  setResetPassword('');
                });
              }}
            >
              <h2>重置 {users.find((u) => u.id === resetUser)?.username} 的密码</h2>
              <p>全部旧会话立即失效，用户下次登录必须改密。</p>
              <input
                aria-label="重置初始密码"
                type="password"
                minLength={12}
                autoComplete="new-password"
                required
                value={resetPassword}
                onChange={(e) => setResetPassword(e.target.value)}
              />
              <button className="primary">确认重置</button>
              <button type="button" onClick={() => setResetUser('')}>
                取消
              </button>
            </form>
          )}
        </>
      )}
      {can('roles.manage') && (
        <>
          <h2>角色与权限</h2>
          <p className="subtle">
            选择角色后勾选权限并保存；仅需修改一个账号时，可创建自定义角色并在账号列表中分配。
          </p>
          <div className="security-roles">
            {roles.map((r) => (
              <button
                key={r.id}
                disabled={savingRole || r.id === 'admin' || (r.id === 'user' && user?.role !== 'admin')}
                aria-pressed={roleId === r.id}
                onClick={() => {
                  setRoleId(r.id);
                  setSelected(r.permissions);
                  setNotice('');
                  setError('');
                }}
              >
                {r.id}
                {r.builtin ? '（内置）' : ''}
              </button>
            ))}
            <button
              type="button"
              disabled={savingRole}
              onClick={() => {
                setRoleId('');
                setSelected([]);
                setNotice('');
                setError('');
              }}
            >
              新建角色
            </button>
          </div>
          <form
            className="security-form"
            onSubmit={async (e) => {
              e.preventDefault();
              if (savingRole || protectedRole) return;
              setSavingRole(true);
              try {
                await action(() =>
                  api('/admin/roles', {
                    method: 'POST',
                    body: JSON.stringify({ id: roleId, permissions: selected }),
                  }),
                );
              } finally {
                setSavingRole(false);
              }
            }}
          >
            {roleId === 'user' && (
              <p className="notice">
                正在编辑 user 默认角色。保存后将影响所有使用 user
                角色的账号，新注册账号也使用这些权限。后端立即按新权限校验，用户可刷新页面更新界面。
              </p>
            )}
            <label>
              角色标识
              <input
                aria-label="角色标识"
                required
                pattern="[a-zA-Z0-9_-]{1,16}"
                value={roleId}
                disabled={savingRole}
                onChange={(e) => setRoleId(e.target.value)}
              />
            </label>
            <div className="permission-grid">
              {Object.entries(catalog).map(([p, label]) => (
                <label key={p}>
                  <input
                    type="checkbox"
                    checked={selected.includes(p)}
                    disabled={savingRole || protectedRole}
                    onChange={(e) =>
                      setSelected(e.target.checked ? [...selected, p] : selected.filter((v) => v !== p))
                    }
                  />
                  {label}
                  <small>{p}</small>
                </label>
              ))}
            </div>
            <button className="primary" disabled={savingRole || protectedRole || !roleId}>
              {savingRole ? '保存中…' : '保存角色'}
            </button>
            <button
              type="button"
              disabled={
                savingRole ||
                !roleId ||
                roleId === 'admin' ||
                roleId === 'user' ||
                roles.some((r) => r.id === roleId && r.builtin)
              }
              onClick={() => void action(() => api(`/admin/roles/${roleId}`, { method: 'DELETE' }))}
            >
              删除未使用角色
            </button>
          </form>
        </>
      )}
      {can('audit.read') && (
        <>
          <h2>安全审计</h2>
          <button
            className="primary"
            onClick={() =>
              void action(async () => {
                const result = await api<{ valid: boolean; events: number }>('/admin/audit/verify');
                if (!result.valid) throw new Error('审计链完整性校验失败');
              })
            }
          >
            校验审计链完整性
          </button>
          <div className="document-table">
            <table>
              <thead>
                <tr>
                  <th>序号 / 时间</th>
                  <th>操作者</th>
                  <th>操作 / 资源</th>
                  <th>结果</th>
                  <th>脱敏详情</th>
                </tr>
              </thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.id}>
                    <td>
                      {e.id}
                      <small>{new Date(e.at * 1000).toLocaleString()}</small>
                    </td>
                    <td>{e.actor_id || '匿名/系统'}</td>
                    <td>
                      {e.action}
                      <small>{e.resource}</small>
                    </td>
                    <td>{e.outcome}</td>
                    <td>
                      <details>
                        <summary>查看</summary>
                        <pre>{JSON.stringify(e.details, null, 2)}</pre>
                        <small>{e.request_id}</small>
                      </details>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <button
            disabled={!events.length}
            onClick={() =>
              void api<Audit[]>(`/admin/audit?after=${events.at(-1)!.id}&limit=100`)
                .then(setEvents)
                .catch((e) => setError(e.message))
            }
          >
            下一页
          </button>
        </>
      )}
    </div>
  );
}
