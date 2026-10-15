// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Memory } from '../pages/Memory';
import { ContextPanel } from './ContextPanel';
import { api } from '../services/api';
vi.mock('../services/api', () => ({ api: vi.fn() }));
const request = vi.mocked(api);
beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);
const item = {
  id: 'm',
  category: 'fact',
  status: 'active',
  version: 1,
  base_id: null,
  payload: { subject: '青禾项目', predicate: '数据库', value: 'PostgreSQL' },
  evidence_ids: ['event'],
  updated_at: 1,
  expires_at: null,
};

test('memory management displays evidence and corrects a version before explicit forget', async () => {
  request.mockImplementation(async (path) => {
    if (path === '/memory')
      return { items: [item], preferences: { enabled: true, auto_extract: true }, jobs: [] };
    if (path.endsWith('/evidence'))
      return { events: [{ id: 'event', content: { text: '用户明确声明数据库为 PostgreSQL' } }] };
    if (path === '/knowledge/bases') return [];
    return {};
  });
  render(<Memory />);
  await screen.findByText('PostgreSQL');
  fireEvent.click(screen.getByRole('button', { name: '查看依据' }));
  await screen.findByText('用户明确声明数据库为 PostgreSQL');
  fireEvent.click(screen.getByRole('button', { name: '关闭依据' }));
  fireEvent.click(screen.getByRole('button', { name: '更正' }));
  fireEvent.change(screen.getByLabelText('记忆内容'), { target: { value: 'MySQL' } });
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(() =>
    expect(
      request.mock.calls.some(
        ([p, o]) =>
          p === '/memory/m' && o?.method === 'PATCH' && JSON.parse(o.body as string).expected_version === 1,
      ),
    ).toBe(true),
  );
  await waitFor(() => expect(screen.queryByRole('dialog', { name: '编辑记忆' })).toBeNull());
  fireEvent.click(screen.getByRole('button', { name: '遗忘' }));
  expect(request.mock.calls.some(([, o]) => o?.method === 'DELETE')).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: '确认遗忘' }));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/memory/m', { method: 'DELETE' }));
});

test('context panel shows honest meter mode, manual compaction and pins', async () => {
  request.mockImplementation(async (path, options) => {
    if (options?.method === 'POST') return { committed: true };
    if (options?.method === 'PUT') return {};
    return {
      revision: 2,
      meter: 'conservative',
      turns: 8,
      pins: [],
      limits: { window: 128000, trigger: 102400, target: 83200 },
      checkpoint: null,
      checkpoints: [],
      events: [{ id: 'e', preview: '不能改变接口' }],
    };
  });
  render(<ContextPanel conversationId="c" onClose={vi.fn()} onBusy={vi.fn()} />);
  await screen.findByText(/保守字节预算/);
  fireEvent.click(screen.getByRole('button', { name: '现在压缩' }));
  await screen.findByText('压缩检查点已保存，原始消息仍可查看。');
  fireEvent.click(screen.getByRole('checkbox', { name: '固定消息 不能改变接口' }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith('/conversations/c/context/pins', {
      method: 'PUT',
      body: JSON.stringify({ event_ids: ['e'] }),
    }),
  );
});

test('memory toggle error keeps the previous policy visible', async () => {
  request.mockImplementation(async (path, options) => {
    if (options?.method === 'PATCH') throw new Error('设置保存失败');
    if (path === '/knowledge/bases') return [];
    return { items: [], preferences: { enabled: true, auto_extract: true }, jobs: [] };
  });
  render(<Memory />);
  fireEvent.click(await screen.findByLabelText('使用个人记忆'));
  await screen.findByText('设置保存失败');
  expect((screen.getByLabelText('使用个人记忆') as HTMLInputElement).checked).toBe(true);
});
