// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import App from '../App';
import { Chat } from '../pages/Chat';
import { AnswerCard } from './AnswerCard';
import { useSession } from '../stores/session';
import { api, streamAnswer } from '../services/api';

vi.mock('../services/api', () => ({ api: vi.fn(), streamAnswer: vi.fn() }));
const apiMock = vi.mocked(api);
const streamMock = vi.mocked(streamAnswer);
const user = { id: 'one', username: 'admin', role: 'admin' as const };
const bases = [{ id: 'legacy', name: '共享资料', owner_id: 'one', access: 'manage' }];
const chatApi = async (path: string) =>
  path === '/knowledge/bases'
    ? bases
    : path.endsWith('/messages')
      ? []
      : { id: 'chat', base_ids: ['legacy'] };

beforeEach(() => {
  vi.resetAllMocks();
  useSession.getState().logout();
  sessionStorage.clear();
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(cleanup);

test('refresh restores the remembered conversation after identity arrives, even if the list arrives first', async () => {
  let resolveIdentity: (value: typeof user) => void = () => {};
  const saved = {
    id: 'a'.repeat(32),
    filename: '恢复.txt',
    media_type: 'text/plain',
    size: 10,
    storage_size: 20,
    text_chars: 4,
    image_count: 0,
    created_at: 1,
  };
  useSession.setState({ token: 'refresh-token', user: null });
  sessionStorage.setItem('rag-e:conversation:one', 'saved-chat');
  apiMock.mockImplementation(async (path) => {
    if (path === '/auth/me')
      return new Promise((resolve) => {
        resolveIdentity = resolve;
      });
    if (path === '/conversations') return [{ id: 'saved-chat', title: '恢复会话' }];
    if (path === '/conversations/saved-chat')
      return { id: 'saved-chat', use_knowledge: false, attachment_ids: [saved.id], attachments: [saved] };
    if (path === '/system') return { mode: 'demo' };
    return [];
  });
  render(<App />);
  await waitFor(() => expect(apiMock).toHaveBeenCalledWith('/conversations', expect.anything()));
  resolveIdentity(user);
  await waitFor(() =>
    expect(apiMock.mock.calls.map(([path]) => path)).toContain('/conversations/saved-chat'),
  );
  await screen.findByRole('button', { name: '移除附件 恢复.txt' });
  expect(screen.getByText('已恢复此会话的附件选择。')).toBeTruthy();
});

test('login enters the workspace and loads knowledge management', async () => {
  apiMock.mockImplementation(async (path: string) => {
    if (path === '/auth/login') return { access_token: 'demo-token', user };
    if (path === '/auth/me') return user;
    if (path === '/system') return { mode: 'demo' };
    if (path === '/knowledge/bases')
      return [{ id: 'legacy', name: '共享资料', owner_id: 'one', access: 'manage' }];
    if (path === '/knowledge/documents')
      return [
        { id: 'doc', base_id: 'legacy', filename: 'chip.md', status: 'ready', chunk_count: 2, created_at: 1 },
      ];
    return [];
  });
  render(<App />);
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'rag-e-demo-2026' } });
  fireEvent.click(screen.getByRole('button', { name: '登录工作台' }));
  await screen.findByText('从你的资料，找到答案。');
  expect(screen.getByText('本地演示模式')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '知识库' }));
  await screen.findByText('chip.md');
  expect(screen.getByRole('button', { name: '上传文档' })).toBeTruthy();
});

test('chat sends selected mode and shows streamed result with sources', async () => {
  apiMock.mockImplementation(chatApi);
  streamMock.mockImplementation(async (_id, _q, _mode, onStep) => {
    onStep({ node: 'retrieve', label: '混合检索知识库', sources: 1, rewrites: 0, generations: 0 });
    return {
      answer: '13.5 nm [1]',
      sources: [{ id: 's', text: 'EUV wavelength', score: 1, metadata: { filename: 'chip.md' } }],
      trace: [],
      status: 'demo',
      mode: 'adaptive',
      runtime: 'demo',
      conversation_id: 'chat',
    };
  });
  const onBusy = vi.fn();
  render(<Chat selected="chat" onSelect={vi.fn()} onUpdated={vi.fn()} onBusy={onBusy} />);
  await screen.findByText('从你的资料，找到答案。');
  fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: 'EUV 光刻波长？' } });
  fireEvent.click(screen.getByRole('button', { name: '发送问题' }));
  await screen.findByText('13.5 nm [1]');
  expect(screen.getAllByText('chip.md').some((element) => element.closest('summary'))).toBe(true);
  expect(streamMock.mock.calls[0].slice(0, 3)).toEqual(['chat', 'EUV 光刻波长？', 'adaptive']);
  expect(streamMock.mock.calls[0][5]).toEqual(['legacy']);
  await waitFor(() => expect(onBusy).toHaveBeenLastCalledWith(false));
});

test('new chat clears an in-flight history loading state', async () => {
  apiMock.mockImplementation((path) =>
    path === '/knowledge/bases' ? Promise.resolve(bases) : new Promise(() => {}),
  );
  const props = { onSelect: vi.fn(), onUpdated: vi.fn(), onBusy: vi.fn() };
  const view = render(<Chat selected="old" {...props} />);
  expect(screen.getByText('正在读取会话与知识库…')).toBeTruthy();
  view.rerender(<Chat selected="" {...props} />);
  await screen.findByText('从你的资料，找到答案。');
  fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: 'new question' } });
  expect((screen.getByRole('button', { name: '发送问题' }) as HTMLButtonElement).disabled).toBe(false);
});

test('retrieved markdown never executes HTML or loads remote images', () => {
  const view = render(
    <AnswerCard
      message={{
        id: 1,
        role: 'assistant',
        content:
          '<script>alert(1)</script>\n\n![track](https://example.org/track)\n\n[unsafe](javascript:alert(1))',
      }}
    />,
  );
  expect(view.container.querySelector('script')).toBeNull();
  expect(view.container.querySelector('img')).toBeNull();
  expect(view.container.querySelector('a')?.getAttribute('href')).toBeNull();
});

test('blocked response shows review status and does not expose citations', async () => {
  apiMock.mockImplementation(chatApi);
  streamMock.mockResolvedValue({
    answer: '本次内容未通过安全审查。',
    sources: [],
    trace: [],
    status: 'blocked',
    mode: 'adaptive',
    runtime: 'demo',
    conversation_id: 'chat',
    safety: { decision: 'blocked', backend: 'local', stage: 'input' },
  });
  render(<Chat selected="chat" onSelect={vi.fn()} onUpdated={vi.fn()} onBusy={vi.fn()} />);
  await screen.findByText('从你的资料，找到答案。');
  fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '安全测试问题' } });
  fireEvent.click(screen.getByRole('button', { name: '发送问题' }));
  await screen.findByText('本次内容未通过安全审查。');
  expect(screen.getByRole('status').textContent).toContain('未保存到聊天历史');
  expect(screen.queryByText(/参考来源/)).toBeNull();
});
