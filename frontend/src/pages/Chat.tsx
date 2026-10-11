import { useEffect, useRef, useState } from 'react';
import { ArrowUp, BookOpen, CornerDownLeft, Sparkles, Square } from 'lucide-react';
import { AnswerCard } from '../components/AnswerCard';
import { KnowledgeScopePicker } from '../components/KnowledgeScopePicker';
import type { KnowledgeBase } from '../components/KnowledgeAccess';
import { api, streamAnswer } from '../services/api';
import type { Conversation, Message, Step } from '../types';

export function Chat({
  selected,
  onSelect,
  onUpdated,
  onBusy,
  initialBaseIds,
  onManage,
}: {
  selected: string;
  onSelect: (id: string) => void;
  onUpdated: () => void;
  onBusy: (busy: boolean) => void;
  initialBaseIds?: string[];
  onManage?: () => void;
}) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [question, setQuestion] = useState('');
  const [mode, setMode] = useState('adaptive');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [savingScope, setSavingScope] = useState(false);
  const [scopeReady, setScopeReady] = useState(false);
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [baseIds, setBaseIds] = useState<string[]>([]);
  const [notice, setNotice] = useState('');
  const [steps, setSteps] = useState<Step[]>([]);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const abort = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const created = useRef('');
  useEffect(() => () => abort.current?.abort(), []);
  useEffect(() => {
    if (created.current === selected && selected) {
      created.current = '';
      return;
    }
    const controller = new AbortController();
    setError('');
    setNotice('');
    setSteps([]);
    setQuestion('');
    setMessages([]);
    setLoading(true);
    setScopeReady(false);
    setBaseIds([]);
    const options = { signal: controller.signal };
    Promise.all([
      api<KnowledgeBase[]>('/knowledge/bases', options),
      selected ? api<Conversation>(`/conversations/${selected}`, options) : Promise.resolve(null),
      selected ? api<Message[]>(`/conversations/${selected}/messages`, options) : Promise.resolve([]),
    ])
      .then(([available, conversation, history]) => {
        if (controller.signal.aborted) return;
        setBases(available);
        setMessages(history);
        // Legacy conversations become a fixed snapshot on their next turn, never an implicit widening.
        setBaseIds(
          conversation
            ? (conversation.base_ids ?? available.map((b) => b.id))
            : (initialBaseIds ?? (available[0] ? [available[0].id] : [])),
        );
        setScopeReady(true);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [selected, initialBaseIds, reload]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages, steps]);
  const unavailable = baseIds.some((id) => !bases.some((b) => b.id === id));
  const canSend =
    !!question.trim() && scopeReady && !!baseIds.length && !unavailable && !loading && !savingScope && !busy;

  async function applyScope(ids: string[]) {
    setSavingScope(true);
    onBusy(true);
    try {
      if (selected)
        await api(`/conversations/${selected}/scope`, {
          method: 'PATCH',
          body: JSON.stringify({ base_ids: ids }),
        });
      const changed = [...baseIds].sort().join(',') !== [...ids].sort().join(',');
      setBaseIds(ids);
      setError('');
      if (changed)
        setNotice(
          messages.length
            ? '问答范围已更新。接下来的提问将使用新上下文，之前的记录仍保留。'
            : `已选择 ${ids.length} 个知识库，可以开始提问。`,
        );
      onUpdated();
    } finally {
      setSavingScope(false);
      onBusy(false);
    }
  }
  async function send(event?: React.FormEvent) {
    event?.preventDefault();
    if (!canSend) return;
    const text = question.trim();
    setQuestion('');
    setBusy(true);
    onBusy(true);
    setSteps([]);
    setError('');
    setNotice('');
    const controller = new AbortController();
    abort.current = controller;
    let id = selected;
    try {
      if (!id) {
        const conversation = await api<Conversation>('/conversations', {
          method: 'POST',
          body: JSON.stringify({ base_ids: baseIds }),
          signal: controller.signal,
        });
        id = conversation.id;
        created.current = id;
        onSelect(id);
        onUpdated();
      }
      setMessages((previous) => [...previous, { id: Date.now(), role: 'user', content: text }]);
      const answer = await streamAnswer(
        id,
        text,
        mode,
        (step) => setSteps((previous) => [...previous, step]),
        controller.signal,
        baseIds,
      );
      setMessages((previous) => [
        ...previous,
        { id: Date.now(), role: 'assistant', content: answer.answer, details: answer },
      ]);
      onUpdated();
    } catch (e) {
      setError((e as Error).name === 'AbortError' ? '已停止生成，问题已放回输入框。' : (e as Error).message);
      setQuestion(text);
      if (id) {
        const history = await api<Message[]>(`/conversations/${id}/messages`).catch(() => null);
        if (history) setMessages(history);
      }
    } finally {
      setBusy(false);
      onBusy(false);
      setSteps([]);
      abort.current = null;
      input.current?.focus();
    }
  }
  return (
    <div className="chat-page">
      <div className="chat-scope-bar">
        <KnowledgeScopePicker
          key={selected || 'new'}
          bases={bases}
          selected={baseIds}
          disabled={busy || loading || savingScope || !scopeReady}
          onApply={applyScope}
          onManage={onManage}
        />
        <span className="scope-assurance">
          <span />
          仅所选知识库 · 不使用网络资料
        </span>
      </div>
      <div className="chat-scroll">
        {loading ? (
          <div className="chat-loading" role="status">
            <span className="pulse" />
            正在读取会话与知识库…
          </div>
        ) : !scopeReady ? (
          <div className="empty">
            <BookOpen size={32} />
            <h2>暂时无法读取工作区</h2>
            <button onClick={() => setReload((v) => v + 1)}>重新加载</button>
          </div>
        ) : !bases.length ? (
          <div className="welcome">
            <div className="welcome-symbol">
              <BookOpen size={30} />
            </div>
            <h1>先准备你的知识库</h1>
            <p>创建知识库并上传资料，或请管理员为你授权。</p>
            {onManage && (
              <button className="primary" onClick={onManage}>
                前往知识库
              </button>
            )}
          </div>
        ) : !messages.length ? (
          <div className="welcome">
            <div className="welcome-symbol">
              <Sparkles size={30} />
            </div>
            <div className="eyebrow">YOUR KNOWLEDGE, CONNECTED</div>
            <h1>从你的资料，找到答案。</h1>
            <p>
              选择一个或多个知识库，再提出你的问题。
              <br />
              每一次回答，都可以回到它的来源。
            </p>
            <div className="welcome-scope">
              <BookOpen size={15} />
              <span>{unavailable ? '请重新选择可访问的知识库' : `当前使用 ${baseIds.length} 个知识库`}</span>
              <small>可在上方调整范围</small>
            </div>
            <div className="suggestions">
              {[
                '请解释资料中的一个核心概念，并给出出处。',
                '这些资料中的核心概念之间有哪些关系？',
                '请对比文档中的不同方案，并给出来源。',
              ].map((q, i) => (
                <button
                  key={q}
                  onClick={() => {
                    setQuestion(q);
                    input.current?.focus();
                  }}
                >
                  <span>0{i + 1}</span>
                  <BookOpen size={17} />
                  {q}
                  <CornerDownLeft size={15} />
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((message) => <AnswerCard key={message.id} message={message} />)
        )}
        {busy && (
          <div className="working" role="status">
            <span className="pulse" />
            {steps.at(-1)?.label || '正在检索所选知识库…'}
            <small>{steps.length > 0 ? `已完成 ${steps.length} 个步骤` : ''}</small>
          </div>
        )}
        {notice && (
          <p role="status" className="notice">
            {notice}
          </p>
        )}
        {unavailable && (
          <p role="alert" className="error">
            部分已选知识库已不可访问，请在上方重新选择。系统不会自动扩大检索范围。
          </p>
        )}
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <div ref={bottom} />
      </div>
      <div className="composer-wrap">
        <form className="composer" onSubmit={send}>
          <textarea
            ref={input}
            aria-label="输入问题"
            placeholder={baseIds.length ? '向所选知识库提问…' : '请先选择一个或多个知识库…'}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            maxLength={4000}
            rows={2}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void send();
              }
            }}
          />
          <div className="composer-actions">
            <select
              aria-label="问答模式"
              value={mode}
              onChange={(e) => setMode(e.target.value)}
              disabled={busy}
            >
              <option value="adaptive">自适应检索</option>
              <option value="agentic">Agent 检索</option>
            </select>
            <span>Enter 发送 · Shift + Enter 换行</span>
            {busy ? (
              <button
                type="button"
                aria-label="停止生成"
                className="send"
                onClick={() => abort.current?.abort()}
              >
                <Square size={17} />
              </button>
            ) : (
              <button aria-label="发送问题" className="send" disabled={!canSend}>
                <ArrowUp size={21} />
              </button>
            )}
          </div>
        </form>
        <p className="composer-note">资料不足时会明确提示。请结合引用原文核实答案。</p>
      </div>
    </div>
  );
}
