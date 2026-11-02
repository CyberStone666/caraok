/**
 * 全局 API 错误通道。
 *
 * 所有经 api.ts 发出的请求失败都会进这里,由 <ErrorToaster /> 统一呈现,
 * 保证任何一个调用点的失败都不会被静默吞掉。
 */

export type ApiFailure = {
  /** 去重键:同 method+path+status 的连续失败合并计数 */
  key: string;
  method: string;
  path: string;
  status: number;
  message: string;
  count: number;
  firstAt: number;
  lastAt: number;
};

type Listener = (failures: ApiFailure[]) => void;

const MAX_ENTRIES = 20;
/** 同一错误的静默窗口:窗口内的重复失败只累加计数,不重复播报 */
const DEDUPE_WINDOW_MS = 8000;

let entries: ApiFailure[] = [];
const listeners = new Set<Listener>();

function emit(): void {
  const snapshot = entries.slice();
  for (const listener of listeners) listener(snapshot);
}

export function reportApiFailure(method: string, path: string, status: number, message: string): void {
  const now = Date.now();
  const key = `${method} ${path} ${status}`;
  const existing = entries.find((entry) => entry.key === key);

  if (existing) {
    existing.count += 1;
    existing.lastAt = now;
    existing.message = message;
  } else if (now - (entries[0]?.lastAt ?? 0) < DEDUPE_WINDOW_MS && entries.some((e) => e.key === key)) {
    // 命中窗口内同名错误(理论上已被上面的 find 覆盖,保留兜底)
    return;
  } else {
    entries = [
      { key, method, path, status, message, count: 1, firstAt: now, lastAt: now },
      ...entries,
    ].slice(0, MAX_ENTRIES);
  }
  emit();
}

export function dismissApiFailure(key: string): void {
  entries = entries.filter((entry) => entry.key !== key);
  emit();
}

export function clearApiFailures(): void {
  entries = [];
  emit();
}

export function subscribeApiFailures(listener: Listener): () => void {
  listeners.add(listener);
  listener(entries.slice());
  return () => {
    listeners.delete(listener);
  };
}
