// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// The Bedrock models the app invokes, and nothing else; the task roles may
// call only these. Keep in step with globals.py and utils.py:
// tests/test_bedrock_model_ids.py fails when they drift apart.

// Cross-region inference profiles: globals.MODEL_ID (also
// FORMALIZATION_MODEL_ID) and the content-filter fallback in utils.py.
export const BEDROCK_INFERENCE_PROFILE_IDS = [
  'global.anthropic.claude-sonnet-4-5-20250929-v1:0',
  'us.anthropic.claude-3-7-sonnet-20250219-v1:0',
];

// Models called directly in the stack's region: globals.EMBEDDING_MODEL_ID.
export const BEDROCK_FOUNDATION_MODEL_IDS = [
  'amazon.titan-embed-text-v1',
];

/** The foundation model behind a cross-region inference profile id. */
export const profileModel = (profileId: string) =>
  profileId.replace(/^(global|us)\./, '');
