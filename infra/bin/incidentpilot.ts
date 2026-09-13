#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { IncidentPilotStack } from '../lib/incidentpilot-stack';

const app = new cdk.App();

// Account and region come from the ambient AWS environment on purpose --
// this stack creates billable resources, so it should never silently target
// an account the operator did not choose. Set them explicitly with
// CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION or the usual AWS env vars.
new IncidentPilotStack(app, 'IncidentPilotDemo', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
  description: 'IncidentPilot bounded demo: processor Lambda, two results tables, three scoped roles',
  tags: {
    Project: 'IncidentPilot',
    Purpose: 'hackathon-demo',
  },
});
