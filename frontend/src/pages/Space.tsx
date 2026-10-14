import { useEffect, useRef, useState } from 'react';
import { Download, FileText, Image, Paperclip, Pencil, Trash2, Upload, X } from 'lucide-react';
import { api, attachmentBlob } from '../services/api';
import type { Attachment, AttachmentSpace } from '../types';

export const attachmentAccept = '.png,.jpg,.jpeg,.webp,.pdf,.docx,.txt,.md,.markdown';
export function fileSize(value: number) {
  return value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(value / 1024)} KB`;
}
export async function uploadAttachment(file: File) {
  const body = new FormData();
  body.append('file', file);
  return api<Attachment>('/attachments', { method: 'POST', body });
}
export async function downloadAttachment(item: Attachment) {
  const url = URL.createObjectURL(await attachmentBlob(item.id));
  const link = document.createElement('a');
  link.href = url;
  link.download = item.filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function AttachmentPreview({ item, onClose }: { item: Attachment; onClose: () => void }) {
  const [text, setText] = useState('');
  const [image, setImage] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true,
      url = '';
    api<Attachment & { text: string }>(`/attachments/${item.id}`)
      .then((data) => {
        if (active) setText(data.text);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    if (item.media_type.startsWith('image/')) {
      attachmentBlob(item.id)
        .then((blob) => {
          if (active) {
            url = URL.createObjectURL(blob);
            setImage(url);
          }
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    }
    return () => {
      active = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [item.id, item.media_type]);
  return (
    <div className="modal-backdrop">
      <section className="modal attachment-preview" role="dialog" aria-modal="true" aria-label="附件预览">
        <div className="space-toolbar">
          <h2>{item.filename}</h2>
          <button aria-label="关闭预览" onClick={onClose}>
            <X size={18} />
          </button>
        </div>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {image && <img src={image} alt={item.filename} />}
        {text ? (
          <pre>{text}</pre>
        ) : (
          !image && <p>此文件暂无文本预览，可下载原文件查看。图片将在提问时识别。</p>
        )}
        <button onClick={() => void downloadAttachment(item).catch((e) => setError(e.message))}>
          <Download size={16} /> 下载原文件
        </button>
      </section>
    </div>
  );
}

export function Space({
  selection,
  onToggle,
  selectionDisabled = false,
}: {
  selection?: string[];
  onToggle?: (item: Attachment) => void;
  selectionDisabled?: boolean;
}) {
  const [space, setSpace] = useState<AttachmentSpace | null>(null);
  const [query, setQuery] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<Attachment | null>(null);
  const [editing, setEditing] = useState<Attachment | null>(null);
  const [deleting, setDeleting] = useState<Attachment | null>(null);
  const [name, setName] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const reload = () => api<AttachmentSpace>('/attachments').then(setSpace);
  useEffect(() => {
    let active = true;
    api<AttachmentSpace>('/attachments')
      .then((s) => {
        if (active) setSpace(s);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, []);
  async function upload(files: FileList | null) {
    if (!files?.length) return;
    setBusy(true);
    setError('');
    try {
      for (const file of Array.from(files)) await uploadAttachment(file);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      await reload().catch((e) => setError(e.message));
      setBusy(false);
      if (input.current) input.current.value = '';
    }
  }
  async function mutate(action: () => Promise<unknown>) {
    setBusy(true);
    setError('');
    try {
      await action();
      await reload();
      setEditing(null);
      setDeleting(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const items = (space?.items ?? []).filter((a) => a.filename.toLowerCase().includes(query.toLowerCase()));
  return (
    <div className={selection ? 'space-picker' : 'content-page space-page'}>
      <div className="page-heading">
        <div>
          <span className="eyebrow">PERSONAL SPACE</span>
          <h1>空间管理</h1>
          <p className="subtle">保存自己的附件，在对话中按需选用。</p>
        </div>
        <button className="primary" disabled={busy} onClick={() => input.current?.click()}>
          <Upload size={16} />
          {busy ? '处理中…' : '上传附件'}
        </button>
      </div>
      <input
        ref={input}
        aria-label="上传到个人空间"
        type="file"
        accept={attachmentAccept}
        multiple
        hidden
        onChange={(e) => void upload(e.target.files)}
      />
      <p className="space-policy">
        支持图片、PDF、Word（DOCX）、TXT /
        Markdown。附件仅本人可见，不自动加入知识库；选择并发送后才用于回答。
      </p>
      {space && (
        <div className="space-usage">
          <span>
            {space.items.length} / {space.max_files} 个附件
          </span>
          <progress value={space.used_bytes} max={space.quota_bytes} aria-label="空间使用量" />
          <span>
            {fileSize(space.used_bytes)} / {fileSize(space.quota_bytes)}
          </span>
          <small>含加密与预览数据 · 单文件最多 {fileSize(space.max_upload_bytes)}</small>
        </div>
      )}
      {space && !space.vision_configured && (
        <p className="notice">
          视觉模型尚未配置。可以保存图片和 PDF，但包含图片的附件需配置视觉模型后才能用于问答。
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="space-toolbar">
        <input
          aria-label="搜索附件"
          placeholder="按文件名搜索…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button disabled={busy} onClick={() => void reload().catch((e) => setError(e.message))}>
          刷新
        </button>
      </div>
      {!space ? (
        <p role="status">正在读取空间…</p>
      ) : !items.length ? (
        <div className="empty">
          <Paperclip size={28} />
          <h3>暂无附件</h3>
          <p>上传文件后，可在聊天框中从空间选择。</p>
        </div>
      ) : (
        <div className="attachment-list">
          {items.map((item) => (
            <article className="attachment-row" key={item.id}>
              {selection && (
                <input
                  type="checkbox"
                  aria-label={`选择附件 ${item.filename}`}
                  checked={selection.includes(item.id)}
                  onChange={() => onToggle?.(item)}
                  disabled={busy || selectionDisabled}
                />
              )}
              {item.image_count ? <Image size={22} /> : <FileText size={22} />}
              <button className="attachment-name" onClick={() => setPreview(item)}>
                <strong>{item.filename}</strong>
                <small>
                  {fileSize(item.size)} · {item.text_chars.toLocaleString()} 字符
                  {item.image_count ? ` · ${item.image_count} 张图片/页面` : ''}
                </small>
              </button>
              <button
                aria-label={`下载 ${item.filename}`}
                disabled={busy}
                onClick={() => void downloadAttachment(item).catch((e) => setError(e.message))}
              >
                <Download size={16} />
              </button>
              {!selection && (
                <>
                  <button
                    aria-label={`重命名 ${item.filename}`}
                    disabled={busy}
                    onClick={() => {
                      setEditing(item);
                      setName(item.filename);
                    }}
                  >
                    <Pencil size={16} />
                  </button>
                  <button
                    aria-label={`删除 ${item.filename}`}
                    disabled={busy}
                    onClick={() => setDeleting(item)}
                  >
                    <Trash2 size={16} />
                  </button>
                </>
              )}
            </article>
          ))}
        </div>
      )}
      {preview && <AttachmentPreview key={preview.id} item={preview} onClose={() => setPreview(null)} />}
      {editing && (
        <div className="modal-backdrop">
          <form
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label="重命名附件"
            onSubmit={(e) => {
              e.preventDefault();
              void mutate(() =>
                api(`/attachments/${editing.id}`, {
                  method: 'PATCH',
                  body: JSON.stringify({ filename: name }),
                }),
              );
            }}
          >
            <h2>重命名附件</h2>
            <input
              aria-label="附件名称"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={180}
            />
            <p>请保留原文件扩展名。</p>
            <button type="button" disabled={busy} onClick={() => setEditing(null)}>
              取消
            </button>
            <button disabled={busy || !name.trim()}>保存</button>
          </form>
        </div>
      )}
      {deleting && (
        <div className="modal-backdrop">
          <section className="modal" role="dialog" aria-modal="true" aria-label="删除附件">
            <h2>删除 {deleting.filename}？</h2>
            <p>附件将从空间移除，引用它的历史回答会隐藏。此操作不可撤销。</p>
            <button disabled={busy} onClick={() => setDeleting(null)}>
              取消
            </button>
            <button
              className="danger"
              disabled={busy}
              onClick={() => void mutate(() => api(`/attachments/${deleting.id}`, { method: 'DELETE' }))}
            >
              确认删除
            </button>
          </section>
        </div>
      )}
    </div>
  );
}
