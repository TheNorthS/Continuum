// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { KnowledgeScopePicker } from './KnowledgeScopePicker';
import { Chat } from '../pages/Chat';
import { Knowledge } from '../pages/Knowledge';
import { api, streamAnswer } from '../services/api';
import type { KnowledgeBase } from './KnowledgeAccess';
import { useSession } from '../stores/session';

vi.mock('../services/api', () => ({ api: vi.fn(), streamAnswer: vi.fn() }));
const mocked = vi.mocked(api);
const stream = vi.mocked(streamAnswer);
const bases: KnowledgeBase[] = [
  { id: 'a', name: '研发手册', owner_id: 'u', access: 'manage' },
  { id: 'b', name: '生产规范', owner_id: 'u', access: 'read' },
];
const props = { onSelect: vi.fn(), onUpdated: vi.fn(), onBusy: vi.fn() };
beforeEach(() => {
  vi.resetAllMocks();
  Element.prototype.scrollIntoView = vi.fn();
  useSession.getState().login('t', {
    id: 'u',
    username: 'user',
    role: 'user',
    permissions: ['knowledge.read', 'knowledge.write', 'knowledge.create'],
  });
  mocked.mockImplementation(async (path) =>
    path === '/knowledge/bases' ? bases : path.endsWith('/messages') ? [] : { id: 'chat', base_ids: ['b'] },
  );
  stream.mockResolvedValue({
    answer: '已完成回答',
    sources: [],
    trace: [],
    status: 'demo',
    mode: 'adaptive',
    runtime: 'demo',
    conversation_id: 'chat',
  });
});
afterEach(() => {
  cleanup();
  useSession.getState().logout();
});

test('switching conversations closes the previous unsaved library selection', async () => {
  const view = render(<Chat selected="chat" {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: /问答范围.*生产规范/ }));
  fireEvent.click(screen.getByRole('checkbox', { name: /研发/ }));
  expect(screen.getByRole('button', { name: '应用范围（2）' })).toBeTruthy();
  view.rerender(<Chat selected="another" {...props} />);
  await screen.findByText('从你的资料，找到答案。');
  expect(screen.queryByLabelText('搜索知识库')).toBeNull();
  expect(
    mocked.mock.calls.some(([path, options]) => path.endsWith('/scope') && options?.method === 'PATCH'),
  ).toBe(false);
});

test('picker searches across libraries, preserves multi-selection and prevents an empty scope', async () => {
  const apply = vi.fn().mockResolvedValue(undefined);
  render(<KnowledgeScopePicker bases={bases} selected={['a']} disabled={false} onApply={apply} />);
  fireEvent.click(screen.getByRole('button', { name: /问答范围/ }));
  fireEvent.change(screen.getByLabelText('搜索知识库'), { target: { value: '生产' } });
  expect(screen.queryByRole('checkbox', { name: /研发/ })).toBeNull();
  fireEvent.click(screen.getByRole('checkbox', { name: /生产/ }));
  fireEvent.click(screen.getByRole('button', { name: '应用范围（2）' }));
  await waitFor(() => expect(apply).toHaveBeenCalledWith(['a', 'b']));
  await waitFor(() => expect(screen.queryByLabelText('搜索知识库')).toBeNull());
  fireEvent.click(screen.getByRole('button', { name: /问答范围/ }));
  fireEvent.click(screen.getByRole('button', { name: '清空' }));
  expect((screen.getByRole('button', { name: '应用范围' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.keyDown(screen.getByLabelText('搜索知识库'), { key: 'Escape' });
  expect(screen.queryByLabelText('搜索知识库')).toBeNull();
});

test('existing conversation restores saved scope and sends only the selected libraries', async () => {
  render(<Chat selected="chat" {...props} />);
  await screen.findByRole('button', { name: /问答范围.*生产规范/ });
  fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '概念说明' } });
  fireEvent.click(screen.getByRole('button', { name: '发送问题' }));
  await screen.findByText('已完成回答');
  expect(stream.mock.calls[0][5]).toEqual(['b']);
});

test('scope edits persist and rejected saves keep the original selection', async () => {
  mocked.mockImplementation(async (path, options) => {
    if (path.endsWith('/scope') && options?.method === 'PATCH') throw new Error('访问权限已变化');
    if (path === '/knowledge/bases') return bases;
    if (path.endsWith('/messages')) return [];
    return { id: 'chat', base_ids: ['a'] };
  });
  render(<Chat selected="chat" {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: /问答范围.*研发手册/ }));
  fireEvent.click(screen.getByRole('checkbox', { name: /生产/ }));
  fireEvent.click(screen.getByRole('button', { name: '应用范围（2）' }));
  await screen.findByRole('alert');
  expect(mocked).toHaveBeenCalledWith('/conversations/chat/scope', {
    method: 'PATCH',
    body: JSON.stringify({ base_ids: ['a', 'b'] }),
  });
  fireEvent.keyDown(screen.getByLabelText('搜索知识库'), { key: 'Escape' });
  expect(screen.getByRole('button', { name: /问答范围.*研发手册/ }).textContent).not.toContain('+');
});

test('new conversation inherits a library shortcut and includes scope in create and stream requests', async () => {
  render(<Chat selected="" initialBaseIds={['b']} {...props} />);
  await screen.findByRole('button', { name: /问答范围.*生产规范/ });
  fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '规范要求' } });
  fireEvent.click(screen.getByRole('button', { name: '发送问题' }));
  await screen.findByText('已完成回答');
  expect(mocked).toHaveBeenCalledWith(
    '/conversations',
    expect.objectContaining({ method: 'POST', body: JSON.stringify({ base_ids: ['b'] }) }),
  );
  expect(stream.mock.calls[0][5]).toEqual(['b']);
});

test('revoked saved scope cannot silently fall back to all accessible libraries', async () => {
  mocked.mockImplementation(async (path) =>
    path === '/knowledge/bases'
      ? bases
      : path.endsWith('/messages')
        ? []
        : { id: 'chat', base_ids: ['revoked'] },
  );
  render(<Chat selected="chat" {...props} />);
  await screen.findByText(/部分已选知识库已不可访问/);
  fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '问题' } });
  expect((screen.getByRole('button', { name: '发送问题' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: /问答范围/ }));
  fireEvent.click(screen.getByRole('checkbox', { name: /研发/ }));
  fireEvent.click(screen.getByRole('button', { name: '应用范围（1）' }));
  await waitFor(() =>
    expect((screen.getByRole('button', { name: '发送问题' }) as HTMLButtonElement).disabled).toBe(false),
  );
});

test('no accessible libraries defaults to ordinary chat without retrieving libraries', async () => {
  mocked.mockResolvedValue([]);
  const manage = vi.fn();
  render(<Chat selected="" {...props} onManage={manage} />);
  await screen.findByText('从一个问题，开始对话。');
  fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '问题' } });
  expect((screen.getByRole('button', { name: '发送问题' }) as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: '发送问题' }));
  await screen.findByText('已完成回答');
  expect(stream.mock.calls[0][5]).toBeUndefined();
  expect(stream.mock.calls[0][6]).toMatchObject({ use_knowledge: false });
});

test('knowledge documents, retrieval test and chat shortcut all follow the active library', async () => {
  mocked.mockImplementation(async (path) => {
    if (path === '/knowledge/bases') return bases;
    if (path === '/knowledge/documents')
      return [
        { id: 'da', base_id: 'a', filename: '研发.md', status: 'ready', chunk_count: 2, created_at: 1 },
        { id: 'db', base_id: 'b', filename: '生产.md', status: 'ready', chunk_count: 3, created_at: 1 },
      ];
    return { results: [] };
  });
  const ask = vi.fn();
  render(<Knowledge onAsk={ask} />);
  await screen.findByText('研发.md');
  expect(screen.queryByText('生产.md')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: /生产规范.*1 份文档/ }));
  expect(screen.queryByText('研发.md')).toBeNull();
  expect(screen.getByText('生产.md')).toBeTruthy();
  expect(screen.queryByRole('button', { name: '上传文档' })).toBeNull();
  fireEvent.change(screen.getByLabelText('检索关键词'), { target: { value: '生产流程' } });
  fireEvent.click(screen.getByRole('button', { name: '检索' }));
  await screen.findByText('没有找到相关资料。');
  expect(mocked).toHaveBeenCalledWith('/knowledge/search', {
    method: 'POST',
    body: JSON.stringify({ query: '生产流程', top_k: 4, base_ids: ['b'] }),
  });
  fireEvent.click(screen.getByRole('button', { name: '向此库提问' }));
  expect(ask).toHaveBeenCalledWith('b');
});
