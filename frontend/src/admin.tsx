import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError } from './api';
import type { AccessToken, AdminTrack, Category, DownloadJob, Tier } from './types';

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

const STATE_LABEL: Record<DownloadJob['state'], string> = {
  queued: '排队中',
  downloading: '下载中',
  remuxing: '封装中',
  done: '完成',
  error: '失败',
};

function StatusBadge({ track }: { track: AdminTrack }) {
  const file = track.file;
  if (file.source === 'local') {
    if (file.exists) {
      return <span className="badge badge-ok" title={file.file_name}>就绪 · {formatBytes(file.size_bytes ?? 0)}</span>;
    }
    return <span className="badge badge-missing">文件缺失{file.detail ? ` · ${file.detail}` : ''}</span>;
  }
  return <span className="badge badge-remote">远端 · {file.url_host}</span>;
}

export default function AdminPage({ onBack, tier }: { onBack: () => void; tier: Tier }) {
  const [tracks, setTracks] = useState<AdminTrack[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [downloads, setDownloads] = useState<DownloadJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState('');
  const [youtubeUrl, setYoutubeUrl] = useState('');
  const [trackName, setTrackName] = useState('');
  const [addCategory, setAddCategory] = useState('');
  const [directUrl, setDirectUrl] = useState('');
  const [directTitle, setDirectTitle] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState('');
  const [editArtist, setEditArtist] = useState('');
  const [tokens, setTokens] = useState<AccessToken[]>([]);
  const [tokenName, setTokenName] = useState('');
  const [mintedToken, setMintedToken] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  const loadTracks = useCallback(async () => {
    try {
      setTracks(await api.adminTracks());
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : '加载管理数据失败');
    } finally {
      setLoading(false);
    }
  }, []);

  const loadDownloads = useCallback(async () => {
    try {
      setDownloads(await api.adminDownloads());
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : '加载管理数据失败');
    }
  }, []);

  const loadTokens = useCallback(async () => {
    if (tier !== 'primary') return;
    try {
      setTokens(await api.adminTokens());
    } catch (error) {
      // 失败已由 api 层上报到全局错误面板,这里只补一条页内提示
      setMessage(error instanceof ApiError ? error.message : '加载访问令牌失败');
    }
  }, [tier]);

  useEffect(() => {
    void loadTokens();
  }, [loadTokens]);

  async function createToken(event: React.FormEvent) {
    event.preventDefault();
    if (!tokenName.trim()) return;
    try {
      const created = await api.adminCreateToken({ name: tokenName.trim() });
      setMintedToken(created.token);
      setTokenName('');
      setTokens((current) => [
        ...current,
        { id: created.id, name: created.name, created_at: created.created_at, last_used_at: null },
      ]);
      setMessage('令牌已生成,明文仅显示这一次,请立即保存。');
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : '生成失败');
    }
  }

  async function removeToken(token: AccessToken) {
    if (!window.confirm(`删除访问令牌「${token.name}」?使用它的会话将立即失效。`)) return;
    try {
      await api.adminDeleteToken(token.id);
      void loadTokens();
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : '删除失败');
    }
  }

  useEffect(() => {
    api.adminCategories().then((list) => {
      setCategories(list);
      if (list.length > 0) setAddCategory((current) => current || list[0].id);
      else setMessage('还没有分类，无法添加作品。');
    }).catch((error) => setMessage(error instanceof ApiError ? error.message : '加载分类失败'));
  }, []);

  useEffect(() => {
    void loadTracks();
    void loadDownloads();
  }, [loadTracks, loadDownloads]);

  const busyJobs = useMemo(
    () => downloads.filter((job) => job.state === 'queued' || job.state === 'downloading' || job.state === 'remuxing'),
    [downloads],
  );
  const hasBusy = busyJobs.length > 0;

  useEffect(() => {
    if (!hasBusy) return undefined;
    pollRef.current = window.setInterval(() => { void loadDownloads(); }, 3000);
    return () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
      pollRef.current = null;
    };
  }, [hasBusy, loadDownloads]);

  const seenJobs = useRef<Map<string, DownloadJob['state']>>(new Map());
  useEffect(() => {
    let refreshTracks = false;
    for (const job of downloads) {
      const previous = seenJobs.current.get(job.job_id);
      if (previous && previous !== job.state && (job.state === 'done' || job.state === 'error')) {
        refreshTracks = true;
        if (job.state === 'error') setMessage(job.error || '下载失败');
      }
      seenJobs.current.set(job.job_id, job.state);
    }
    if (refreshTracks) void loadTracks();
  }, [downloads, loadTracks]);

  async function submitYouTube(event: React.FormEvent) {
    event.preventDefault();
    if (!youtubeUrl.trim() || !addCategory) return;
    setSubmitting(true);
    setMessage('');
    try {
      await api.adminAddDownload({
        youtube_url: youtubeUrl.trim(),
        category_id: addCategory,
        name: trackName.trim(),
      });
      setYoutubeUrl('');
      setTrackName('');
      setMessage('已加入下载队列,完成后自动进入曲库。');
      void loadDownloads();
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : '提交失败');
    } finally {
      setSubmitting(false);
    }
  }

  async function submitDirect(event: React.FormEvent) {
    event.preventDefault();
    if (!directUrl.trim() || !directTitle.trim() || !addCategory) return;
    setSubmitting(true);
    setMessage('');
    try {
      await api.adminAddTrack({
        title: directTitle.trim(),
        url: directUrl.trim(),
        category_id: addCategory,
      });
      setDirectUrl('');
      setDirectTitle('');
      setMessage('直链作品已添加。');
      void loadTracks();
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : '添加失败');
    } finally {
      setSubmitting(false);
    }
  }

  async function toggleEnabled(track: AdminTrack) {
    try {
      await api.adminPatchTrack(track.id, { enabled: !track.enabled });
      void loadTracks();
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : '更新失败');
    }
  }

  async function saveEdit(track: AdminTrack) {
    try {
      await api.adminPatchTrack(track.id, { title: editTitle.trim() || track.title, artist: editArtist.trim() });
      setEditing(null);
      void loadTracks();
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : '保存失败');
    }
  }

  async function remove(track: AdminTrack) {
    const isLocal = track.file.source === 'local' && track.file.exists === true;
    const question = isLocal
      ? `删除「${track.title}」?本地音频文件将一并删除。`
      : `删除「${track.title}」?`;
    if (!window.confirm(question)) return;
    try {
      await api.adminDeleteTrack(track.id, isLocal);
      void loadTracks();
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : '删除失败');
    }
  }

  return (
    <main className="content admin-content">
      <button className="back-button" onClick={onBack}>← <span>返回播放</span></button>
      <section className="section-block">
        <div className="section-heading">
          <div><p className="eyebrow">LIBRARY CONSOLE</p><h2>曲库管理</h2></div>
          <span className="muted small-text">{tracks.length} 个作品</span>
        </div>
        {message && <div className="notice" role="status">{message}</div>}

        <form className="admin-form" onSubmit={submitYouTube}>
          <div className="form-row">
            <label htmlFor="yt-url">添加 YouTube 音频</label>
            <input
              id="yt-url" type="url" required placeholder="https://www.youtube.com/watch?v=…"
              value={youtubeUrl} onChange={(event) => setYoutubeUrl(event.target.value)}
            />
          </div>
          <div className="form-row form-row-split">
            <input
              type="text" placeholder="文件名(可选,留空自动)" value={trackName}
              onChange={(event) => setTrackName(event.target.value)}
            />
            <select value={addCategory} onChange={(event) => setAddCategory(event.target.value)} disabled={categories.length === 0}>
              {categories.length === 0 && <option value="">没有可用分类</option>}
              {categories.map((category) => <option key={category.id} value={category.id}>{category.name}</option>)}
            </select>
            <button className="primary-button" type="submit" disabled={submitting || !addCategory || busyJobs.length >= 4}>
              {submitting ? '提交中…' : '加入下载队列'}
            </button>
          </div>
          <p className="muted small-text">下载在服务器后台执行(cookies + WARP + PO Token),完成后自动注册进曲库。</p>
        </form>

        <details className="admin-direct">
          <summary>直链音频 URL(高级)</summary>
          <form className="admin-form" onSubmit={submitDirect}>
            <div className="form-row">
              <input type="url" required placeholder="https://主机/路径/file.mp3" value={directUrl}
                onChange={(event) => setDirectUrl(event.target.value)} />
            </div>
            <div className="form-row form-row-split">
              <input type="text" required placeholder="标题" value={directTitle}
                onChange={(event) => setDirectTitle(event.target.value)} />
              <select value={addCategory} onChange={(event) => setAddCategory(event.target.value)} disabled={categories.length === 0}>
                {categories.length === 0 && <option value="">没有可用分类</option>}
                {categories.map((category) => <option key={category.id} value={category.id}>{category.name}</option>)}
              </select>
              <button className="primary-button" type="submit" disabled={submitting || !addCategory}>添加直链</button>
            </div>
          </form>
        </details>
      </section>

      {busyJobs.length > 0 && (
        <section className="section-block">
          <div className="section-heading"><div><p className="eyebrow">IN FLIGHT</p><h2>下载任务</h2></div></div>
          <div className="job-list">
            {busyJobs.map((job) => (
              <div className="job-row" key={job.job_id}>
                <span className="job-title">{job.title || job.slug}</span>
                <div className="job-bar"><div className="job-bar-fill" style={{ width: `${Math.max(4, job.progress)}%` }} /></div>
                <span className="job-state">{STATE_LABEL[job.state]} · {Math.round(job.progress)}%</span>
              </div>
            ))}
          </div>
        </section>
      )}

      {tier === 'primary' && (
        <section className="section-block">
          <div className="section-heading">
            <div><p className="eyebrow">ACCESS TOKENS</p><h2>访问令牌</h2></div>
            <span className="muted small-text">{tokens.length} 个访客令牌</span>
          </div>
          {mintedToken && (
            <div className="notice" role="status">
              <strong>新令牌(仅显示这一次,关闭后无法找回):</strong>
              <code className="minted-token">{mintedToken}</code>
              <button className="mini-button" onClick={() => { void navigator.clipboard?.writeText(mintedToken); }}>复制</button>
            </div>
          )}
          <form className="admin-form" onSubmit={createToken}>
            <div className="form-row form-row-split">
              <input
                type="text" required maxLength={60} placeholder="令牌名称(如:家人 / 车机)"
                value={tokenName} onChange={(event) => setTokenName(event.target.value)}
              />
              <button className="primary-button" type="submit">生成访客令牌</button>
            </div>
            <p className="muted small-text">访客令牌可登录收听,但不能进入本管理页、不能管理令牌;删除后其会话立即失效。</p>
          </form>
          <div className="admin-table">
            {tokens.length === 0 ? <div className="empty-state">还没有访客令牌。</div> : tokens.map((token) => (
              <div className="admin-row" key={token.id}>
                <div className="admin-row-main">
                  <strong>{token.name}</strong>
                  <small>创建于 {new Date(token.created_at * 1000).toLocaleString()}<i>·</i>
                    {token.last_used_at ? `最近使用 ${new Date(token.last_used_at * 1000).toLocaleString()}` : '从未使用'}</small>
                </div>
                <div className="admin-row-side">
                  <button className="mini-button danger" onClick={() => void removeToken(token)}>删除</button>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="section-block">
        <div className="section-heading"><div><p className="eyebrow">CATALOG</p><h2>作品列表</h2></div></div>
        {loading ? <div className="empty-state">加载中…</div> : tracks.length === 0 ? (
          <div className="empty-state"><strong>曲库为空</strong><p>先在上方添加一个作品。</p></div>
        ) : (
          <div className="admin-table">
            {tracks.map((track) => (
              <div className="admin-row" key={track.id}>
                <div className="admin-row-main">
                  {editing === track.id ? (
                    <div className="edit-fields">
                      <input value={editTitle} onChange={(event) => setEditTitle(event.target.value)} placeholder="标题" />
                      <input value={editArtist} onChange={(event) => setEditArtist(event.target.value)} placeholder="作者" />
                      <button className="mini-button" onClick={() => void saveEdit(track)}>保存</button>
                      <button className="mini-button ghost" onClick={() => setEditing(null)}>取消</button>
                    </div>
                  ) : (
                    <>
                      <strong>{track.title}</strong>
                      <small>{track.artist} <i>·</i> {track.category_name || track.category_id} <i>·</i> {track.id}</small>
                    </>
                  )}
                </div>
                <div className="admin-row-side">
                  <StatusBadge track={track} />
                  <button
                    className={`mini-button${track.enabled ? ' ghost' : ''}`}
                    onClick={() => void toggleEnabled(track)}
                    title={track.enabled ? '下架' : '上架'}
                  >{track.enabled ? '下架' : '上架'}</button>
                  <button className="mini-button ghost" onClick={() => {
                    setEditing(track.id); setEditTitle(track.title); setEditArtist(track.artist);
                  }}>编辑</button>
                  <button className="mini-button danger" onClick={() => void remove(track)}>删除</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </main>
  );
}
