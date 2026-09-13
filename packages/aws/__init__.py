"""``AwsGateway``: everything IncidentPilot does to/reads from the *monitored*
demo application's AWS resources (its Lambda alias, its results tables, its
CloudWatch log group).

This is deliberately separate from ``packages/storage`` (IncidentPilot's own
control-plane state) and from IAM roles: the investigator only ever holds a
read-only gateway, the executor holds a gateway scoped to one allowlisted
alias update, and fixture mode's gateway touches no AWS API at all -- see
docs/decisions/0006-fixture-live-adapters.md.
"""
