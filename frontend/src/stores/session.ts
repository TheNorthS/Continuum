import { create } from 'zustand';
import type { User } from '../types';

interface Session {
  token: string;
  refresh: string;
  user: User | null;
  login: (token: string, user: User, refresh?: string) => void;
  logout: () => void;
}

// Session-scoped storage: credentials are not kept across browser sessions.
export const useSession = create<Session>((set) => ({
  token: sessionStorage.getItem('rag-e-token') || '',
  refresh: sessionStorage.getItem('rag-e-refresh') || '',
  user: null,
  login: (token, user, refresh) => {
    sessionStorage.setItem('rag-e-token', token);
    if (refresh) sessionStorage.setItem('rag-e-refresh', refresh);
    set({ token, user, ...(refresh ? { refresh } : {}) });
  },
  logout: () => {
    sessionStorage.removeItem('rag-e-token');
    sessionStorage.removeItem('rag-e-refresh');
    set({ token: '', refresh: '', user: null });
  },
}));
