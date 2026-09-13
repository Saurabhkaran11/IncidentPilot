import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import * as path from 'path';

/**
 * The bounded demo environment: one processor Lambda, two results tables,
 * one alias, and three deliberately-separate runtime identities.
 *
 * The interesting part of this stack is what each role *cannot* do. The
 * whole product rests on those boundaries being real IAM, not application
 * logic (docs/decisions/0004-separate-executor-identity.md):
 *
 *   processor    - writes ONLY to the expected results table. It is given
 *                  no access to the quarantine table, which is what makes
 *                  the demo's AccessDenied failure genuine rather than
 *                  simulated.
 *   investigator - reads logs, alias/function config, and results. It can
 *                  mutate nothing and cannot assume the executor role.
 *   executor     - updates exactly ONE alias and invokes exactly that
 *                  alias. No IAM actions, no other function, no wildcard.
 *
 * Deliberately absent: NAT gateway, VPC, always-on database, container
 * cluster. None is needed for this workload and each would dominate the
 * cost of a demo (build brief section 13).
 */
export interface IncidentPilotStackProps extends cdk.StackProps {
  /** Days to retain demo logs. Kept short: this is a demo, not an audit trail. */
  readonly logRetentionDays?: logs.RetentionDays;
}

export class IncidentPilotStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: IncidentPilotStackProps = {}) {
    super(scope, id, props);

    const retention = props.logRetentionDays ?? logs.RetentionDays.THREE_DAYS;

    // ---------------------------------------------------------------
    // Results tables
    // ---------------------------------------------------------------

    // The table the processor is supposed to write to. `request_id` is the
    // partition key because the business effect is a conditional insert
    // keyed by request ID -- that is what makes replay idempotent.
    const resultsTable = new dynamodb.Table(this, 'ResultsTable', {
      tableName: 'incidentpilot-demo-results',
      partitionKey: { name: 'request_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // A real table that really exists and that the processor role really
    // cannot touch. Version B points at this one. The failure under
    // investigation must be an authentic AccessDenied from AWS, not an
    // error we throw on purpose.
    const quarantineTable = new dynamodb.Table(this, 'QuarantineResultsTable', {
      tableName: 'incidentpilot-demo-results-quarantine',
      partitionKey: { name: 'request_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ---------------------------------------------------------------
    // Processor function + alias
    // ---------------------------------------------------------------

    const processorRole = new iam.Role(this, 'ProcessorRole', {
      roleName: 'incidentpilot-demo-processor-role',
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Demo document processor. Writes only to the expected results table.',
    });
    processorRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:/aws/lambda/incidentpilot-demo-processor:*`],
      }),
    );
    // Exactly the two actions demo/workload/processor.py performs: a
    // conditional PutItem and the GetItem that follows a condition failure.
    // `grantReadWriteData` would also hand over DeleteItem, Scan, Query and
    // UpdateItem, none of which this function calls (build brief section 13:
    // minimize actions).
    //
    // Note the absence of quarantineTable here. This single omission is the
    // fault the whole demo investigates.
    processorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'WriteOnlyToTheExpectedResultsTable',
        actions: ['dynamodb:PutItem', 'dynamodb:GetItem'],
        resources: [resultsTable.tableArn],
      }),
    );

    const logGroup = new logs.LogGroup(this, 'ProcessorLogGroup', {
      logGroupName: '/aws/lambda/incidentpilot-demo-processor',
      retention: retention,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const processor = new lambda.Function(this, 'Processor', {
      functionName: 'incidentpilot-demo-processor',
      runtime: lambda.Runtime.PYTHON_3_12,
      // The deployed code is demo/workload/processor.py verbatim -- the same
      // module fixture mode runs in-process. There is no separate "real"
      // implementation to drift from the tested one.
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'demo', 'workload')),
      handler: 'processor.lambda_handler',
      role: processorRole,
      logGroup: logGroup,
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      environment: {
        // Version G's configuration. scripts/deploy_demo_workload.py
        // publishes this as the known-good version, then republishes with
        // RESULTS_TABLE pointing at the quarantine table to create B.
        RESULTS_TABLE: resultsTable.tableName,
        SCHEMA_VERSION: '1.0',
      },
    });

    const alias = new lambda.Alias(this, 'LiveAlias', {
      aliasName: 'live',
      version: processor.currentVersion,
    });

    // ---------------------------------------------------------------
    // Investigation runtime -- read-only, no path to a mutation
    // ---------------------------------------------------------------

    const investigatorRole = new iam.Role(this, 'InvestigatorRole', {
      roleName: 'incidentpilot-investigator-role',
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Strands investigator + MCP evidence tools. Read-only on registered resources.',
    });
    investigatorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ReadRegisteredLogGroup',
        actions: ['logs:FilterLogEvents', 'logs:GetLogEvents', 'logs:DescribeLogStreams'],
        resources: [logGroup.logGroupArn, `${logGroup.logGroupArn}:*`],
      }),
    );
    investigatorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ReadRegisteredFunctionConfiguration',
        actions: ['lambda:GetAlias', 'lambda:GetFunctionConfiguration', 'lambda:ListVersionsByFunction'],
        resources: [processor.functionArn, `${processor.functionArn}:*`],
      }),
    );
    // Read-only on results so the investigator can observe request status.
    // GetItem only -- the evidence tools look up specific request IDs and
    // never scan the table.
    investigatorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ReadIndividualResultRecords',
        actions: ['dynamodb:GetItem'],
        resources: [resultsTable.tableArn],
      }),
    );
    investigatorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeConfiguredBedrockModel',
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        // Bedrock model access is account/region specific; narrow this to
        // the exact model ID you enabled rather than leaving it broad.
        resources: [`arn:aws:bedrock:${this.region}::foundation-model/*`],
      }),
    );

    // ---------------------------------------------------------------
    // Recovery executor -- one alias, one canary, nothing else
    // ---------------------------------------------------------------

    const executorRole = new iam.Role(this, 'ExecutorRole', {
      roleName: 'incidentpilot-executor-role',
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Applies the approved rollback. Updates exactly one alias; no IAM actions.',
    });
    executorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'UpdateOnlyTheRegisteredDemoAlias',
        actions: ['lambda:UpdateAlias', 'lambda:GetAlias'],
        // Resource-scoped to this one alias. Not the function, not a
        // wildcard: the executor cannot move any other alias in the account.
        resources: [alias.functionArn],
      }),
    );
    executorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeCanaryAndReplayThroughTheAliasOnly',
        actions: ['lambda:InvokeFunction'],
        resources: [alias.functionArn],
      }),
    );
    executorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ReadBackResultsToVerifyRecovery',
        // Verification must read the durable result itself; a 200 from
        // Invoke is not recovery (build brief section 10).
        actions: ['dynamodb:GetItem'],
        resources: [resultsTable.tableArn],
      }),
    );

    // ---------------------------------------------------------------
    // Outputs -- consumed by scripts/deploy_demo_workload.py to write the
    // trusted manifest in demo/manifests/.
    // ---------------------------------------------------------------

    new cdk.CfnOutput(this, 'FunctionName', { value: processor.functionName });
    new cdk.CfnOutput(this, 'AliasName', { value: alias.aliasName });
    new cdk.CfnOutput(this, 'LogGroupName', { value: logGroup.logGroupName });
    new cdk.CfnOutput(this, 'ExpectedResultsTable', { value: resultsTable.tableName });
    new cdk.CfnOutput(this, 'ForbiddenResultsTable', { value: quarantineTable.tableName });
    new cdk.CfnOutput(this, 'ProcessorRoleArn', { value: processorRole.roleArn });
    new cdk.CfnOutput(this, 'InvestigatorRoleArn', { value: investigatorRole.roleArn });
    new cdk.CfnOutput(this, 'ExecutorRoleArn', { value: executorRole.roleArn });
  }
}
