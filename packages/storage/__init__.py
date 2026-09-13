"""Durable storage adapters behind one interface (`interface.ControlPlaneStore`).

`sqlite_store.SqliteControlPlaneStore` backs fixture mode and the local-live
demo. `dynamodb_store.DynamoDbControlPlaneStore` backs hosted/live-AWS mode.
Nothing outside this package should import `sqlite3` or `boto3` directly for
control-plane state -- see docs/decisions/0006-fixture-live-adapters.md.
"""
