export interface User {
  id: string;
  username: string;
  role: string;
  permissions?: string[];
  active?: boolean;
  must_change_password?: boolean;
}
export interface Conversation {
  attachment_ids?: string[];
  attachments?: Attachment[];
  unavailable_attachment_ids?: string[];
  use_knowledge?: boolean;
  id: string;
  title: string;
  created_at: number;
  base_ids?: string[] | null;
  scope_start_id?: number;
}
export interface Source {
  id: string;
  text: string;
  score: number;
  rerank_score?: number;
  metadata: {
    attachment_id?: string;
    title?: string;
    filename?: string;
    source?: string;
    category?: string;
    rerank_status?: 'ranked' | 'fallback';
    rerank_model?: string;
    web_providers?: string[];
    web_status?: 'complete' | 'partial';
    web_failed_providers?: string[];
  };
}
export interface Step {
  node: string;
  label: string;
  sources: number;
  rewrites: number;
  generations: number;
}
export interface Answer {
  knowledge_scope?: {
    base_ids: string[] | null;
    base_names: string[];
    scope_start_id: number;
    use_knowledge?: boolean;
  };
  attachments?: Attachment[];
  safety?: { decision: 'allowed' | 'blocked'; stage?: string; policy_version?: string; backend: string };
  answer: string;
  sources: Source[];
  trace: Step[];
  status: string;
  mode: string;
  runtime: string;
  conversation_id: string;
}
export interface Message {
  id: number;
  role: string;
  content: string;
  details?: Partial<Answer>;
}

export interface Attachment {
  id: string;
  filename: string;
  media_type: string;
  size: number;
  storage_size: number;
  text_chars: number;
  image_count: number;
  created_at: number;
}
export interface AttachmentSpace {
  items: Attachment[];
  used_bytes: number;
  quota_bytes: number;
  max_files: number;
  max_upload_bytes: number;
  max_per_message: number;
  max_chars: number;
  max_images: number;
  vision_configured: boolean;
}
export interface KnowledgeDocument {
  can_write?: boolean;
  base_id: string;
  id: string;
  filename: string;
  status: string;
  chunk_count: number;
  created_at: number;
  error?: string;
  duplicate?: boolean;
}
export interface SystemInfo {
  reranker?: {
    enabled: boolean;
    provider: 'none' | 'huggingface' | 'api';
    model: string | null;
    state: string;
    candidates: number;
    max_length: number;
    min_score: number | null;
    failure_mode: 'error' | 'rrf';
    last_error: string | null;
  };
  moderation?: { enabled: boolean; backend: string; policy_version: string; failure_mode: string };
  graph_enabled: boolean;
  langfuse_enabled: boolean;
  knowledge_domain: string;
  mode: string;
  vector_backend: string;
  embedding_provider: string;
  web_search_enabled: boolean;
  web_search?: {
    enabled: boolean;
    provider: 'tavily' | 'exa' | 'hybrid';
    configured: Record<string, boolean>;
    max_results: number;
    timeout: number;
    failure_mode: 'error' | 'partial';
    scope_policy: 'unscoped_only';
  };
  version: string;
  max_upload_bytes: number;
}
