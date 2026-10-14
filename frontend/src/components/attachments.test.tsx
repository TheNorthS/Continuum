// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { Space } from '../pages/Space';
import { Chat } from '../pages/Chat';
import { api, streamAnswer } from '../services/api';

vi.mock('../services/api', () => ({ api: vi.fn(), streamAnswer: vi.fn(), attachmentBlob: vi.fn() }));
const request = vi.mocked(api);
const stream = vi.mocked(streamAnswer);
const item = {
  id: 'a'.repeat(32),
  filename: '研究.txt',
  media_type: 'text/plain',
  size: 100,
  storage_size: 200,
  text_chars: 40,
  image_count: 0,
  created_at: 1,
};
const space = {
  items: [item],
  used_bytes: 200,
  quota_bytes: 100000,
  max_files: 100,
  max_upload_bytes: 5000000,
  max_per_message: 4,
  max_chars: 24000,
  max_images: 12,
  vision_configured: false,
};
const props = { onSelect: vi.fn(), onUpdated: vi.fn(), onBusy: vi.fn() };
beforeEach(() => {
  vi.resetAllMocks();
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(cleanup);

function conversationApi(saved: (typeof item)[] = [item]) {
  request.mockImplementation(async (path, options) => {
    if (path === '/knowledge/bases' || path.endsWith('/messages')) return [];
    if (path === '/attachments') return space;
    if (path.endsWith('/attachments') && options?.method === 'PATCH') {
      const ids = JSON.parse(options.body as string).attachment_ids as string[];
      saved = [item].filter((a) => ids.includes(a.id));
    }
    return {
      id: 'chat',
      use_knowledge: false,
      attachment_ids: saved.map((a) => a.id),
      attachments: saved,
      unavailable_attachment_ids: [],
    };
  });
}

test('saved attachment selection restores after unmount and reopen, including persisted removal', async () => {
  conversationApi();
  const first = render(<Chat selected="chat" {...props} />);
  await screen.findByRole('button', { name: '移除附件 研究.txt' });
  first.unmount();
  const second = render(<Chat selected="chat" {...props} />);
  const remove = await screen.findByRole('button', { name: '移除附件 研究.txt' });
  fireEvent.click(remove);
  await waitFor(() => expect(screen.queryByRole('button', { name: '移除附件 研究.txt' })).toBeNull());
  expect(request).toHaveBeenCalledWith(
    '/conversations/chat/attachments',
    expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ attachment_ids: [] }) }),
  );
  second.unmount();
  render(<Chat selected="chat" {...props} />);
  await screen.findByText('从一个问题，开始对话。');
  expect(screen.queryByRole('button', { name: '移除附件 研究.txt' })).toBeNull();
});

test('failed attachment save preserves selection and blocks sends until the request finishes', async () => {
  let rejectSave: (error: Error) => void = () => {};
  conversationApi();
  const base = request.getMockImplementation()!;
  request.mockImplementation((path, options) =>
    path.endsWith('/attachments') && options?.method === 'PATCH'
      ? new Promise((_, reject) => {
          rejectSave = reject;
        })
      : base(path, options),
  );
  render(<Chat selected="chat" {...props} />);
  const remove = await screen.findByRole('button', { name: '移除附件 研究.txt' });
  fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '追问' } });
  fireEvent.click(remove);
  expect((screen.getByRole('button', { name: '发送问题' }) as HTMLButtonElement).disabled).toBe(true);
  expect((remove as HTMLButtonElement).disabled).toBe(true);
  rejectSave(new Error('选择保存失败'));
  await screen.findByText('选择保存失败');
  expect(screen.getByRole('button', { name: '移除附件 研究.txt' })).toBeTruthy();
  expect((screen.getByRole('button', { name: '发送问题' }) as HTMLButtonElement).disabled).toBe(false);
});

test('new conversation persists uploaded selection before the first question', async () => {
  request.mockImplementation(async (path, options) => {
    if (path === '/knowledge/bases') return [];
    if (path === '/attachments' && options?.method === 'POST') return item;
    return { id: 'new-chat', use_knowledge: false, attachment_ids: [item.id], attachments: [item] };
  });
  render(<Chat selected="" {...props} />);
  await screen.findByText('从一个问题，开始对话。');
  fireEvent.change(screen.getByLabelText('上传聊天附件'), {
    target: { files: [new File(['data'], '研究.txt')] },
  });
  await screen.findByRole('button', { name: '移除附件 研究.txt' });
  expect(request).toHaveBeenCalledWith(
    '/conversations',
    expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ use_knowledge: false, attachment_ids: [item.id] }),
    }),
  );
  expect(props.onSelect).toHaveBeenCalledWith('new-chat');
  expect(stream).not.toHaveBeenCalled();
});

test('creating a conversation during upload finishes saving and keeps the question editable', async () => {
  request.mockImplementation(async (path, options) => {
    if (path === '/knowledge/bases' || path.endsWith('/messages')) return [];
    if (path === '/attachments' && options?.method === 'POST') return item;
    return { id: 'created', use_knowledge: false, attachment_ids: [item.id], attachments: [item] };
  });
  function Host() {
    const [selected, setSelected] = useState('');
    return <Chat {...props} selected={selected} onSelect={setSelected} />;
  }
  render(<Host />);
  await screen.findByText('从一个问题，开始对话。');
  fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '总结研究' } });
  fireEvent.change(screen.getByLabelText('上传聊天附件'), {
    target: { files: [new File(['data'], '研究.txt')] },
  });
  await screen.findByRole('button', { name: '移除附件 研究.txt' });
  await waitFor(() =>
    expect((screen.getByRole('button', { name: '发送问题' }) as HTMLButtonElement).disabled).toBe(false),
  );
  expect((screen.getByLabelText('输入问题') as HTMLTextAreaElement).value).toBe('总结研究');
  expect(props.onBusy).toHaveBeenLastCalledWith(false);
});

test('unavailable saved attachments block sending until the user removes them', async () => {
  let missing = ['deleted'];
  request.mockImplementation(async (path, options) => {
    if (path === '/knowledge/bases' || path.endsWith('/messages')) return [];
    if (options?.method === 'PATCH') missing = [];
    return {
      id: 'chat',
      use_knowledge: false,
      attachment_ids: [item.id, ...missing],
      attachments: [item],
      unavailable_attachment_ids: missing,
    };
  });
  render(<Chat selected="chat" {...props} />);
  await screen.findByText(/1 个附件已删除或不可访问/);
  fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '追问' } });
  expect((screen.getByRole('button', { name: '发送问题' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '移除失效附件' }));
  await waitFor(() => expect(screen.queryByText(/1 个附件已删除或不可访问/)).toBeNull());
  expect(request).toHaveBeenCalledWith(
    '/conversations/chat/attachments',
    expect.objectContaining({ body: JSON.stringify({ attachment_ids: [item.id] }) }),
  );
  expect((screen.getByRole('button', { name: '发送问题' }) as HTMLButtonElement).disabled).toBe(false);
});

test('late attachment save response cannot overwrite the next conversation', async () => {
  let finish: (value: unknown) => void = () => {};
  request.mockImplementation(async (path, options) => {
    if (path === '/knowledge/bases' || path.endsWith('/messages')) return [];
    if (options?.method === 'PATCH')
      return new Promise((resolve) => {
        finish = resolve;
      });
    if (path === '/conversations/second') return { id: 'second', use_knowledge: false, attachments: [] };
    return { id: 'first', use_knowledge: false, attachment_ids: [item.id], attachments: [item] };
  });
  const view = render(<Chat selected="first" {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: '移除附件 研究.txt' }));
  view.rerender(<Chat selected="second" {...props} />);
  await screen.findByText('从一个问题，开始对话。');
  finish({ id: 'first', attachments: [item] });
  await waitFor(() => expect(screen.queryByRole('button', { name: '移除附件 研究.txt' })).toBeNull());
});

test('space lists, searches, previews, renames and explicitly deletes private attachments', async () => {
  let items = [item];
  request.mockImplementation(async (path, options) => {
    if (options?.method === 'PATCH') {
      items = [{ ...item, filename: JSON.parse(options.body as string).filename }];
      return items[0];
    }
    if (options?.method === 'DELETE') {
      items = [];
      return;
    }
    if (path === '/attachments') return { ...space, items };
    return { ...item, text: '可预览的文本' };
  });
  render(<Space />);
  fireEvent.click(await screen.findByText('研究.txt'));
  await screen.findByText('可预览的文本');
  fireEvent.click(screen.getByRole('button', { name: '关闭预览' }));
  fireEvent.change(screen.getByLabelText('搜索附件'), { target: { value: '不存在' } });
  expect(screen.queryByText('研究.txt')).toBeNull();
  fireEvent.change(screen.getByLabelText('搜索附件'), { target: { value: '' } });
  fireEvent.click(screen.getByRole('button', { name: '重命名 研究.txt' }));
  fireEvent.change(screen.getByLabelText('附件名称'), { target: { value: '新版.txt' } });
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  await screen.findByText('新版.txt');
  fireEvent.click(screen.getByRole('button', { name: '删除 新版.txt' }));
  expect(request.mock.calls.some(([, o]) => o?.method === 'DELETE')).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
  await screen.findByText('暂无附件');
});

test('chat can disable knowledge, choose stored attachments and send their IDs', async () => {
  request.mockImplementation(async (path) => {
    if (path === '/knowledge/bases') return [{ id: 'kb', name: '研发', access: 'read' }];
    if (path === '/attachments') return space;
    if (path.endsWith('/messages')) return [];
    return { id: 'chat', base_ids: ['kb'], use_knowledge: true };
  });
  stream.mockResolvedValue({
    answer: '附件回答',
    sources: [],
    trace: [],
    status: 'answered',
    mode: 'adaptive',
    runtime: 'live',
    conversation_id: 'chat',
  });
  render(<Chat selected="chat" {...props} />);
  await screen.findByText('从你的资料，找到答案。');
  fireEvent.click(screen.getByLabelText('使用知识库'));
  await screen.findByText('从一个问题，开始对话。');
  expect(request).toHaveBeenCalledWith(
    '/conversations/chat/scope',
    expect.objectContaining({ body: JSON.stringify({ use_knowledge: false }) }),
  );
  fireEvent.click(screen.getByRole('button', { name: '从空间选择附件' }));
  fireEvent.click(await screen.findByRole('checkbox', { name: '选择附件 研究.txt' }));
  fireEvent.click(await screen.findByRole('button', { name: '使用已选附件（1）' }));
  fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '总结附件' } });
  fireEvent.click(screen.getByRole('button', { name: '发送问题' }));
  await screen.findByText('附件回答');
  expect(stream.mock.calls[0][5]).toBeUndefined();
  expect(stream.mock.calls[0][6]).toEqual({ use_knowledge: false, attachment_ids: [item.id] });
});

test('chat upload saves to space and removing a chip does not delete the file', async () => {
  request.mockImplementation(async (path, options) => {
    if (path === '/attachments' && options?.method === 'POST') return item;
    if (path === '/knowledge/bases' || path.endsWith('/messages')) return [];
    return { id: 'chat', use_knowledge: false };
  });
  render(<Chat selected="chat" {...props} />);
  await screen.findByText('从一个问题，开始对话。');
  fireEvent.change(screen.getByLabelText('上传聊天附件'), {
    target: { files: [new File(['正文'], '研究.txt', { type: 'text/plain' })] },
  });
  const remove = await screen.findByRole('button', { name: '移除附件 研究.txt' });
  await waitFor(() => expect((remove as HTMLButtonElement).disabled).toBe(false));
  const upload = request.mock.calls.find(([p, o]) => p === '/attachments' && o?.method === 'POST');
  expect(upload?.[1]?.body).toBeInstanceOf(FormData);
  fireEvent.click(remove);
  await waitFor(() => expect(screen.queryByRole('button', { name: '移除附件 研究.txt' })).toBeNull());
  expect(request.mock.calls.some(([, o]) => o?.method === 'DELETE')).toBe(false);
});

test('failed knowledge toggle retains the original mode', async () => {
  request.mockImplementation(async (path) => {
    if (path.endsWith('/scope')) throw new Error('保存失败');
    if (path === '/knowledge/bases') return [{ id: 'kb', name: '研发', access: 'read' }];
    if (path.endsWith('/messages')) return [];
    return { id: 'chat', base_ids: ['kb'], use_knowledge: true };
  });
  render(<Chat selected="chat" {...props} />);
  await screen.findByText('从你的资料，找到答案。');
  fireEvent.click(screen.getByLabelText('使用知识库'));
  await screen.findByText('保存失败');
  expect((screen.getByLabelText('使用知识库') as HTMLInputElement).checked).toBe(true);
});
