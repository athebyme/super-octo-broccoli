import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  plugins: [vue()],
  publicDir: false,
  build: {
    emptyOutDir: false,
    outDir: fileURLToPath(new URL('../static/workspace', import.meta.url)),
    sourcemap: false,
    lib: {
      entry: fileURLToPath(new URL('./src/index.js', import.meta.url)),
      name: 'SellerHubMyProductsImport',
      formats: ['iife'],
      fileName: () => 'my-products-import.iife.js',
    },
    rollupOptions: {
      external: ['vue'],
      output: {
        globals: { vue: 'Vue' },
        assetFileNames: 'my-products-import.[ext]',
      },
    },
  },
});
