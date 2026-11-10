// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/// <reference types="vite/client" />

interface ImportMetaEnv {
  // API origin; "" in the deployed image (same origin behind CloudFront).
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
