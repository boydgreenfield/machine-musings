import { defineConfig } from 'vite';

// `base` is overridden at build time for GitHub Pages (see .github/workflows/deploy.yml).
export default defineConfig({
  base: process.env.VITE_BASE ?? '/',
  build: {
    target: 'es2022',
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
  server: { host: true, port: 5173 },
});
