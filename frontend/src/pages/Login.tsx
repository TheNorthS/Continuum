import { useEffect, useState } from 'react';
import { ArrowRight, Layers3, ShieldCheck } from 'lucide-react';
import { api } from '../services/api';
import { useSession } from '../stores/session';
import type { User } from '../types';

export function Login() {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [registrationEnabled, setRegistrationEnabled] = useState(false);
  const [registering, setRegistering] = useState(false);
  useEffect(() => {
    void api<{ registration_enabled: boolean }>('/auth/configuration')
      .then((v) => setRegistrationEnabled(Boolean(v.registration_enabled)))
      .catch(() => {});
  }, []);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      if (registering) {
        await api('/auth/register', { method: 'POST', body: JSON.stringify({ username, password }) });
      }
      const data = await api<{ access_token: string; refresh_token: string; user: User }>('/auth/login', {
        method: 'POST',
        body: JSON.stringify({ username, password }),
      });
      useSession.getState().login(data.access_token, data.user, data.refresh_token);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-page">
      <div className="login-story">
        <div className="brand">
          <Layers3 size={30} />
          <strong>RAG-E</strong>
          <span>KNOWLEDGE WORKSPACE</span>
        </div>
        <div className="story-copy">
          <div className="eyebrow">让知识，成为答案。</div>
          <h1>
            连接专业知识
            <br />
            与每一个好问题。
          </h1>
          <p>
            从研究资料到业务文档，
            <br />
            在自己的知识库中探索有据可依的答案。
          </p>
          <div className="story-line" />
          <small>混合检索 / 自适应问答 / 来源追溯</small>
        </div>
        <span className="story-foot">ENGINEERING EDITION · 01</span>
      </div>
      <div className="login-panel">
        <form onSubmit={submit}>
          <span className="eyebrow">WELCOME BACK</span>
          <h2>进入知识工作台</h2>
          <p className="subtle">登录后继续你的研究与探索。</p>
          <label>
            用户名
            <input
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              minLength={3}
            />
          </label>
          <label>
            密码
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={registering ? 8 : 1}
            />
          </label>
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          <button className="primary" disabled={busy}>
            {busy ? '处理中…' : registering ? '注册并登录' : '登录工作台'}
            <ArrowRight size={18} />
          </button>
          {registrationEnabled && (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setRegistering(!registering);
                setError('');
              }}
            >
              {registering ? '已有账号，返回登录' : '创建普通用户账号'}
            </button>
          )}
          <p className="login-note">
            <ShieldCheck size={15} /> 账号由管理员开通，权限由角色与知识库授权共同决定
          </p>
        </form>
      </div>
    </div>
  );
}
