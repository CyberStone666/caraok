import { reportApiFailure } from './errors';
import type { AccessToken, AdminTrack, Category, DownloadJob, Tier, Track } from './types';

export class ApiError extends Error {
  readonly status: number;
  /** 该失败是否已上报到全局错误通道(避免重复播报) */
  reported = true;

  constructor(status: number, message: string, reported = true) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.reported = reported;
  }
}

/** 这些状态的失败属于"预期内"(未登录/无权限),不上报为异常 */
const SILENT_STATUSES = new Set([401]);

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const method = (init.method ?? 'GET').toUpperCase();

  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers,
      credentials: 'same-origin',
      cache: 'no-store',
    });
  } catch {
    const message = '网络连接失败，请检查网络后重试。';
    reportApiFailure(method, path, 0, message);
    throw new ApiError(0, message);
  }

  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) message = payload.detail;
    } catch {
      // Keep the status-based message when the response is not JSON.
    }
    // 401 是"会话失效"的正常信号,由调用方跳转登录页,不当异常播报
    if (!SILENT_STATUSES.has(response.status)) {
      reportApiFailure(method, path, response.status, message);
    }
    throw new ApiError(response.status, message, !SILENT_STATUSES.has(response.status));
  }
  return response.json() as Promise<T>;
}

export const api = {
  me: () => request<{ authenticated: boolean; tier: Tier }>('/api/v1/auth/me'),
  login: (token: string) => request<{ authenticated: boolean }>('/api/v1/auth/session', {
    method: 'POST', body: JSON.stringify({ token }),
  }),
  logout: () => request<{ authenticated: boolean }>('/api/v1/auth/logout', { method: 'POST' }),
  categories: () => request<Category[]>('/api/v1/categories'),
  adminCategories: () => request<Category[]>('/api/v1/admin/categories'),
  tracks: (category?: string) => request<Track[]>(`/api/v1/tracks${category ? `?category=${encodeURIComponent(category)}` : ''}`),
  adminTracks: () => request<AdminTrack[]>('/api/v1/admin/tracks'),
  adminTokens: () => request<AccessToken[]>('/api/v1/admin/tokens'),
  adminCreateToken: (body: { name: string }) =>
    request<{ id: string; name: string; token: string; created_at: number }>('/api/v1/admin/tokens', {
      method: 'POST', body: JSON.stringify(body),
    }),
  adminDeleteToken: (id: string) =>
    request<{ deleted: boolean }>(`/api/v1/admin/tokens/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  adminAddTrack: (body: { title: string; url: string; category_id: string; artist?: string; description?: string; mime_type?: string }) =>
    request<{ id: string }>('/api/v1/admin/tracks', { method: 'POST', body: JSON.stringify(body) }),
  adminPatchTrack: (id: string, body: Record<string, unknown>) =>
    request<{ id: string }>(`/api/v1/admin/tracks/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(body) }),
  adminDeleteTrack: (id: string, deleteFile: boolean) =>
    request<{ file_removed: boolean }>(`/api/v1/admin/tracks/${encodeURIComponent(id)}?delete_file=${deleteFile}`, { method: 'DELETE' }),
  adminAddDownload: (body: { youtube_url: string; category_id: string; name?: string; title?: string; artist?: string; description?: string }) =>
    request<{ job_id: string; state: string; slug: string }>('/api/v1/admin/downloads', { method: 'POST', body: JSON.stringify(body) }),
  adminDownloads: () => request<DownloadJob[]>('/api/v1/admin/downloads'),
};

export function streamUrl(trackId: string): string {
  return `/api/v1/stream/${encodeURIComponent(trackId)}`;
}
