import { useEffect, useState } from 'react';
import { clearApiFailures, dismissApiFailure, subscribeApiFailures, type ApiFailure } from './errors';

/** 状态码 -> 可读标签 */
function statusLabel(status: number): string {
  if (status === 0) return '网络';
  if (status === 403) return '无权限';
  if (status === 404) return '不存在';
  if (status === 429) return '限流';
  if (status >= 500) return '服务端';
  return `HTTP ${status}`;
}

function formatTime(at: number): string {
  return new Date(at).toLocaleTimeString();
}

/**
 * 全局 API 错误面板:任何 API 调用失败都会出现在这里,
 * 保证不会因为某个调用点忘了 catch 而静默丢失。
 */
export default function ErrorToaster() {
  const [failures, setFailures] = useState<ApiFailure[]>([]);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => subscribeApiFailures(setFailures), []);

  if (failures.length === 0) return null;

  const total = failures.reduce((sum, item) => sum + item.count, 0);
  const latest = failures[0];

  return (
    <div className={`api-errors${expanded ? ' expanded' : ''}`} role="region" aria-label="接口错误">
      <button
        className="api-errors-bar"
        onClick={() => setExpanded((current) => !current)}
        aria-expanded={expanded}
      >
        <span className="api-errors-dot" aria-hidden="true" />
        <strong>{expanded ? '接口错误' : `${latest.message}`}</strong>
        <span className="api-errors-meta">
          {total > 1 && <span className="api-errors-count">{total}</span>}
          {statusLabel(latest.status)}
          <span className="api-errors-caret">{expanded ? '▾' : '▸'}</span>
        </span>
      </button>

      {expanded && (
        <div className="api-errors-list">
          <div className="api-errors-head">
            <span>{failures.length} 类错误</span>
            <button className="mini-button ghost" onClick={clearApiFailures}>全部清除</button>
          </div>
          {failures.map((failure) => (
            <div className="api-error-row" key={failure.key}>
              <div className="api-error-main">
                <strong>{failure.message}</strong>
                <small>
                  <code>{failure.method} {failure.path}</code>
                  <i>·</i>{statusLabel(failure.status)}
                  <i>·</i>{formatTime(failure.lastAt)}
                  {failure.count > 1 && <><i>·</i>出现 {failure.count} 次</>}
                </small>
              </div>
              <button
                className="mini-button ghost"
                onClick={() => dismissApiFailure(failure.key)}
                aria-label="忽略这条错误"
              >✕</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
