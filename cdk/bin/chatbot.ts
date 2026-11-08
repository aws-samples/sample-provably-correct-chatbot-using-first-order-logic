#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { ChatbotStack } from '../lib/chatbot-stack';

const app = new cdk.App();

// Region must have Bedrock access to the model IDs in globals.py
// (Claude Opus 4.5 / Haiku 4.5). us-east-1 is the default used by the project.
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
};

// A stack of its own: its own VPC, Cognito user pool, theory store, builder
// task and CloudFront distribution, with no resource names shared with any
// other deployment in the account.
new ChatbotStack(app, 'ProvablyCorrectChatbotStack', {
  env,
  description:
    'Provably-correct chatbot (FastAPI + React + kiro-cli builder) on Fargate behind ALB + CloudFront',
  // kiro-cli --model for theory builds; empty uses kiro-cli's default.
  kiroModel: app.node.tryGetContext('kiroModel') ?? '',
  // Secrets Manager secret holding the Kiro API key the builder uses.
  kiroApiKeySecretName:
    app.node.tryGetContext('kiroApiKeySecretName') ?? 'provably-correct-chatbot/kiro-api-key',
});
