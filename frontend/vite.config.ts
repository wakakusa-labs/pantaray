import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { resolve } from 'path';

const DEFAULT_FRONTEND_PORT = 3001;

export default defineConfig(({ command, mode }) => {
  const isProd = mode === 'production';
  let frontendPort: number | undefined;
  if (command === 'serve') {
    const env = loadEnv(mode, process.cwd(), 'FRONTEND_PORT');
    frontendPort =
      env.FRONTEND_PORT === undefined ? DEFAULT_FRONTEND_PORT : Number(env.FRONTEND_PORT);
    if (!Number.isInteger(frontendPort) || frontendPort < 1 || frontendPort > 65535) {
      throw new Error('FRONTEND_PORT must be a valid TCP port (1-65535).');
    }
  }

  return {
    plugins: [react()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      port: frontendPort,
      strictPort: true,
      // Loopback only, with Vite's default CORS (localhost origins): the dev server serves
      // source and project files, so neither the LAN nor arbitrary web pages may read it.
      host: '127.0.0.1',
    },
    build: {
      outDir: 'dist',
      emptyOutDir: true,
      chunkSizeWarningLimit: 600,
      rollupOptions: {
        input: {
          main: resolve(__dirname, 'index.html'),
          notification: resolve(__dirname, 'notification.html'),
        },
      },
    },
    // Production build: remove console/debugger from renderer bundle
    // - 配布版（file://）で devtools を無効化しても、将来的な露出/誤共有を防ぐためビルド時に落とす
    esbuild: isProd ? { drop: ['console', 'debugger'] } : undefined,
    envPrefix: ['VITE_'],
    // NOTE:
    // - Electron（file://）向けは `./` が必須（相対パスでないと assets が読めず白画面になる）
    // - Web（Firebase等）は通常 `/` を使う
    // - ここでは「環境変数で明示」できるようにし、デフォルトは web 向けの `/` にする
    base: process.env.VITE_BASE || '/',
    appType: 'spa',
    optimizeDeps: {
      esbuildOptions: {
        target: 'es2020',
      },
    },
  };
});
