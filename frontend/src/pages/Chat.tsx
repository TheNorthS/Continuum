import { useEffect, useRef, useState } from 'react';
import { ArrowUp, BookOpen, CornerDownLeft, Sparkles, Square, Paperclip, FolderOpen, X } from 'lucide-react';
import { AnswerCard } from '../components/AnswerCard';
import { KnowledgeScopePicker } from '../components/KnowledgeScopePicker';
import type { KnowledgeBase } from '../components/KnowledgeAccess';
import { api, streamAnswer } from '../services/api';
import type { Attachment, Conversation, Message, Step } from '../types';
import { Space, attachmentAccept, uploadAttachment } from './Space';
import { useSession } from '../stores/session';

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
  const [useKnowledge, setUseKnowledge] = useState(true);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [unavailableAttachments, setUnavailableAttachments] = useState<string[]>([]);
  const [savingAttachments, setSavingAttachments] = useState(false);
  const attachmentWrite = useRef(false);
  const viewVersion = useRef(0);
  const [showSpace, setShowSpace] = useState(false);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const [notice, setNotice] = useState('');
  const [steps, setSteps] = useState<Step[]>([]);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const abort = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const created = useRef('');
  const canReadKnowledge = useSession((s) => s.user?.permissions?.includes('knowledge.read') ?? true);
  useEffect(
    () => () => {
      viewVersion.current++;
      abort.current?.abort();
    },
    [],
  );
  useEffect(() => {
    if (created.current === selected && selected) {
      created.current = '';
      return;
    }
    const controller = new AbortController();
    const version = ++viewVersion.current;
    setError('');
    setNotice('');
    setSteps([]);
    setQuestion('');
    setMessages([]);
    setLoading(true);
    setScopeReady(false);
    setBaseIds([]);
    setAttachments([]);
    setUnavailableAttachments([]);
    setSavingAttachments(false);
    setUploading(false);
    attachmentWrite.current = false;
    setShowSpace(false);
    const options = { signal: controller.signal };
    Promise.all([
      canReadKnowledge
        ? api<KnowledgeBase[]>('/knowledge/bases', options)
        : Promise.resolve([] as KnowledgeBase[]),
      selected ? api<Conversation>(`/conversations/${selected}`, options) : Promise.resolve(null),
      selected ? api<Message[]>(`/conversations/${selected}/messages`, options) : Promise.resolve([]),
    ])
      .then(([available, conversation, history]) => {
        if (controller.signal.aborted) return;
        setBases(available);
        setMessages(history);
        setAttachments(conversation?.attachments ?? []);
        setUnavailableAttachments(conversation?.unavailable_attachment_ids ?? []);
        if (conversation?.attachment_ids?.length) setNotice('已恢复此会话的附件选择。');
        setUseKnowledge(conversation?.use_knowledge ?? !!available.length);
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
    return () => {
      controller.abort();
      // 草稿创建成正式会话仍属于同一视图，保留正在收尾的上传/保存任务。
      if (viewVersion.current === version && !created.current) viewVersion.current++;
    };
  }, [selected, initialBaseIds, reload, canReadKnowledge]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages, steps]);
  const unavailable = baseIds.some((id) => !bases.some((b) => b.id === id));
  const canSend =
    !!question.trim() &&
    scopeReady &&
    (!useKnowledge || (!!baseIds.length && !unavailable)) &&
    !loading &&
    !savingScope &&
    !savingAttachments &&
    !unavailableAttachments.length &&
    !busy &&
    !uploading;

  async function toggleKnowledge(enabled: boolean) {
    if (enabled && !baseIds.length) {
      setError('请先创建或选择一个知识库。');
      return;
    }
    setSavingScope(true);
    onBusy(true);
    setError('');
    try {
      if (selected)
        await api(`/conversations/${selected}/scope`, {
          method: 'PATCH',
          body: JSON.stringify(
            enabled ? { use_knowledge: true, base_ids: baseIds } : { use_knowledge: false },
          ),
        });
      setUseKnowledge(enabled);
      setNotice('作答范围已更新，后续提问使用新上下文。');
      onUpdated();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSavingScope(false);
      onBusy(false);
    }
  }
  async function saveAttachments(
    next: Attachment[],
    missing: string[] = unavailableAttachments,
    fromUpload = false,
  ) {
    // 同一页面仅允许一个选择写请求，避免快速点击导致后返回的旧响应覆盖新状态。
    if (attachmentWrite.current || busy || loading || savingScope) return false;
    attachmentWrite.current = true;
    const version = viewVersion.current;
    setSavingAttachments(true);
    onBusy(true);
    setError('');
    try {
      const ids = [...next.map((a) => a.id), ...missing];
      const conversation = selected
        ? await api<Conversation>(`/conversations/${selected}/attachments`, {
            method: 'PATCH',
            body: JSON.stringify({ attachment_ids: ids }),
          })
        : await api<Conversation>('/conversations', {
            method: 'POST',
            body: JSON.stringify({
              ...(useKnowledge ? { base_ids: baseIds } : { use_knowledge: false }),
              attachment_ids: ids,
            }),
          });
      if (version !== viewVersion.current) return false;
      setAttachments(conversation.attachments ?? next);
      setUnavailableAttachments(conversation.unavailable_attachment_ids ?? []);
      setNotice('附件选择已保存到会话，刷新或重新打开会自动恢复。');
      if (!selected) {
        created.current = conversation.id;
        onSelect(conversation.id);
      }
      onUpdated();
      return true;
    } catch (e) {
      if (version === viewVersion.current) setError((e as Error).message);
      return false;
    } finally {
      if (version === viewVersion.current) {
        attachmentWrite.current = false;
        setSavingAttachments(false);
        if (!fromUpload) onBusy(false);
      }
    }
  }
  function toggleAttachment(item: Attachment) {
    void saveAttachments(
      attachments.some((a) => a.id === item.id)
        ? attachments.filter((a) => a.id !== item.id)
        : [...attachments, item],
    );
  }
  async function upload(files: FileList | null) {
    if (!files?.length) return;
    setUploading(true);
    onBusy(true);
    setError('');
    const version = viewVersion.current;
    try {
      const next = [...attachments];
      for (const file of Array.from(files)) {
        const item = await uploadAttachment(file);
        if (version !== viewVersion.current) return;
        next.push(item);
      }
      // 上传只保存文件；选择保存失败时保持原选择，已上传文件仍可从空间重新选择。
      await saveAttachments(next, unavailableAttachments, true);
    } catch (e) {
      if (version === viewVersion.current) setError((e as Error).message + ' 已上传成功的文件保留在空间中。');
    } finally {
      if (version === viewVersion.current) {
        setUploading(false);
        onBusy(false);
        if (fileInput.current) fileInput.current.value = '';
      }
    }
  }

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
          body: JSON.stringify({
            ...(useKnowledge ? { base_ids: baseIds } : { use_knowledge: false }),
            ...(attachments.length ? { attachment_ids: attachments.map((a) => a.id) } : {}),
          }),
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
        useKnowledge ? baseIds : undefined,
        { use_knowledge: useKnowledge, attachment_ids: attachments.map((a) => a.id) },
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
        <label className="knowledge-toggle">
          <input
            type="checkbox"
            checked={useKnowledge}
            disabled={busy || uploading || savingAttachments || loading || savingScope || !scopeReady}
            onChange={(e) => void toggleKnowledge(e.target.checked)}
          />
          使用知识库
        </label>
        {useKnowledge && (
          <KnowledgeScopePicker
            key={selected || 'new'}
            bases={bases}
            selected={baseIds}
            disabled={busy || uploading || savingAttachments || loading || savingScope || !scopeReady}
            onApply={applyScope}
            onManage={onManage}
          />
        )}
        <span className="scope-assurance">
          <span />
          {useKnowledge ? '所选知识库 + 本轮附件' : '仅本轮附件或普通对话'} · 不使用网络资料
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
        ) : !bases.length && useKnowledge ? (
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
            <h1>{useKnowledge ? '从你的资料，找到答案。' : '从一个问题，开始对话。'}</h1>
            <p>
              {useKnowledge ? '选择知识库，可添加附件一起提问。' : '可以直接提问，或上传图片和文档进行分析。'}
              <br />
              每一次回答，都可以回到它的来源。
            </p>
            <div className="welcome-scope">
              <BookOpen size={15} />
              <span>
                {!useKnowledge
                  ? '知识库已关闭 · 附件不进行向量化'
                  : unavailable
                    ? '请重新选择可访问的知识库'
                    : `当前使用 ${baseIds.length} 个知识库`}
              </span>
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
        {useKnowledge && unavailable && (
          <p role="alert" className="error">
            部分已选知识库已不可访问，请在上方重新选择。系统不会自动扩大检索范围。
          </p>
        )}
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        {!!unavailableAttachments.length && (
          <p role="alert" className="error">
            此会话有 {unavailableAttachments.length} 个附件已删除或不可访问，请移除失效选择后再提问。
            <button
              disabled={busy || uploading || savingAttachments}
              onClick={() => void saveAttachments(attachments, [])}
            >
              移除失效附件
            </button>
          </p>
        )}
        <div ref={bottom} />
      </div>
      <div className="composer-wrap">
        <input
          ref={fileInput}
          type="file"
          aria-label="上传聊天附件"
          accept={attachmentAccept}
          multiple
          hidden
          onChange={(e) => void upload(e.target.files)}
        />
        {!!attachments.length && (
          <div className="attachment-chips">
            {attachments.map((item) => (
              <span key={item.id}>
                <Paperclip size={14} />
                {item.filename}
                <button
                  aria-label={`移除附件 ${item.filename}`}
                  disabled={busy || uploading || savingAttachments}
                  onClick={() => toggleAttachment(item)}
                >
                  <X size={14} />
                </button>
              </span>
            ))}
          </div>
        )}
        <form className="composer" onSubmit={send}>
          <textarea
            ref={input}
            aria-label="输入问题"
            placeholder={
              !useKnowledge
                ? '提出问题，或添加附件后提问…'
                : baseIds.length
                  ? '向所选知识库和附件提问…'
                  : '请先选择知识库，或关闭知识库开关…'
            }
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
            <button
              type="button"
              aria-label="上传附件"
              disabled={busy || uploading || savingAttachments || savingScope || loading || !scopeReady}
              onClick={() => fileInput.current?.click()}
            >
              <Paperclip size={18} />
            </button>
            <button
              type="button"
              aria-label="从空间选择附件"
              disabled={busy || uploading || savingAttachments || savingScope || loading || !scopeReady}
              onClick={() => setShowSpace(true)}
            >
              <FolderOpen size={18} />
            </button>
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
        <p className="composer-note">
          {savingAttachments
            ? '正在保存会话附件选择…'
            : uploading
              ? '正在解析附件并保存到个人空间…'
              : attachments.length
                ? `会话已保存 ${attachments.length} 个附件；追问时继续使用，移除后不再自动带入。`
                : '可添加图片、PDF、DOCX、TXT / Markdown。请结合原文核实答案。'}
        </p>
      </div>
      {showSpace && (
        <div className="modal-backdrop">
          <section className="modal space-modal" role="dialog" aria-modal="true" aria-label="选择空间附件">
            <button
              className="space-close"
              aria-label="关闭空间选择"
              disabled={savingAttachments}
              onClick={() => setShowSpace(false)}
            >
              <X size={18} />
            </button>
            <Space
              selection={attachments.map((a) => a.id)}
              onToggle={toggleAttachment}
              selectionDisabled={savingAttachments}
            />
            <button className="primary" disabled={savingAttachments} onClick={() => setShowSpace(false)}>
              使用已选附件（{attachments.length}）
            </button>
          </section>
        </div>
      )}
    </div>
  );
}
