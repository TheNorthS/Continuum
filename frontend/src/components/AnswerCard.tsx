import { BookOpen, ChevronDown } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Message, Source } from '../types';

function safeLink(url?: string) {
  return url && /^https?:\/\//i.test(url) ? url : undefined;
}

export function Sources({ sources }: { sources: Source[] }) {
  return (
    <div className="sources">
      {sources.some((source) => source.metadata.rerank_status === 'fallback') && (
        <p className="notice" role="status">
          本次模型精排不可用，已按配置回退为 RRF 融合排序。
        </p>
      )}
      {sources.some((source) => source.metadata.web_status === 'partial') && (
        <p className="notice" role="status">
          部分网络搜索服务暂不可用，本次回答仅基于成功返回并通过审查的来源。
        </p>
      )}
      {sources.map((source, i) => (
        <details key={source.id} className="source">
          <summary>
            <span className="citation">{i + 1}</span>
            <BookOpen size={14} />
            <span>{source.metadata.title || source.metadata.filename || '参考资料'}</span>
            <ChevronDown size={14} />
          </summary>
          <p>{source.text}</p>
          {!!source.metadata.web_providers?.length && (
            <small>
              网络来源 ·{' '}
              {source.metadata.web_providers
                .map((provider) => ({ tavily: 'Tavily', exa: 'Exa' })[provider] || provider)
                .join(' + ')}
            </small>
          )}
          {source.metadata.rerank_status === 'ranked' && typeof source.rerank_score === 'number' && (
            <small>
              模型精排分数：{source.rerank_score.toFixed(4)} · {source.metadata.rerank_model}
              （相关性分数，不代表答案置信度）
            </small>
          )}
          {source.metadata.rerank_status === 'fallback' && (
            <small className="rerank-warning">本次精排服务不可用，已按配置回退为 RRF 融合排序。</small>
          )}
          {safeLink(source.metadata.source) ? (
            <a href={safeLink(source.metadata.source)} target="_blank" rel="noopener noreferrer">
              查看原始来源 ↗
            </a>
          ) : (
            <small>{source.metadata.filename}</small>
          )}
        </details>
      ))}
    </div>
  );
}

export function AnswerCard({ message }: { message: Message }) {
  const assistant = message.role === 'assistant';
  return (
    <article className={`message ${assistant ? 'assistant' : 'human'}`}>
      <div className="message-label">
        {assistant ? (
          <>
            <span className="mini-logo">E</span> RAG-E <span className="subtle">知识助手</span>
          </>
        ) : (
          '你'
        )}
      </div>
      {assistant && message.details?.knowledge_scope?.base_ids && (
        <div className="answer-scope">
          <BookOpen size={13} />
          <span>本次范围：{message.details.knowledge_scope.base_names.join(' · ')}</span>
        </div>
      )}
      {message.details?.status === 'blocked' && (
        <p className="notice" role="status">
          内容安全审查已拦截本次问答。本轮问题与候选答案未保存到聊天历史。
        </p>
      )}
      <div className="markdown">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            img: () => null,
            a: ({ children, href }) => (
              <a href={safeLink(href)} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            ),
          }}
        >
          {message.content}
        </ReactMarkdown>
      </div>
      {message.details?.sources?.length ? (
        <>
          <div className="source-label">参考来源 · {message.details.sources.length}</div>
          <Sources sources={message.details.sources} />
        </>
      ) : null}
      {message.details?.trace?.length ? (
        <details className="trace">
          <summary>查看执行过程 · {message.details.trace.length} 步</summary>
          <ol>
            {message.details.trace.map((s, i) => (
              <li key={i}>
                {s.label} <span>{s.sources} 条资料</span>
              </li>
            ))}
          </ol>
        </details>
      ) : null}
    </article>
  );
}
