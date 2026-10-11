import { useEffect, useRef, useState } from 'react';
import { BookOpen, Check, ChevronDown, Search, X } from 'lucide-react';
import type { KnowledgeBase } from './KnowledgeAccess';

export function KnowledgeScopePicker({
  bases,
  selected,
  disabled,
  onApply,
  onManage,
}: {
  bases: KnowledgeBase[];
  selected: string[];
  disabled: boolean;
  onApply: (ids: string[]) => Promise<void>;
  onManage?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<string[]>([]);
  const [query, setQuery] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const names = selected.map((id) => bases.find((b) => b.id === id)?.name || '不可访问的知识库');
  useEffect(() => {
    if (!open) return;
    search.current?.focus();
    function outside(e: PointerEvent) {
      if (!saving && !root.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open, saving]);
  const close = () => {
    setOpen(false);
    trigger.current?.focus();
  };
  async function apply() {
    setSaving(true);
    setError('');
    try {
      await onApply(draft);
      close();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }
  return (
    <div
      className="scope-picker"
      ref={root}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && !saving) {
          e.preventDefault();
          close();
        }
      }}
    >
      <button
        ref={trigger}
        type="button"
        className="scope-trigger"
        aria-expanded={open}
        aria-controls="scope-options"
        disabled={disabled}
        onClick={() => {
          if (open) {
            close();
            return;
          }
          setDraft(selected.filter((id) => bases.some((b) => b.id === id)));
          setQuery('');
          setError('');
          setOpen(true);
        }}
      >
        <BookOpen size={18} />
        <span className="scope-trigger-text">
          <small>问答范围</small>
          <strong title={names.join('、')}>
            {names.length
              ? names.length === 1
                ? names[0]
                : `${names[0]} + ${names.length - 1} 个知识库`
              : '选择知识库'}
          </strong>
        </span>
        <ChevronDown size={16} />
      </button>
      {open && (
        <section id="scope-options" className="scope-popover" aria-label="选择问答知识库">
          <div className="scope-popover-heading">
            <div>
              <strong>选择问答知识库</strong>
              <p>可多选，回答仅依据所选库中的资料。</p>
            </div>
            <button
              type="button"
              className="icon-button"
              aria-label="关闭知识库选择"
              onClick={close}
              disabled={saving}
            >
              <X size={17} />
            </button>
          </div>
          <label className="scope-search">
            <Search size={16} />
            <input
              ref={search}
              aria-label="搜索知识库"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索知识库名称"
            />
          </label>
          <div className="scope-selection-actions">
            <span>
              已选 {draft.length} / {bases.length}
            </span>
            <button
              type="button"
              disabled={saving || !bases.length}
              onClick={() => setDraft(bases.map((b) => b.id))}
            >
              全选
            </button>
            <button type="button" disabled={saving || !draft.length} onClick={() => setDraft([])}>
              清空
            </button>
          </div>
          <div className="scope-options" role="group" aria-label="可用知识库">
            {bases
              .filter((b) => b.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
              .map((b) => (
                <label key={b.id} className={`scope-option ${draft.includes(b.id) ? 'checked' : ''}`}>
                  <input
                    type="checkbox"
                    checked={draft.includes(b.id)}
                    disabled={saving}
                    onChange={() =>
                      setDraft((ids) =>
                        ids.includes(b.id) ? ids.filter((id) => id !== b.id) : [...ids, b.id],
                      )
                    }
                  />
                  <BookOpen size={17} />
                  <span>
                    {b.name}
                    <small>
                      {b.access === 'read' ? '可阅读' : b.access === 'manage' ? '可管理' : '可编辑'}
                    </small>
                  </span>
                  {draft.includes(b.id) && <Check size={16} />}
                </label>
              ))}
            {!bases.length && (
              <p className="scope-empty">还没有可访问的知识库。请先创建，或联系管理员授权。</p>
            )}
            {!!bases.length &&
              !bases.some((b) => b.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())) && (
                <p className="scope-empty">没有匹配的知识库，试试其他名称。</p>
              )}
          </div>
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          <p className="scope-hint">更换范围后，将从新上下文开始；已有聊天记录仍保留。</p>
          <div className="scope-footer">
            {onManage && (
              <button type="button" className="text-button" disabled={saving} onClick={onManage}>
                管理知识库 ↗
              </button>
            )}
            <button
              type="button"
              className="primary"
              disabled={!draft.length || saving}
              onClick={() => void apply()}
            >
              {saving ? '保存中…' : `应用范围${draft.length ? `（${draft.length}）` : ''}`}
            </button>
          </div>
        </section>
      )}
    </div>
  );
}
