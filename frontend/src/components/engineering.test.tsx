// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { KnowledgeGraph } from '../pages/KnowledgeGraph';
import { Observability } from '../pages/Observability';
import { System } from '../pages/System';
import { Sources } from './AnswerCard';
import { useSession } from '../stores/session';
import { api } from '../services/api';

vi.mock('../services/api', () => ({ api: vi.fn() }));
const mocked = vi.mocked(api);
const data = {
  nodes: [
    { id: 'a', name: '研究团队', kind: '组织' },
    { id: 'b', name: '蛋白质A', kind: '概念' },
  ],
  links: [
    {
      id: 'r',
      source: 'a',
      target: 'b',
      predicate: '发现',
      evidence: '研究团队发现蛋白质A',
      filename: 'research.md',
      extraction: 'live',
    },
  ],
};
beforeEach(() => {
  vi.resetAllMocks();
  useSession.getState().login('token', { id: 'admin', username: 'admin', role: 'admin' });
});
afterEach(() => {
  cleanup();
  useSession.getState().logout();
});

test('force graph displays entities, evidence and relationship paths', async () => {
  mocked.mockImplementation(async (path) => {
    if (path === '/graph/status')
      return {
        enabled: true,
        mode: 'live',
        documents: [{ id: 'doc', filename: 'research.md', status: 'ready', graph_status: 'ready' }],
      };
    if (path.startsWith('/graph/network')) return data;
    if (path === '/graph/paths') return { paths: [data], inferences: [] };
    return {};
  });
  render(<KnowledgeGraph />);
  await screen.findByRole('img', { name: '知识图谱力导向图' });
  fireEvent.click(await screen.findByRole('button', { name: '实体 研究团队' }));
  expect(screen.getByRole('region', { name: '选中实体的关系证据' }).textContent).toContain(
    '研究团队发现蛋白质A',
  );
  expect((screen.getByLabelText('路径起点') as HTMLSelectElement).value).toBe('a');
  fireEvent.change(screen.getByLabelText('路径终点'), { target: { value: 'b' } });
  fireEvent.click(screen.getByRole('button', { name: '探索三跳关系' }));
  await screen.findByText('研究团队 ↔ 蛋白质A');
  expect(screen.getAllByText(/研究团队发现蛋白质A/).length).toBeGreaterThan(0);
  fireEvent.click(screen.getByRole('button', { name: '重建图谱' }));
  await waitFor(() =>
    expect(mocked).toHaveBeenCalledWith('/graph/documents/doc/rebuild', { method: 'POST' }),
  );
});

test('observability renders real snapshot percentiles, firing and parent spans', async () => {
  mocked.mockResolvedValue({
    window_seconds: 300,
    profiler: { llm: { count: 10, errors: 2, p50_ms: 100, p95_ms: 450, p99_ms: 700 } },
    alerts: [{ name: 'llm_error_rate', state: 'firing', value: 0.2, threshold: 0.2 }],
    history: [],
    cache: { l1_size: 4, l1_capacity: 256, l2_enabled: true, events: { l1_hit: 9 } },
    pool: { connections: 32, max_concurrency: 8, batch_size: 32 },
    tracing: { enabled: true, capture_content: false, instrumentation_errors: 0 },
    dependencies: { neo4j: true },
    spans: [
      {
        trace_id: 'request123456789',
        span_id: 'generation',
        parent_id: 'parent',
        name: 'llm',
        status: 'ok',
        duration_ms: 450,
        at: 1,
      },
    ],
  });
  render(<Observability />);
  expect((await screen.findAllByText('450.0 ms')).length).toBe(2);
  expect(screen.getByText('firing')).toBeTruthy();
  expect(screen.getByText(/generation ← parent/)).toBeTruthy();
  expect(screen.getByText('追踪已启用')).toBeTruthy();
  expect(screen.getByText(/l1_hit: 9/)).toBeTruthy();
});

test('system shows actual reranker configuration and waiting state', async () => {
  mocked.mockImplementation(async (path) => {
    if (path === '/system')
      return {
        mode: 'demo',
        reranker: {
          enabled: true,
          provider: 'huggingface',
          model: 'BAAI/bge-reranker-base',
          state: 'not_loaded',
          candidates: 32,
          min_score: null,
          failure_mode: 'error',
        },
      };
    if (path === '/health/ready') return { checks: { database: 'ok' } };
    if (path === '/agents/workflow') return { mermaid: 'graph LR' };
    return {};
  });
  render(<System />);
  await screen.findByText(/本地 Cross-Encoder/);
  expect(screen.getByText(/等待首次加载/)).toBeTruthy();
  expect(screen.getByText('混合检索 + 模型精排')).toBeTruthy();
});

test('citations display rerank score distinctly and fallback is visible', () => {
  render(
    <Sources
      sources={[
        {
          id: 'a',
          text: 'source text',
          score: 0.01,
          rerank_score: 0.82,
          metadata: { filename: 'a.md', rerank_status: 'ranked', rerank_model: 'reranker' },
        },
        {
          id: 'b',
          text: 'other text',
          score: 0.01,
          metadata: { filename: 'b.md', rerank_status: 'fallback' },
        },
      ]}
    />,
  );
  expect(screen.getByText(/模型精排分数：0.8200/).textContent).toContain('不代表答案置信度');
  expect(screen.getByRole('status').textContent).toContain('回退为 RRF');
});
