import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
// 用 vitest/config 的 defineConfig：它在 vite 的配置上多一个 `test` 段，
// 两边共用同一份 alias——测试里 `@/components/...` 与构建时解析到同一个文件
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8080', changeOrigin: true },
      '/hubs': { target: 'http://localhost:8080', changeOrigin: true, ws: true },
    },
  },
  test: {
    // 组件要 mount，需要 DOM。jsdom 够用：这几条断言只看渲染出的文本与结构，
    // 不测布局、不测样式
    environment: 'jsdom',
    include: ['tests/**/*.spec.ts'],
    // 测试文件不放 src/：`vue-tsc -b` 的 include 只有 src，构建不会把测试
    // 一起类型检查（测试由 vitest 用 esbuild 转译，不需要 tsc）
    reporters: ['default'],
    // 全量 Python 测试之后，Windows 上一次拉起多个 fork worker 会间歇性
    // 超时（standalone 能过，CI 顺序下过不了）。单 worker 顺序跑，把这类
    // 环境噪声从测试结果里去掉。
    fileParallelism: false,
    maxWorkers: 1,
  },
})
