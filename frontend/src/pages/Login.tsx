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
        <div className="brand" aria-label="知恒 · Continuum">
          <Layers3 size={30} />
          <strong>知恒</strong>
          <span>CONTINUUM</span>
        </div>
        <div className="story-copy">
          <div className="eyebrow">让知识有据，让记忆延续。</div>
          <h1>
            知识与记忆
            <br />
            让对话持续生长。
          </h1>
          <p>知识与记忆双驱动的长程多模态智能体平台</p>
          <p lang="en">Knowledge- and Memory-Driven Platform for Long-Horizon Multimodal Agents</p>
          <div className="story-line" />
          <small>知识检索 / 长期记忆 / 上下文压缩 / 多模态理解</small>
        </div>
        <span className="story-foot">ENGINEERING EDITION · 01</span>
      </div>
      <div className="login-panel">
        <form onSubmit={submit}>
          <span className="eyebrow">WELCOME BACK</span>
          <h2>进入知恒 · Continuum</h2>
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
