import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';

function serviceWorkerCacheVersion(): Plugin {
  return {
    name: 'caraok-sw-cache-version',
    apply: 'build',
    closeBundle() {
      const dist = resolve('dist');
      const swPath = resolve(dist, 'sw.js');
      const index = readFileSync(resolve(dist, 'index.html'));
      const hash = createHash('sha256').update(index).digest('hex').slice(0, 12);
      const source = readFileSync(swPath, 'utf8');
      const token = 'caraok-shell-__BUILD__';
      if (!source.includes(token)) {
        throw new Error('service worker is missing the build cache token');
      }
      writeFileSync(swPath, source.replaceAll(token, `caraok-shell-${hash}`));
    },
  };
}

export default defineConfig({
  plugins: [react(), serviceWorkerCacheVersion()],
  server: {
    host: '0.0.0.0',
    proxy: {
      '/api': {
        target: process.env.VITE_BACKEND_ORIGIN ?? 'http://127.0.0.1:8000',
      },
      '/healthz': {
        target: process.env.VITE_BACKEND_ORIGIN ?? 'http://127.0.0.1:8000',
      },
    },
  },
});
