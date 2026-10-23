import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * Vite 配置
 * - 产物直接给 Tauri 的 webview 加载（file:// 协议），所以 base 必须是 './'
 * - target 固定 chrome105：Windows 上 Tauri 用 WebView2，无需兼容旧浏览器
 * - 关闭 sourcemap / 开启 esbuild 压缩，减小安装包体积
 */
export default defineConfig({
  plugins: [react()],
  base: './',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    host: '127.0.0.1',
  },
  clearScreen: false,
  envPrefix: ['VITE_', 'TAURI_'],
  build: {
    target: 'chrome105',
    minify: 'esbuild',
    sourcemap: false,
    cssMinify: true,
    chunkSizeWarningLimit: 2000,
    rollupOptions: {
      output: {
        // 把不常变的第三方库单独拆包，方便增量更新与缓存
        manualChunks: {
          pixi: ['pixi.js'],
          react: ['react', 'react-dom'],
        },
      },
    },
  },
});
