import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { api, ApiError, streamUrl } from './api';
import type { Category, Track } from './types';
import AdminPage from './admin';
import ErrorToaster from './ErrorToaster';
import type { Tier } from './types';

type AuthState = 'checking' | 'signed-out' | 'signed-in';
type Page = 'home' | 'player' | 'admin';

function publishMediaPosition(audio: HTMLAudioElement) {
  if (!('mediaSession' in navigator) || typeof navigator.mediaSession.setPositionState !== 'function') return;
  const duration = audio.duration;
  if (!Number.isFinite(duration) || duration <= 0) return;
  const position = Math.min(Math.max(audio.currentTime, 0), duration);
  try {
    navigator.mediaSession.setPositionState({
      duration,
      playbackRate: audio.playbackRate || 1,
      position,
    });
  } catch {
    // Some browsers reject a position while metadata is still changing.
  }
}

function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${String(secs).padStart(2, '0')}`;
}

function Cover({ track, large = false }: { track?: Track | null; large?: boolean }) {
  if (track?.cover_url) {
    return <img className={`cover${large ? ' cover-large' : ''}`} src={track.cover_url} alt="" loading="lazy" />;
  }
  const label = track?.category_name ?? 'CARAOK';
  return (
    <div className={`cover cover-generated${large ? ' cover-large' : ''}`} aria-label={label}>
      <span className="cover-wave">〰</span>
      <span>{label.slice(0, 16)}</span>
    </div>
  );
}

function Login({ onLogin }: { onLogin: (token: string) => Promise<void> }) {
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token.trim()) return;
    setBusy(true);
    setError('');
    try {
      await onLogin(token.trim());
      setToken('');
    } catch (err) {
      const raw = err instanceof Error ? err.message : '登录失败';
      setError(raw === 'Invalid access token' ? 'Token 不正确，请核对后重试。' : raw);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-screen">
      <div className="login-card">
        <div className="eq login-eq" aria-hidden="true"><i /><i /><i /><i /><i /></div>
        <div className="login-head">
          <div className="brand-mark">〰</div>
          <div>
            <h1>Caraok</h1>
            <p className="eyebrow">Private Audio · Night Drive</p>
          </div>
        </div>
        <p className="muted login-copy">输入你的个人访问 Token，连接私人音频库。</p>
        <form onSubmit={submit} className="login-form">
          <label htmlFor="access-token">访问 Token</label>
          <input
            id="access-token"
            type="text"
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            value={token}
            onChange={(event) => setToken(event.target.value)}
            placeholder="粘贴服务端 ACCESS_TOKEN"
            required
          />
          {error && <p className="error-text" role="alert">{error}</p>}
          <button className="primary-button login-button" type="submit" disabled={busy || !token.trim()}>
            {busy ? '正在连接…' : '连接音频库'}
          </button>
        </form>
        <p className="security-note">Token 只用于建立 HttpOnly 会话，不会保存到浏览器本地存储。</p>
      </div>
    </main>
  );
}

export default function App() {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [auth, setAuth] = useState<AuthState>('checking');
  const [tier, setTier] = useState<Tier>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [tracks, setTracks] = useState<Track[]>([]);
  const [activeCategory, setActiveCategory] = useState<string>('all');
  const [currentTrack, setCurrentTrack] = useState<Track | null>(null);
  const [page, setPage] = useState<Page>('home');
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('');

  const visibleTracks = useMemo(
    () => activeCategory === 'all' ? tracks : tracks.filter((track) => track.category_id === activeCategory),
    [activeCategory, tracks],
  );
  const currentIndex = currentTrack ? visibleTracks.findIndex((track) => track.id === currentTrack.id) : -1;

  const loadLibrary = useCallback(async () => {
    setLoading(true);
    setMessage('');
    try {
      const [nextCategories, nextTracks] = await Promise.all([api.categories(), api.tracks()]);
      setCategories(nextCategories);
      setTracks(nextTracks);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        // Session expired or revoked: return to the login screen instead of
        // showing repeated failures while signed in.
        setAuth('signed-out');
        return;
      }
      setMessage(error instanceof Error ? error.message : '加载作品列表失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    api.me()
      .then((result) => {
        setAuth(result.authenticated ? 'signed-in' : 'signed-out');
        setTier(result.authenticated ? result.tier : null);
      })
      .catch(() => setAuth('signed-out'));
  }, []);

  useEffect(() => {
    if (auth === 'signed-in') void loadLibrary();
  }, [auth, loadLibrary]);

  // 会话层级变化(如令牌被吊销后重新探测)时,把管理页复位到首页,
  // 避免停留在无权访问的界面并持续产生 403 请求。
  useEffect(() => {
    if (page === 'admin' && tier !== 'primary') setPage('home');
  }, [page, tier]);

  const startTrack = useCallback(async (track: Track, shouldPlay = true) => {
    const audio = audioRef.current;
    setCurrentTrack(track);
    setPage('player');
    setCurrentTime(0);
    setDuration(0);
    setMessage('');
    if (!audio) return;
    audio.pause();
    audio.src = streamUrl(track.id);
    audio.load();
    if (shouldPlay) {
      try {
        await audio.play();
        setPlaying(true);
      } catch {
        setPlaying(false);
        setMessage('车机浏览器阻止了自动播放，请再次点击播放。');
      }
    } else {
      setPlaying(false);
    }
  }, []);

  const togglePlayback = useCallback(async () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) {
      try {
        await audio.play();
        setPlaying(true);
      } catch {
        setMessage('无法开始播放，请检查网络或音频源。');
      }
    } else {
      audio.pause();
      setPlaying(false);
    }
  }, []);

  const playRelative = useCallback((direction: -1 | 1) => {
    if (!visibleTracks.length) return;
    const start = currentIndex < 0 ? (direction === 1 ? -1 : 0) : currentIndex;
    const next = (start + direction + visibleTracks.length) % visibleTracks.length;
    void startTrack(visibleTracks[next]);
  }, [currentIndex, startTrack, visibleTracks]);

  useEffect(() => {
    if (!currentTrack || !('mediaSession' in navigator) || typeof MediaMetadata === 'undefined') return;
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: currentTrack.title,
        artist: currentTrack.artist,
        album: currentTrack.category_name,
        artwork: currentTrack.cover_url ? [{ src: currentTrack.cover_url }] : [],
      });
      navigator.mediaSession.setActionHandler('play', () => { void togglePlayback(); });
      navigator.mediaSession.setActionHandler('pause', () => { audioRef.current?.pause(); setPlaying(false); });
      navigator.mediaSession.setActionHandler('previoustrack', () => playRelative(-1));
      navigator.mediaSession.setActionHandler('nexttrack', () => playRelative(1));
      navigator.mediaSession.setActionHandler('seekto', (details) => {
        if (details.seekTime !== undefined && audioRef.current) audioRef.current.currentTime = details.seekTime;
      });
    } catch (error) {
      console.debug('Media Session is partially supported on this browser', error);
    }
  }, [currentTrack, playRelative, togglePlayback]);

  useEffect(() => {
    if (!('mediaSession' in navigator)) return;
    navigator.mediaSession.playbackState = playing ? 'playing' : 'paused';
  }, [playing]);

  async function login(token: string) {
    await api.login(token);
    setAuth('signed-in');
    // 登录响应不携带层级,重新探测一次以更新 ⚙ 入口可见性
    void api.me().then((result) => setTier(result.authenticated ? result.tier : null));
  }

  async function logout() {
    try {
      audioRef.current?.pause();
      await api.logout();
    } finally {
      setCurrentTrack(null);
      setPlaying(false);
      setAuth('signed-out');
      setTier(null);
    }
  }

  if (auth === 'checking') {
    return <main className="loading-screen"><div className="brand-mark">〰</div><p>正在连接 Caraok…</p></main>;
  }
  if (auth === 'signed-out') {
    return (
      <>
        <Login onLogin={login} />
        <ErrorToaster />
      </>
    );
  }

  return (
    <div className="app-shell">
      <audio
        ref={audioRef}
        preload="none"
        onTimeUpdate={(event) => {
          setCurrentTime(event.currentTarget.currentTime);
          publishMediaPosition(event.currentTarget);
        }}
        onDurationChange={(event) => {
          setDuration(Number.isFinite(event.currentTarget.duration) ? event.currentTarget.duration : 0);
          publishMediaPosition(event.currentTarget);
        }}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => playRelative(1)}
        onError={() => {
          if (currentTrack) setMessage('音频加载失败：请检查 URL、授权状态和上游是否支持 Range 请求。');
          setPlaying(false);
        }}
      />

      <header className="topbar">
        <button className="brand-button" onClick={() => setPage('home')} aria-label="返回首页">
          <span className="brand-mark small">〰</span>
          <span>CARAOK</span>
        </button>
        <div className="topbar-right">
          <span className="private-pill"><span className="status-dot" /> PRIVATE</span>
          {tier === 'primary' && (
            <button className="icon-button" onClick={() => setPage('admin')} aria-label="曲库管理" title="曲库管理">⚙</button>
          )}
          <button className="icon-button" onClick={() => void loadLibrary()} aria-label="刷新作品列表" title="刷新作品列表">↻</button>
          <button className="icon-button" onClick={() => void logout()} aria-label="退出登录" title="退出登录">⇥</button>
        </div>
      </header>

      {page === 'home' ? (
        <main className="content home-content">
          <section className="hero">
            <div>
              <p className="eyebrow">YOUR PRIVATE SOUNDTRACK</p>
              <h1>路上，听点好的。</h1>
              <p className="muted">你的精选音频，随时出发。</p>
            </div>
            <div className="hero-art" aria-hidden="true">
              <span className="hero-horizon" />
              <div className="eq"><i /><i /><i /><i /><i /><i /><i /></div>
            </div>
          </section>

          <section className="section-block">
            <div className="section-heading">
              <div><p className="eyebrow">EXPLORE</p><h2>分类</h2></div>
              <span className="muted small-text">{categories.length} 个分类</span>
            </div>
            <div className="category-row">
              <button className={`category-chip${activeCategory === 'all' ? ' active' : ''}`} onClick={() => setActiveCategory('all')}>
                <span>✦</span><strong>全部</strong><small>{tracks.length}</small>
              </button>
              {categories.map((category, index) => (
                <button
                  className={`category-chip${activeCategory === category.id ? ' active' : ''}`}
                  key={category.id}
                  onClick={() => setActiveCategory(category.id)}
                >
                  <span>{['◉', '♫', '✧', '◌', '⌁'][index % 5]}</span>
                  <strong>{category.name}</strong>
                  <small>{category.count}</small>
                </button>
              ))}
            </div>
          </section>

          <section className="section-block track-section">
            <div className="section-heading">
              <div><p className="eyebrow">CURATED FOR YOU</p><h2>{activeCategory === 'all' ? '精选列表' : categories.find((cat) => cat.id === activeCategory)?.name ?? '精选列表'}</h2></div>
              <span className="muted small-text">{visibleTracks.length} 个作品</span>
            </div>
            {message && <div className="notice" role="status">{message}</div>}
            {loading ? <div className="empty-state">正在加载作品…</div> : visibleTracks.length === 0 ? (
              <div className="empty-state">
                <span className="empty-icon">♫</span>
                <strong>这里还没有作品</strong>
                <p>在服务器的 config/tracks.yaml 中添加音频作品并启用。</p>
              </div>
            ) : (
              <div className="track-list">
                {visibleTracks.map((track, index) => (
                  <button className="track-row" key={track.id} onClick={() => void startTrack(track)}>
                    <span className="track-number">{String(index + 1).padStart(2, '0')}</span>
                    <Cover track={track} />
                    <span className="track-copy">
                      <strong>{track.title}</strong>
                      <small>{track.artist} <i>·</i> {track.category_name}</small>
                    </span>
                    <span className="track-play" aria-hidden="true">▶</span>
                  </button>
                ))}
              </div>
            )}
          </section>
          <footer className="footer-note">PERSONAL LIBRARY · STREAMED THROUGH YOUR SERVER</footer>
        </main>
      ) : page === 'admin' && tier === 'primary' ? (
        <AdminPage onBack={() => { setPage('home'); void loadLibrary(); }} tier={tier} />
      ) : (
        <main className="content player-content">
          <button className="back-button" onClick={() => setPage('home')}>← <span>返回作品列表</span></button>
          {currentTrack ? (
            <div className="player-layout">
              <div className="player-cover-wrap">
                <Cover track={currentTrack} large />
                <div className="eq" aria-hidden="true"><i /><i /><i /><i /><i /><i /><i /><i /><i /><i /><i /><i /></div>
              </div>
              <div className="player-details">
                <p className="eyebrow">NOW PLAYING · {currentTrack.category_name.toUpperCase()}</p>
                <h1>{currentTrack.title}</h1>
                <p className="muted artist-name">{currentTrack.artist}</p>
                {currentTrack.description && <p className="track-description">{currentTrack.description}</p>}
                {message && <div className="notice" role="status">{message}</div>}
                <div className="timeline">
                  <input
                    aria-label="播放进度"
                    type="range"
                    min={0}
                    max={duration || 0}
                    step={1}
                    value={Math.min(currentTime, duration || 0)}
                    disabled={!duration}
                    onChange={(event) => {
                      const value = Number(event.target.value);
                      if (audioRef.current) audioRef.current.currentTime = value;
                      setCurrentTime(value);
                    }}
                  />
                  <div className="time-labels"><span>{formatTime(currentTime)}</span><span>{formatTime(duration)}</span></div>
                </div>
                <div className="player-controls">
                  <button className="secondary-control" onClick={() => playRelative(-1)} aria-label="上一首">|◀</button>
                  <button className="main-play" onClick={() => void togglePlayback()} aria-label={playing ? '暂停' : '播放'}>{playing ? 'Ⅱ' : '▶'}</button>
                  <button className="secondary-control" onClick={() => playRelative(1)} aria-label="下一首">▶|</button>
                </div>
                <div className="player-footnote"><span className="status-dot" /> SERVER RELAY · PRIVATE SESSION</div>
              </div>
            </div>
          ) : <div className="empty-state">尚未选择作品。</div>}
          {tracks.length > 0 && (
            <section className="up-next">
              <div className="section-heading"><div><p className="eyebrow">KEEP LISTENING</p><h2>播放队列</h2></div></div>
              <div className="queue-list">
                {tracks.slice(Math.max(0, currentIndex + 1), Math.max(0, currentIndex + 1) + 4).map((track) => (
                  <button key={track.id} className="queue-row" onClick={() => void startTrack(track)}>
                    <Cover track={track} /><span><strong>{track.title}</strong><small>{track.artist}</small></span><span className="queue-arrow">↗</span>
                  </button>
                ))}
              </div>
            </section>
          )}
        </main>
      )}

      <ErrorToaster />
    </div>
  );
}
