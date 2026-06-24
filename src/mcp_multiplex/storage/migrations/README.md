# Storage Migrations

Migration files are plain SQL and run in lexical order. Each migration is
recorded in `schema_migrations` with a SHA-256 checksum before the transaction
commits.

The TASK-004 migration establishes core durable tables only. Typed model
validation, audit writing, rollback behavior, and runtime semantics are handled
by later task packets.

