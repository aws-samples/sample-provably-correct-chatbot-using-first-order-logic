// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as path from 'path';
import { addNagSuppressions } from './nag-suppressions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecsPatterns from 'aws-cdk-lib/aws-ecs-patterns';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';

export interface ChatbotStackProps extends cdk.StackProps {
  /** kiro-cli --model for theory builds; empty uses kiro-cli's default. */
  readonly kiroModel?: string;
  /**
   * Secrets Manager secret holding the Kiro API key (plain string) that
   * builder tasks run kiro-cli with. Created out of band, before the first
   * build (it holds a credential).
   */
  readonly kiroApiKeySecretName?: string;
}

// Prefix for this stack's account-unique names (the Cognito Hosted UI domain)
// and its log streams, so it never collides with another deployment.
const NAME_PREFIX = 'provably-correct-chatbot';
const BUILDER_STREAM_PREFIX = 'chatbot-builder';

export class ChatbotStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: ChatbotStackProps = {}) {
    super(scope, id, props);

    // --- Networking -------------------------------------------------------
    // 2 AZs, public + private-with-egress. Fargate tasks sit in private
    // subnets and reach Bedrock / the internet via NAT.
    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 1,
    });

    // VPC Block Public Access: when the account (or its organization) turns
    // it on, the internet gateway drops all traffic to the public subnets,
    // so CloudFront cannot reach the ALB and every page returns 504. Exempt
    // this stack's public subnets (the ALB and the NAT gateway); the private
    // subnets, where the tasks run, stay under the account setting. An
    // exclusion has no effect while Block Public Access is off.
    vpc.publicSubnets.forEach((subnet, i) => {
      new ec2.CfnVPCBlockPublicAccessExclusion(this, `PublicSubnetBpaExclusion${i + 1}`, {
        subnetId: subnet.subnetId,
        internetGatewayExclusionMode: 'allow-bidirectional',
      });
    });

    const cluster = new ecs.Cluster(this, 'Cluster', { vpc });
    // Every task in this cluster (task ids are unknown at synth time):
    // arn:aws:ecs:<region>:<account>:task/<cluster name>/*
    const clusterTasksArn = cdk.Stack.of(this).formatArn({
      service: 'ecs',
      resource: 'task',
      resourceName: `${cluster.clusterName}/*`,
    });

    // --- Cognito (app-level auth) ----------------------------------------
    // A user pool of its own. The chatbot is private: the
    // FastAPI app enforces a Cognito-authenticated session cookie on every
    // page/API request and redirects unauthenticated users to the Hosted UI.
    //
    // Dependency note: the app client's OAuth callback/logout URLs need the
    // CloudFront domain, which does not exist until the distribution (below)
    // is created. To avoid a resource cycle (service -> distribution ->
    // client -> service), the container env does NOT carry the CloudFront
    // domain -- the app derives its redirect URLs from the forwarded host of
    // the incoming request. The callback/logout URLs are added to the client
    // after the distribution via an escape hatch (distribution -> client).
    const userPool = new cognito.UserPool(this, 'ChatbotUserPool', {
      selfSignUpEnabled: false, // admin-created users only; this is private
      signInAliases: { email: true },
      autoVerify: { email: true },
      passwordPolicy: {
        minLength: 12,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: cdk.RemovalPolicy.DESTROY, // sandbox; recreate freely
    });

    // A unique Hosted UI domain prefix (account+region scoped namespace).
    const userPoolDomain = userPool.addDomain('ChatbotUserPoolDomain', {
      cognitoDomain: {
        domainPrefix: `${NAME_PREFIX}-${this.account}`,
      },
    });

    // Confidential client (has a secret) using the OAuth authorization-code
    // flow. The app exchanges the code server-side with the secret, so tokens
    // never touch the browser; the browser only holds the app's own signed
    // session cookie.
    const userPoolClient = userPool.addClient('ChatbotClient', {
      generateSecret: true,
      authFlows: { userSrp: true },
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.PROFILE,
        ],
        // Real callback/logout URLs are injected after the distribution
        // exists (see escape hatch below). A placeholder keeps synth valid.
        callbackUrls: ['https://example.com/auth/callback'],
        logoutUrls: ['https://example.com/'],
      },
      preventUserExistenceErrors: true,
    });


    // --- Theory store (S3 + DynamoDB) ------------------------------------
    // Source documents and computed theory YAML live in S3; the
    // theory and job indexes live in DynamoDB. The web task reads/writes both
    // via the task role (store.py). This backs tabs 2/3/4 and the theory
    // selector in tab 1.
    const theoryBucket = new s3.Bucket(this, 'TheoryStore', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY, // sandbox
      autoDeleteObjects: true,                  // sandbox
    });

    // Explicit S3 access to the theory bucket, instead of grantReadWrite (which
    // adds s3:Abort*, DeleteObject*, GetBucket* and the other List* actions).
    // s3:ListBucket on the bucket makes a missing object read as NoSuchKey
    // rather than AccessDenied, which store.py and job_introspection.py rely
    // on to tell "not written yet" from an error.
    const theoryBucketAccess = (role: iam.IRole, objectActions: string[]) => {
      role.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: objectActions,
        resources: [theoryBucket.arnForObjects('*')],
      }));
      role.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [theoryBucket.bucketArn],
      }));
    };

    // DynamoDB item access on one table, instead of grantReadWriteData.
    const TABLE_RW = ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:Scan'];
    const tableAccess = (role: iam.IRole, table: dynamodb.ITable, actions: string[]) => {
      role.addToPrincipalPolicy(new iam.PolicyStatement({
        actions,
        resources: [table.tableArn],
      }));
    };

    // Bedrock InvokeModel for the models the app uses (globals.py): Anthropic
    // Claude, through global.* / us.* cross-region inference profiles, and
    // Titan text embeddings. A cross-region profile is authorized on the
    // profile in this region and on the foundation model in whichever region
    // it routes to, including the region-less arn:aws:bedrock:::foundation-
    // model/... of global profiles, so the foundation-model region stays '*'.
    const bedrockInvokeStatement = () => new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: [
        `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.anthropic.claude-*`,
        `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/us.anthropic.claude-*`,
        'arn:aws:bedrock:*::foundation-model/anthropic.claude-*',
        `arn:aws:bedrock:${this.region}::foundation-model/amazon.titan-embed-*`,
      ],
    });

    const theoriesTable = new dynamodb.Table(this, 'TheoriesTable', {
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY, // sandbox
    });

    const jobsTable = new dynamodb.Table(this, 'JobsTable', {
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY, // sandbox
    });


    // --- Container image --------------------------------------------------
    // Build context is the REPO ROOT (one level up): the backend imports
    // auto_formalizer/utils/globals/params from the root, so the whole tree
    // must be in the image. See cdk/docker/Dockerfile.
    const repoRoot = path.join(__dirname, '..', '..');

    const image = ecs.ContainerImage.fromAsset(repoRoot, {
      file: path.join('cdk', 'docker', 'Dockerfile'),
      // ARM64 to match the cvc5 arm64 static binary the Dockerfile installs
      // and Fargate ARM (cheaper). Requires buildx/finch emulation if your
      // host is x86; set CDK_DOCKER=finch on the macOS sandbox.
      platform: cdk.aws_ecr_assets.Platform.LINUX_ARM64,
      exclude: [
        'cdk/node_modules',
        'cdk/cdk.out',
        'frontend/node_modules',
        'frontend/build',
        'runs',
        '**/__pycache__',
        '**/.DS_Store',
        '.git',
      ],
    });

    const logGroup = new logs.LogGroup(this, 'ServiceLogs', {
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // --- Fargate service behind an ALB -----------------------------------
    // 2 vCPU / 8 GB: answer_q_from_chatbot fans out num_jobs=10 subprocesses
    // per question (multiprocessing), so it is CPU/memory hungry.
    const service = new ecsPatterns.ApplicationLoadBalancedFargateService(
      this,
      'Service',
      {
        cluster,
        cpu: 2048,
        memoryLimitMiB: 8192,
        desiredCount: 1,
        // Fail a bad deploy fast (and roll back) instead of letting
        // CloudFormation hang for hours waiting for a crash-looping task.
        circuitBreaker: { rollback: true },
        runtimePlatform: {
          cpuArchitecture: ecs.CpuArchitecture.ARM64,
          operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
        },
        taskImageOptions: {
          image,
          containerPort: 8000,
          logDriver: ecs.LogDrivers.awsLogs({
            streamPrefix: 'chatbot',
            logGroup,
          }),
          environment: {
            // Keep the app off Langfuse/OTel unless the user wires keys later.
            DISABLE_LANGFUSE: '1',
            AWS_REGION: this.region,
            BEDROCK_REGION: this.region,
            // --- Cognito app-level auth ---
            // The app fetches the client SECRET at startup via
            // DescribeUserPoolClient (task role grant below), so it is never
            // embedded in the task definition. The Hosted UI domain and the
            // OAuth code-exchange token endpoint are derived from these.
            // The server refuses to start without the Cognito settings below
            // (auth.init), so a missing one cannot switch the login off.
            AUTH_REQUIRED: '1',
            COGNITO_USER_POOL_ID: userPool.userPoolId,
            COGNITO_CLIENT_ID: userPoolClient.userPoolClientId,
            COGNITO_DOMAIN:
              `${userPoolDomain.domainName}.auth.${this.region}.amazoncognito.com`,
            COGNITO_REGION: this.region,
            // Random per-deploy signing key for the app session cookie would
            // reset sessions on every deploy; a stable one lives in the app's
            // SESSION_SECRET env, defaulted here to a stack-stable token.
            SESSION_SECRET: cdk.Names.uniqueId(this) + this.account,
            // --- Theory store (tabs 2/3/4) ---
            THEORY_STORE_BUCKET: theoryBucket.bucketName,
            THEORIES_TABLE: theoriesTable.tableName,
            JOBS_TABLE: jobsTable.tableName,
            // The job-logs endpoint tails this task's own CloudWatch group
            // (inline jobs log into the running task's stream). store.job_logs
            // reads it; the task role is granted read access below.
            SERVICE_LOG_GROUP: logGroup.logGroupName,
            // Max sentences an inline build (in this web task, only when no
            // builder is configured) formalizes. Builder tasks are uncapped.
            MAX_SENTENCES_INLINE: '300',
          },
        },
        publicLoadBalancer: true,
        // CloudFront terminates TLS; the ALB origin is plain HTTP on :80.
        listenerPort: 80,
      }
    );

    // Health check hits /healthz, which is intentionally UNauthenticated (the
    // ALB has no Cognito session), unlike /subject which is now auth-gated.
    service.targetGroup.configureHealthCheck({
      path: '/healthz',
      healthyHttpCodes: '200',
      interval: cdk.Duration.seconds(30),
      timeout: cdk.Duration.seconds(10),
      healthyThresholdCount: 2,
      unhealthyThresholdCount: 5,
    });
    // Prover fan-out means a single /ask can take many seconds.
    service.targetGroup.setAttribute(
      'deregistration_delay.timeout_seconds',
      '30'
    );

    // --- Bedrock permissions (task role, not static creds) ----------------
    service.taskDefinition.taskRole.addToPrincipalPolicy(
      bedrockInvokeStatement()
    );

    // The app fetches its own Cognito client secret at startup (so the secret
    // is never baked into the task definition).
    service.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['cognito-idp:DescribeUserPoolClient'],
        resources: [userPool.userPoolArn],
      })
    );

    // The web task reads/writes the theory store (documents + computed
    // theories in S3; theory/job indexes in DynamoDB) via store.py. It
    // deletes a theory's two objects when the theory is deleted.
    theoryBucketAccess(service.taskDefinition.taskRole,
                       ['s3:GetObject', 's3:PutObject', 's3:DeleteObject']);
    // DynamoDB: only the calls store.py makes (get/put/update/scan; theory
    // records are also deleted). No Query, Batch*, ConditionCheckItem or
    // DescribeTable; job records are never deleted.
    tableAccess(service.taskDefinition.taskRole, theoriesTable, [...TABLE_RW, 'dynamodb:DeleteItem']);
    tableAccess(service.taskDefinition.taskRole, jobsTable, TABLE_RW);

    // The job-logs endpoint (store.job_logs) tails this task's own CloudWatch
    // log group to return a job's full pipeline output, so the task role needs
    // read access to that group. The ECS awslogs driver already grants the
    // EXECUTION role write (CreateLogStream/PutLogEvents); this is a separate
    // read grant on the TASK role for the app code.
    logGroup.grant(
      service.taskDefinition.taskRole,
      'logs:FilterLogEvents',
      'logs:GetLogEvents',
      'logs:DescribeLogStreams',
    );

    // --- Fargate builder task (one task per theory build) -----------------
    // Every theory build runs in its OWN ephemeral Fargate task running
    // cdk/docker/build_task.py, instead of in the web task. This task-def has
    // NO service -- the web task launches a task on demand via ecs:RunTask and
    // the task exits when the build finishes. Each build gets its own task,
    // launched immediately. Same ARM64 image as the web task; only the command
    // (set as a RunTask container override) differs.
    const builderLogGroup = new logs.LogGroup(this, 'BuilderLogs', {
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const kiroApiKeySecretName =
      props.kiroApiKeySecretName ?? `${NAME_PREFIX}/kiro-api-key`;

    const builderTaskDef = new ecs.FargateTaskDefinition(this, 'BuilderTaskDef', {
      // A build runs one kiro-cli session (~0.7 GB) at a time, plus the
      // parallel work around it: test-set generation (qa_pool_size threads),
      // the agent's batched cvc5 checks (one cvc5 per vCPU) and the harness's
      // re-verification (verify_pool_size cvc5 runs + Bedrock calls). The
      // vCPUs are what those cvc5 batches use. Memory is generous for a large
      // document's theory and solver state; size it down from measured runs.
      cpu: 8192,
      memoryLimitMiB: 49152,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    const builderContainerName = 'builder';
    builderTaskDef.addContainer(builderContainerName, {
      image, // same asset as the web task
      // Default command; the web task overrides it per build via RunTask, but a
      // sensible default makes a manual `aws ecs run-task` work too.
      command: ['python', 'build_task.py'],
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: BUILDER_STREAM_PREFIX,
        logGroup: builderLogGroup,
      }),
      environment: {
        // BUILDER_MODE=1 tells store.run_job it is the dedicated builder and
        // may build the whole document (no MAX_SENTENCES_INLINE cap, and real
        // per-sentence decontextualization rather than the inline
        // standalone==original shortcut).
        BUILDER_MODE: '1',
        DISABLE_LANGFUSE: '1',
        AWS_REGION: this.region,
        BEDROCK_REGION: this.region,
        // Same theory store as the web task -- the builder reads the source doc
        // from S3 and writes the computed theory back, flipping the DynamoDB
        // records to ready/finished.
        THEORY_STORE_BUCKET: theoryBucket.bucketName,
        THEORIES_TABLE: theoriesTable.tableName,
        JOBS_TABLE: jobsTable.tableName,
        SERVICE_LOG_GROUP: builderLogGroup.logGroupName,
        // The build runs a sandboxed kiro-cli session that reads its API key
        // from KIRO_API_KEY_SECRET_ID at runtime.
        CHATBOT_KIRO_MODEL: props.kiroModel ?? '',
        KIRO_API_KEY_SECRET_ID: kiroApiKeySecretName,
      },
    });

    // Builder task role needs the same data-plane grants the web task has:
    // Bedrock invoke, the theory store (S3 + both tables), and write access to
    // its own log group (the awslogs driver grants the EXECUTION role; this is
    // the TASK role).
    builderTaskDef.taskRole.addToPrincipalPolicy(bedrockInvokeStatement());
    // The builder reads the source document and writes the theory and its
    // build progress; it never deletes.
    theoryBucketAccess(builderTaskDef.taskRole, ['s3:GetObject', 's3:PutObject']);
    // The builder updates its job and theory records and never deletes one.
    tableAccess(builderTaskDef.taskRole, theoriesTable, TABLE_RW);
    tableAccess(builderTaskDef.taskRole, jobsTable, TABLE_RW);
    // Read the Kiro API key for kiro-cli. Secrets Manager
    // appends a random 6-char suffix to secret ARNs, hence the wildcard. The
    // secret is created out of band (it holds a credential), so it is
    // referenced by name rather than defined in this stack.
    builderTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['secretsmanager:GetSecretValue'],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:${kiroApiKeySecretName}-*`,
        ],
      })
    );

    // Let the WEB task launch and stop builder tasks, and pass the builder's
    // task + execution roles to ECS when launching (iam:PassRole is required
    // for RunTask with a task-def that has roles).
    service.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['ecs:RunTask'],
        resources: [builderTaskDef.taskDefinitionArn],
        // Only in this stack's cluster, like the other ECS grants.
        conditions: {
          ArnEquals: { 'ecs:cluster': cluster.clusterArn },
        },
      })
    );
    service.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        // _dispatch_build passes tags= on run_task (the job and theory ids), which requires ecs:TagResource on the launched
        // task. Without it RunTask fails with AccessDeniedException and the web
        // task silently falls back to the inline runner. Task ARNs are unknown
        // at synth time, so scope to the task wildcard in this cluster.
        actions: ['ecs:TagResource'],
        resources: [clusterTasksArn],
        conditions: {
          ArnEquals: { 'ecs:cluster': cluster.clusterArn },
          // Only tags applied by RunTask itself: it cannot retag existing tasks.
          StringEquals: { 'ecs:CreateAction': 'RunTask' },
        },
      })
    );
    service.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        // StopTask (kill-a-job) only on builder tasks: every one is launched
        // with the chatbot:job tag (store._dispatch_build) and the web
        // service's own tasks are not, so the web task cannot stop itself or
        // other untagged tasks in the cluster.
        actions: ['ecs:StopTask'],
        resources: [clusterTasksArn],
        conditions: {
          ArnEquals: { 'ecs:cluster': cluster.clusterArn },
          Null: { 'aws:ResourceTag/chatbot:job': 'false' },
        },
      })
    );
    service.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        // DescribeTasks (read-only reconciliation of builder jobs) stays on
        // the cluster's tasks: describe actions take no tag conditions.
        actions: ['ecs:DescribeTasks'],
        resources: [clusterTasksArn],
        conditions: {
          ArnEquals: { 'ecs:cluster': cluster.clusterArn },
        },
      })
    );
    service.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['iam:PassRole'],
        resources: [
          builderTaskDef.taskRole.roleArn,
          builderTaskDef.executionRole!.roleArn,
        ],
        // Only to ECS, for RunTask: the web task cannot hand the builder's
        // roles (and their S3, DynamoDB and Secrets Manager access) to any
        // other service.
        conditions: {
          StringEquals: { 'iam:PassedToService': 'ecs-tasks.amazonaws.com' },
        },
      })
    );

    // Builder tasks run in the same private subnets as the web task, reusing
    // the service's security group (egress to Bedrock/S3/DynamoDB via NAT).
    const builderSubnets = vpc.selectSubnets({
      subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
    });
    const builderSecurityGroup = service.service.connections.securityGroups[0];

    // Tell the web task HOW to launch builder tasks (store._dispatch_build
    // reads these). Added to the running container's env via an escape hatch so
    // it does not disturb the taskImageOptions.environment block above.
    const webContainer = service.taskDefinition.defaultContainer!;
    // An ECS health check on the web container, as well as the ALB's: ECS
    // replaces a task whose server has hung even if the ALB has not yet
    // drained it. The pattern's taskImageOptions has no healthCheck, hence
    // the override (the web container is the task definition's only one).
    (service.taskDefinition.node.defaultChild as ecs.CfnTaskDefinition).addPropertyOverride(
      'ContainerDefinitions.0.HealthCheck', {
        Command: ['CMD-SHELL', 'curl -fsS http://localhost:8000/healthz || exit 1'],
        Interval: 30,
        Timeout: 5,
        Retries: 3,
        StartPeriod: 60,
      });
    webContainer.addEnvironment('BUILDER_TASK_DEF', builderTaskDef.taskDefinitionArn);
    webContainer.addEnvironment('BUILDER_CLUSTER', cluster.clusterArn);
    webContainer.addEnvironment('BUILDER_SUBNETS', builderSubnets.subnetIds.join(','));
    webContainer.addEnvironment('BUILDER_SECURITY_GROUPS', builderSecurityGroup.securityGroupId);
    webContainer.addEnvironment('BUILDER_CONTAINER_NAME', builderContainerName);
    // Job Introspection reads a build's log out of its builder task's stream
    // (store.build_logs), so the web task needs read access to the builder
    // log group as well as its own, and the stream prefix to find it.
    webContainer.addEnvironment('BUILDER_LOG_GROUP', builderLogGroup.logGroupName);
    webContainer.addEnvironment('BUILDER_LOG_STREAM_PREFIX', BUILDER_STREAM_PREFIX);
    // The Build parameters dialog's defaults are read in the WEB task
    // (build_params.specs -> params.Params), so give it the builder's model
    // default too.
    webContainer.addEnvironment('CHATBOT_KIRO_MODEL', props.kiroModel ?? '');
    builderLogGroup.grant(
      service.taskDefinition.taskRole,
      'logs:FilterLogEvents',
      'logs:GetLogEvents',
      'logs:DescribeLogStreams',
    );

    // --- CloudFront -------------------------------------------------------
    // Default origin = the ALB (serves both the SPA and the API). API paths
    // bypass the cache; everything else (the static SPA) is cacheable.
    const albOrigin = new origins.LoadBalancerV2Origin(service.loadBalancer, {
      protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
      httpPort: 80,
      // Default origin-response timeout is 30s. The /api/jobs/{id}/logs tail
      // can take longer than that for a long/busy job (CloudWatch round-trips),
      // which surfaced as a CloudFront 504. 60s is the max for a standard
      // distribution and gives the slow API calls headroom.
      readTimeout: cdk.Duration.seconds(60),
    });

    const apiBehavior: cloudfront.BehaviorOptions = {
      origin: albOrigin,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
    };

    const distribution = new cloudfront.Distribution(this, 'Cdn', {
      comment: 'Provably-correct chatbot',
      // The SPA root ('/' and client-routed paths) returns auth-dependent
      // HTML now (the app redirects to the Hosted UI when there is no valid
      // session cookie), so the default behavior must forward the session
      // cookie to the origin and must NOT cache. Hashed static assets are
      // cached separately via the '/static/*' behavior below.
      defaultBehavior: {
        origin: albOrigin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
      },
      additionalBehaviors: {
        // CRA emits content-hashed files under /static/, safe to cache hard.
        '/static/*': {
          origin: albOrigin,
          viewerProtocolPolicy:
            cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
          cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        },
        // The chatbot endpoints the frontend calls (api.ts): /ask,
        // /ask-multi (Super Chat), /subject, /lucky, /lucky-questions. All go
        // to the ALB, uncached, full passthrough. Each needs its own entry:
        // the default behavior allows only GET/HEAD, so a POST to a path
        // missing here is refused by CloudFront.
        '/ask': apiBehavior,
        '/ask-multi': apiBehavior,
        '/subject': apiBehavior,
        '/lucky': apiBehavior,
        '/lucky-questions': apiBehavior,
        '/api/*': apiBehavior,
        // OAuth login / callback / logout: must reach the ALB uncached with
        // cookies + query string forwarded (ALL_VIEWER does both).
        '/auth/*': apiBehavior,
      },
    });

    // Inject the real OAuth callback/logout URLs now that the CloudFront
    // domain exists. Done via escape hatch so the dependency runs
    // distribution -> client (not client -> distribution), avoiding a cycle
    // with the service's task-def env.
    const cfnClient = userPoolClient.node.defaultChild as cognito.CfnUserPoolClient;
    cfnClient.callbackUrLs = [
      `https://${distribution.distributionDomainName}/auth/callback`,
    ];
    cfnClient.logoutUrLs = [
      `https://${distribution.distributionDomainName}/`,
    ];

    // --- Outputs ----------------------------------------------------------
    new cdk.CfnOutput(this, 'PublicUrl', {
      value: `https://${distribution.distributionDomainName}`,
      description: 'Public chatbot URL (CloudFront)',
    });
    new cdk.CfnOutput(this, 'AlbDnsName', {
      value: service.loadBalancer.loadBalancerDnsName,
      description: 'Internal ALB DNS (origin; not for direct use)',
    });
    new cdk.CfnOutput(this, 'ServiceLogGroup', {
      value: logGroup.logGroupName,
    });
    new cdk.CfnOutput(this, 'UserPoolId', {
      value: userPool.userPoolId,
      description: 'Cognito user pool (create users here to grant access)',
    });
    new cdk.CfnOutput(this, 'UserPoolClientId', {
      value: userPoolClient.userPoolClientId,
      description: 'Cognito app client id',
    });
    new cdk.CfnOutput(this, 'CognitoHostedUiDomain', {
      value:
        `${userPoolDomain.domainName}.auth.${this.region}.amazoncognito.com`,
      description: 'Cognito Hosted UI domain',
    });

    // cdk-nag: the findings this sample accepts, each with its reason.
    addNagSuppressions(this);
  }
}
