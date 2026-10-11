// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SecurityAdmin, AccountSecurity } from '../pages/Security';
import { KnowledgeAccess } from './KnowledgeAccess';
import { useSession } from '../stores/session';
import { api } from '../services/api';
vi.mock('../services/api', () => ({ api: vi.fn() }));
const mocked = vi.mocked(api);
beforeEach(() => {
  vi.resetAllMocks();
  useSession.getState().login('t', {
    id: 'admin',
    username: 'admin',
    role: 'admin',
    permissions: ['users.manage', 'roles.manage', 'audit.read', 'knowledge.create', 'knowledge.share'],
  });
});
afterEach(() => {
  cleanup();
  useSession.getState().logout();
});

test('administrator can create accounts and assign custom role permissions', async () => {
  mocked.mockImplementation(async (path) => {
    if (path === '/admin/users') return [];
    if (path === '/admin/roles')
      return {
        catalog: { 'knowledge.read': '读取知识库' },
        roles: [{ id: 'user', permissions: [], builtin: true }],
      };
    if (path === '/admin/audit?limit=100') return [];
    return undefined;
  });
  render(<SecurityAdmin />);
  await screen.findByText('读取知识库');
  fireEvent.change(screen.getByLabelText('新账号用户名'), { target: { value: 'newuser' } });
  fireEvent.change(screen.getByLabelText('初始密码'), { target: { value: 'temporary-password' } });
  fireEvent.click(screen.getByRole('button', { name: '创建（首次登录须改密）' }));
  await waitFor(() =>
    expect(mocked).toHaveBeenCalledWith('/admin/users', {
      method: 'POST',
      body: JSON.stringify({ username: 'newuser', password: 'temporary-password', role: 'user' }),
    }),
  );
  fireEvent.change(screen.getByLabelText('角色标识'), { target: { value: 'reader' } });
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('button', { name: '保存角色' }));
  await waitFor(() =>
    expect(mocked).toHaveBeenCalledWith('/admin/roles', {
      method: 'POST',
      body: JSON.stringify({ id: 'reader', permissions: ['knowledge.read'] }),
    }),
  );
});

test('knowledge owner grants and revokes scoped read access', async () => {
  mocked.mockResolvedValue([{ subject_type: 'user', subject_id: 'other', access: 'read' }]);
  render(
    <KnowledgeAccess
      bases={[{ id: 'private', name: '私人资料', owner_id: 'admin', access: 'manage' }]}
      selected="private"
      onSelect={vi.fn()}
      onRefresh={async () => {}}
    />,
  );
  fireEvent.click(screen.getByRole('button', { name: '管理授权' }));
  await screen.findByText(/user \/ other \/ read/);
  fireEvent.click(screen.getByRole('button', { name: '撤销' }));
  await waitFor(() =>
    expect(mocked).toHaveBeenCalledWith('/knowledge/bases/private/grants', {
      method: 'PUT',
      body: JSON.stringify({ subject_type: 'user', subject_id: 'other', access: null }),
    }),
  );
});

test('administrator edits user permissions with shared impact visible and deletion disabled', async () => {
  mocked.mockImplementation(async (path) => {
    if (path === '/admin/roles')
      return {
        catalog: { 'chat.use': '问答与本人会话', 'knowledge.create': '创建私有知识库' },
        roles: [
          { id: 'admin', permissions: ['chat.use', 'knowledge.create'], builtin: true },
          { id: 'user', permissions: ['chat.use', 'knowledge.create'], builtin: true },
        ],
      };
    return [];
  });
  render(<SecurityAdmin />);
  const edit = await screen.findByRole('button', { name: 'user（内置）' });
  expect((edit as HTMLButtonElement).disabled).toBe(false);
  expect((screen.getByRole('button', { name: 'admin（内置）' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(edit);
  expect(screen.getByText(/保存后将影响所有使用 user 角色的账号/)).toBeTruthy();
  expect((screen.getByRole('button', { name: '删除未使用角色' }) as HTMLButtonElement).disabled).toBe(true);
  const create = screen.getByRole('checkbox', { name: /创建私有知识库/ });
  expect((create as HTMLInputElement).checked).toBe(true);
  fireEvent.click(create);
  fireEvent.click(screen.getByRole('button', { name: '保存角色' }));
  await waitFor(() =>
    expect(mocked).toHaveBeenCalledWith('/admin/roles', {
      method: 'POST',
      body: JSON.stringify({ id: 'user', permissions: ['chat.use'] }),
    }),
  );
  await screen.findByRole('status');
});

test('delegated role manager sees user role as protected', async () => {
  useSession
    .getState()
    .login('t', { id: 'manager', username: 'manager', role: 'manager', permissions: ['roles.manage'] });
  mocked.mockResolvedValue({
    catalog: { 'chat.use': '问答' },
    roles: [{ id: 'user', permissions: ['chat.use'], builtin: true }],
  });
  render(<SecurityAdmin />);
  const edit = await screen.findByRole('button', { name: 'user（内置）' });
  expect((edit as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('角色标识'), { target: { value: 'user' } });
  expect((screen.getByRole('button', { name: '保存角色' }) as HTMLButtonElement).disabled).toBe(true);
});

test('first login forces password change and clears local login after server revocation', async () => {
  useSession
    .getState()
    .login('t', { id: 'user', username: 'user', role: 'user', must_change_password: true });
  mocked.mockResolvedValue(undefined);
  render(<AccountSecurity />);
  fireEvent.change(screen.getByLabelText('原密码'), { target: { value: 'initial-password' } });
  fireEvent.change(screen.getByLabelText('新密码'), { target: { value: 'new-secure-password' } });
  fireEvent.click(screen.getByRole('button', { name: '修改并退出所有会话' }));
  await waitFor(() => expect(useSession.getState().token).toBe(''));
  expect(mocked).toHaveBeenCalledWith('/auth/password', {
    method: 'POST',
    body: JSON.stringify({ old_password: 'initial-password', new_password: 'new-secure-password' }),
  });
});
