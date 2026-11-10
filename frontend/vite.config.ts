// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    // cdk/docker/server.py serves frontend/build and mounts /static from it,
    // and CloudFront caches '/static/*' hard, so keep that layout.
    outDir: 'build',
    assetsDir: 'static',
  },
  server: {
    port: 5173,
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['src/setupTests.ts'],
    // App.css is read by sentencesTableCss.test.ts; other CSS imports are
    // not needed in tests.
    css: false,
  },
});
