// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// cdk-nag (AwsSolutionsChecks, applied in bin/chatbot.ts) suppressions for
// ChatbotStack. Every finding the stack does not fix is suppressed here, on
// the one resource it concerns, with the reason it is acceptable for this
// sample and, where there is one, what a production deployment should do
// instead. Remove a suppression when you make the change it describes; the
// next synth then checks the resource again.

import * as cdk from 'aws-cdk-lib';
import { NagSuppressions } from 'cdk-nag';

// Findings whose text contains the account id or a token are matched by regex.
const re = (pattern: string) => ({ regex: `/${pattern}/g` });

export function addNagSuppressions(stack: cdk.Stack): void {
  const at = (id: string) => `${stack.node.path}/${id}`;
  const suppress = (
    id: string,
    rules: { id: string; reason: string; appliesTo?: (string | { regex: string })[] }[]
  ) => NagSuppressions.addResourceSuppressionsByPath(stack, at(id), rules);

  // --- Networking -----------------------------------------------------------
  suppress('Vpc/Resource', [{
    id: 'AwsSolutions-VPC7',
    reason:
      'No VPC flow logs: the VPC carries only this sample\'s tasks and their ' +
      'egress through one NAT gateway, and flow logs add CloudWatch ingestion ' +
      'cost. Add vpc.addFlowLog() for production.',
  }]);
  suppress('Service/LB/SecurityGroup/Resource', [{
    id: 'AwsSolutions-EC23',
    reason:
      'The ALB accepts HTTP from anywhere because CloudFront (which has no ' +
      'fixed addresses) is its origin client. Requests that skip CloudFront ' +
      'still need a Cognito session for every API route. For production, ' +
      'restrict ingress to the com.amazonaws.global.cloudfront.origin-facing ' +
      'managed prefix list, or use a CloudFront VPC origin.',
  }]);
  suppress('Service/LB/Resource', [{
    id: 'AwsSolutions-ELB2',
    reason:
      'No ALB access logs: they need a dedicated log bucket and add cost; the ' +
      'web task logs every request (uvicorn) to CloudWatch. Enable ' +
      'loadBalancer.logAccessLogs() for production.',
  }]);

  // --- CloudFront -------------------------------------------------------------
  suppress('Cdn/Resource', [
    {
      id: 'AwsSolutions-CFR1',
      reason: 'No geo restriction: the sample serves invited Cognito users wherever they are.',
    },
    {
      id: 'AwsSolutions-CFR2',
      reason:
        'No AWS WAF web ACL: WAF has a monthly charge, and every API route ' +
        'already requires a Cognito session. Attach a web ACL (e.g. the AWS ' +
        'managed common rule set and a rate limit) for production.',
    },
    {
      id: 'AwsSolutions-CFR3',
      reason:
        'No CloudFront access logs: they need a log bucket with ACLs enabled ' +
        'and add cost. Enable standard logging for production.',
    },
    {
      id: 'AwsSolutions-CFR4',
      reason:
        'The distribution uses the default *.cloudfront.net certificate, whose ' +
        'minimum TLS version CloudFront fixes; a custom domain with its own ' +
        'ACM certificate lets you set TLSv1.2_2021.',
    },
    {
      id: 'AwsSolutions-CFR5',
      reason:
        'CloudFront reaches the ALB over HTTP because the ALB has no ' +
        'certificate (no custom domain). Viewers always use HTTPS ' +
        '(REDIRECT_TO_HTTPS). Add an ACM certificate and an HTTPS listener ' +
        'for production.',
    },
  ]);

  // --- Cognito ----------------------------------------------------------------
  suppress('ChatbotUserPool/Resource', [
    {
      id: 'AwsSolutions-COG2',
      reason:
        'MFA is not required for this invite-only sample. Set mfa: ' +
        'Mfa.REQUIRED for production.',
    },
    {
      id: 'AwsSolutions-COG3',
      reason:
        'Cognito threat protection (advanced security) is an extra per-user ' +
        'charge; sign-up is admin-only. Enable it for production.',
    },
  ]);

  // --- Storage ----------------------------------------------------------------
  suppress('TheoryStore/Resource', [{
    id: 'AwsSolutions-S1',
    reason:
      'No S3 server access logs: the bucket is private (block public access, ' +
      'SSL enforced) and read only by the stack\'s own task roles. Add a log ' +
      'bucket for production.',
  }]);
  for (const table of ['TheoriesTable/Resource', 'JobsTable/Resource']) {
    suppress(table, [{
      id: 'AwsSolutions-DDB3',
      reason:
        'No point-in-time recovery: the tables hold this sample\'s theory and ' +
        'job records, which the stack deletes with it (RemovalPolicy.DESTROY). ' +
        'Enable pointInTimeRecovery for production.',
    }]);
  }

  // --- ECS --------------------------------------------------------------------
  suppress('Cluster/Resource', [{
    id: 'AwsSolutions-ECS4',
    reason:
      'Container Insights is off to avoid its CloudWatch metric charges; the ' +
      'tasks log to CloudWatch Logs. Set containerInsights: true for production.',
  }]);
  for (const taskDef of ['Service/TaskDef/Resource', 'BuilderTaskDef/Resource']) {
    suppress(taskDef, [{
      id: 'AwsSolutions-ECS2',
      reason:
        'The environment variables are configuration, not secrets: resource ' +
        'names, ids, the region and flags. The Cognito client secret is read ' +
        'at startup (DescribeUserPoolClient) and the Kiro API key from Secrets ' +
        'Manager, so neither is in the task definition.',
    }]);
  }

  // --- IAM wildcards ------------------------------------------------------------
  const bedrock = {
    id: 'AwsSolutions-IAM5',
    reason:
      'Bedrock InvokeModel on foundation models and inference profiles in any ' +
      'region: cross-region inference profiles route a request to a model in ' +
      'another region, so the model ARN cannot be pinned to one region.',
    appliesTo: [
      'Resource::arn:aws:bedrock:*::foundation-model/*',
      re('^Resource::arn:aws:bedrock:\\*:[0-9]+:inference-profile\\/\\*$'),
    ],
  };
  const bucketObjects = {
    id: 'AwsSolutions-IAM5',
    reason:
      'CDK grantReadWrite on the theory bucket: object-level actions on its ' +
      'objects (<bucket>/*) only. Source documents, theories and build ' +
      'progress are written under ids not known at synth time.',
    appliesTo: [
      'Action::s3:GetObject*', 'Action::s3:GetBucket*', 'Action::s3:List*',
      'Action::s3:DeleteObject*', 'Action::s3:Abort*',
      re('^Resource::<TheoryStore[A-Za-z0-9]+\\.Arn>\\/\\*$'),
    ],
  };
  suppress('Service/TaskDef/TaskRole/DefaultPolicy/Resource', [
    bedrock,
    bucketObjects,
    {
      id: 'AwsSolutions-IAM5',
      reason:
        'ecs:TagResource, StopTask and DescribeTasks on the builder tasks of ' +
        'this cluster only (task/<cluster>/*, plus an ecs:cluster condition): ' +
        'task ids are created per build at run time.',
      appliesTo: [re('^Resource::arn:<AWS::Partition>:ecs:[a-z0-9-]+:[0-9]+:task\\/<Cluster[A-Za-z0-9]+>\\/\\*$')],
    },
  ]);
  suppress('BuilderTaskDef/TaskRole/DefaultPolicy/Resource', [
    bedrock,
    bucketObjects,
    {
      id: 'AwsSolutions-IAM5',
      reason:
        'The trailing -* matches the random suffix Secrets Manager appends to ' +
        'the Kiro API key secret\'s ARN; it covers that one secret name only.',
      appliesTo: [re('^Resource::arn:aws:secretsmanager:[a-z0-9-]+:[0-9]+:secret:.+-\\*$')],
    },
  ]);
  for (const role of ['Service/TaskDef/ExecutionRole/DefaultPolicy/Resource',
                      'BuilderTaskDef/ExecutionRole/DefaultPolicy/Resource']) {
    suppress(role, [{
      id: 'AwsSolutions-IAM5',
      reason:
        'ecr:GetAuthorizationToken supports no resource-level permissions, so ' +
        'CDK grants it on *. The image pull itself is scoped to the asset ' +
        'repository.',
      appliesTo: ['Resource::*'],
    }]);
  }
}
