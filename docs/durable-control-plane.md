# Durable Control-Plane Foundation

This document describes the F2 durable foundation and its HTTP cutover slices.
It adds a PostgreSQL authority model, checksum-verified migrations, fenced
transactions, a transactional outbox, a durable NATS JetStream event stream, a
`videosim.worker/v2` lease/report path, direct PostgreSQL monitor-alarm/operator
reads, and selective durable HTTP security auditing. It does **not** claim that
event consumers have completed their cutover.

## Evidence status

Confirmed in this unit:

- `migrations/001_durable_control_plane.sql` creates tenant, feed, worker,
  lease, report/result, current-state, alarm/event, audit, and outbox records.
- `videosim.migrations.PostgresMigrator` serializes migration application with
  a PostgreSQL advisory transaction lock and rejects a changed name/checksum
  for an applied version.
- `videosim.postgres_store.PostgresControlPlaneStore` uses a bounded connection
  pool and implements durable feed versions, worker incarnations, expiring
  offered/acknowledged leases with monotonic epochs, idempotent fenced
  reports/results, centralized alarm transitions, immutable audit/outbox IDs,
  consumer-inbox keys, and `SKIP LOCKED` outbox claims.
- Migration `004_immutable_audit_outbox.sql` makes `audit_events` append-only
  (including truncate) and prevents outbox identity/content mutation while
  leaving only delivery-state fields available to the publisher. Migration
  `005_feed_generation_fence.sql` retains a feed generation across physical
  deletion. Together they grant explicit non-owner service roles and remove
  ambient `PUBLIC` authority on control-plane tables/sequences.
- Lease and heartbeat freshness decisions use PostgreSQL time. New or changed
  authority is `offered` until the matching worker incarnation explicitly
  acknowledges its epoch/config tuple; stale workers cannot offer, acknowledge,
  renew, or ingest. A restart, owner/config change, or expiry advances the epoch.
  A one-second real-time HTTP integration test keeps one survivor heartbeating,
  lets the failed worker and lease expire, preserves the survivor epoch, advances
  the replacement epoch, and rejects the stale report.
- A report can mutate current state only when tenant, worker ID, worker
  incarnation, lease epoch, feed config version, expiry, and sequence all pass
  in one transaction. Observation time is bounded against database time and
  cannot move a check backward. Duplicate report IDs return the stored
  disposition; identical result IDs are reported as already processed, while
  payload drift on either immutable ID is rejected.
- Accepted result and alarm/audit events enter the outbox in the same database
  transaction. `videosim.nats_publisher` publishes them with `Nats-Msg-Id` and
  stores the JetStream acknowledgement sequence. Failed claims use bounded
  exponential retry and become `dead` after ten attempts.
- The production Compose overlay provisions PostgreSQL 17 and NATS 2.11 with
  persistent volumes and required credentials. A pre-migration role initializer,
  migration service, and post-migration role-grant service gate app, publisher,
  and pruner startup; each runtime service uses a distinct non-owner role.
- SQLite feed definitions have an idempotent cutover command.
- Backup, restore, restored-lease expiry fencing, explicit broker-recovery
  mode, and snapshot-consistent semantic comparison tools exist under `scripts/`.

Remaining work and evidence limits:

- PostgreSQL-backed deployments negotiate `videosim.worker/v2`: durable worker
  incarnation, offered/acknowledged lease epoch/config, per-lease epoch/config
  sequence, immutable report ID, bounded observation time, deterministic result
  IDs, and bounded catalog monitor observations. SQLite trusted-lab deployments
  retain worker v1 compatibility.
- Migration `003_direct_monitor_projection.sql` adds durable pending-alert
  state, alarm repeat timestamps, and an alarm-event read/retention index. In
  PostgreSQL mode, accepted catalog observations update pending/current alarms,
  immutable alarm edges, and outbox records in the same fenced transaction;
  `/state.json` reads that PostgreSQL projection rather than a local JSON file
  in one repeatable-read database snapshot.
- Durable audit scope is intentionally selective: PostgreSQL records proxy,
  worker, and operator authentication/authorization denials best-effort, and
  records successful persistent feed/profile mutations atomically with their
  outbox row. A failed durable mutation audit returns HTTP 503 and commits
  neither mutation nor audit. Allowed reads/workers and local runtime
  start/stop/validate actions remain stdout-audited; a denial-store failure
  preserves the denial and emits an explicit stdout audit-gap event.
- No JetStream consumer is deployed. The direct PostgreSQL projection is the
  current operator read model; an inbox-deduplicated consumer/replay parity
  path remains required before event-driven projection can be claimed.
- PostgreSQL and NATS are single instances in Compose. There is no HA, PITR,
  multi-zone, or production RPO/RTO evidence. The database controls are
  tamper-resistant against the normal runtime roles, not a WORM/external audit
  archive and not protection against a PostgreSQL owner/superuser.
- NATS credentials protect the private Compose network, but cross-VM broker TLS
  is deferred to the F4 deployment unit. Do not publish ports 4222 or 5432.
- No 1,000-, 5,000-, or 10,000-stream capacity claim follows from these
  transaction-level integration tests.

## Authority and transaction model

PostgreSQL is authoritative. JetStream transports committed events and is not
an alternative source of truth.

1. A feed row owns a monotonic `config_version`.
2. A worker row owns its current UUID `incarnation_id`.
3. A stream has at most one current lease row. Ownership/config/incarnation or
   expiry changes advance its monotonic `epoch`; a changed lease resets its
   per-epoch result sequence to zero, is offered, and must be acknowledged
   before it can authorize a report. Same-epoch renewal preserves sequence.
4. Report ingestion first reserves an immutable UUID `report_id` and payload
   hash.
5. Each result is checked against the current locked lease and its last
   accepted sequence.
6. Accepted immutable results update `current_check_state`. Catalog monitor
   observations additionally update server-profiled pending/current alarms;
   only conclusive healthy evidence clears, while
   unknown/stale/skipped/error/timeout and an omitted monitor preserve active
   and pending state. The PostgreSQL clock owns alert delay and repeat cadence.
7. Alarm edges (`raised`, repeat `active`, `cleared`, or explicit profile
   `suppressed`) append immutable `alarm_events`; per-stream history is bounded
   to 1,000 events and seven days by default without deleting immutable outbox
   records. The production `monitor-history-pruner` runs a bounded global age
   sweep hourly so stopped streams also expire; invoke
   `python3 -m videosim monitor-history-prune --once` for an inspected manual
   sweep.
8. Events are appended to `outbox` before the transaction commits.
9. Publishers claim due rows with `FOR UPDATE SKIP LOCKED`, publish with the
   outbox `event_id` as `Nats-Msg-Id`, and persist the broker sequence.

A stale or mismatched result is recorded only in the report disposition. It
cannot update result, current-state, alarm/event, or outbox rows.

## Worker API v2 transition

When `VIDEOSIM_DATABASE_URL` selects PostgreSQL, worker endpoints require v2:

1. One worker process generates a UUID incarnation and sends it on assignment,
   heartbeat, acknowledgement, and report calls. A different incarnation cannot
   take over the same worker ID while the current one is heartbeat-fresh; crash
   replacement waits for freshness expiry, preventing old-process ABA revival.
2. Assignment reads use DB-fresh worker membership and deterministic balanced
   placement. Fresh offered/active lease owners are retained up to each
   worker's current total/protocol target; loss moves unavailable ownership,
   while joins and capacity changes still rebalance. The scheduler revokes
   dropped leases, reconciles the worker's complete stream set, and returns each
   stream's epoch, config version, and expiry. Membership/owner reads and all
   lease changes share one tenant-scoped PostgreSQL advisory-lock transaction;
   connection loss rolls it back before another scheduler transaction can lead.
3. The worker acknowledges the exact lease tuple before probing. Authenticated
   lease arrays validate completely and commit atomically in one transaction;
   independent heartbeats extend only active leases for the same incarnation.
4. Each lease tuple maintains its own positive sequence, resetting to one when
   epoch/config changes and advancing only while that tuple persists. The worker
   also sends an immutable batch report UUID. The server deterministically
   derives result UUIDs for scoped `probe.*` metrics and catalog monitor
   observations, maps statuses explicitly, and lets PostgreSQL revalidate every
   fence.
5. PostgreSQL commits accepted results, current check state, pending/current
   monitor alarms, alarm edges, and outbox atomically. A duplicate report
   returns its stored disposition and cannot rerun projection; stale,
   reassigned, expired, or config-revoked reports mutate none of those rows.
   `/state.json` directly queries the durable projection, so a JSON shadow write
   cannot leave the PostgreSQL operator view behind committed results.
6. Monitor observations are expand-compatible: deploy workers that emit them
   before switching an environment to direct PostgreSQL operator reads. Missing
   observations preserve existing active/pending alarms; they never imply a
   healthy clear. A fence rejection returns 409 and causes a bounded assignment
   refetch.

SQLite keeps `videosim.worker/v1` and its file-backed monitor JSON for
trusted-lab compatibility. V1 is not a production fallback and cannot submit
to the durable result path. PostgreSQL mode never falls back to that file when
a direct projection read fails; it reports a disconnected monitor view instead.

## Migrations

Install production dependencies and apply migrations before starting an app:

```sh
python3 -m pip install -r requirements.txt
export VIDEOSIM_DATABASE_URL='postgresql://videosim:...@db:5432/videosim'
python3 -m videosim migrate
python3 -m videosim migration-status
```

Migration files are forward-only in normal operation. The matching `.down.sql`
is destructive and exists for transactional reversal testing, not routine
production rollback. Never edit an applied migration; add the next numbered
migration. The migrator rejects unknown database versions and checksum/name
changes.

## Durable HTTP audit and runtime database roles

In PostgreSQL mode, the HTTP boundary writes a durable audit event for denied
proxy, worker, and operator authentication/authorization attempts. The record
uses the parsed path only (not a query string), retains an authenticated operator
or worker principal when one was established, bounds trusted subjects to the
512-character durable-audit limit, and enters `outbox` in the same transaction.
Audit persistence is deliberately best-effort for a denial: a
storage/outbox outage cannot turn a denied request into an allowed one, and the
stdout audit record explicitly reports the gap.

For successful persistent operator actions, feed create/update/delete,
external-feed expectation updates, and alert-profile updates use one transaction
for the feed configuration, immutable audit row, and audit outbox event. The
handler returns HTTP 503 before changing in-memory configuration or starting,
stopping, or deleting a local process if that transaction fails. A generated-feed
configuration change is committed before its local restart; durable deletion is
committed before best-effort detached-process cleanup. PostgreSQL mutations use
the loaded feed `config_version` as an expected-version fence, so a stale process
cannot recreate a feed deleted by another process. This boundary does not make
transient runtime start/stop/validate behavior durable.

PostgreSQL-backed API processes do not preload the durable feed catalog into
`GuiState` during startup. Operator reads and external-feed assignment use the
bounded/direct database paths instead. New durable feeds use UUID-backed IDs;
the existing expected-version-zero insert and primary key reject a forced
collision without overwriting the committed row. SQLite keeps sequential IDs
and startup reloads for the single-process lab path.

`postgres-role-init` runs `scripts/postgres-runtime-role.sh prepare` before
migrations. It resets `videosim_app`, `videosim_publisher`, and
`videosim_pruner` to non-owner/no-membership roles, reassigns any accidental
ownership, removes direct grants, and rotates their distinct URL-safe passwords.
`postgres-role-grants` calls the owner-only migration function after every
migration attempt, including a restart with migrations 004/005 already applied.
The roles are intentionally narrow:

- `videosim_app` runs the GUI/control-plane and may insert its required outbox
  events, but cannot update/delete `audit_events` or mutate/delete outbox
  delivery state.
- `videosim_publisher` may only claim/mark `outbox` delivery state.
- `videosim_pruner` may only call the security-definer bounded alarm-history
  pruning function; it has no direct alarm-event table access.

These controls are not an external immutable archive. PostgreSQL owner/superuser
credentials, destructive administrative actions, backup retention, and off-box
log/audit export remain operational security responsibilities.

### SQLite feed cutover

Stop writes to the old app, retain the SQLite file, apply PostgreSQL migrations,
and run:

```sh
python3 -m videosim import-sqlite-feeds \
  --sqlite-path /var/lib/videosim/feeds.sqlite3
```

The import inserts missing feeds and increments `config_version` only when the
stored configuration differs, so a retry is safe. Compare the GUI feed list,
then start the PostgreSQL-backed app. This command does not import transient
process state or JSON alarm/event history.

## JetStream and outbox

Provision or validate the stream:

```sh
python3 -m videosim nats-init --nats-url 'nats://user:...@nats:4222'
python3 -m videosim outbox-publisher \
  --nats-url 'nats://user:...@nats:4222' --batch-size 100
```

The `VIDEOSIM_EVENTS` stream uses file storage, seven-day retention, a
two-minute duplicate window, and versioned subjects:

- `videosim.results.accepted.v1`
- `videosim.alarms.transition.v1`
- `videosim.audit.v1`
- `videosim.workers.health.v1`

Initialization also rejects retention or duplicate-window drift rather than
silently claiming readiness. Publishing is deliberately **at least once**: a
broker acknowledgement followed by a database-mark failure can be replayed,
and JetStream's two-minute duplicate window is only a short optimization. Every
future consumer must atomically record `(consumer_name, event_id)` in
`consumer_inbox` with its projection mutation and reject payload-hash drift.
No consumer/read cutover is allowed until that inbox behavior is implemented
and replay-tested. Monitor pending, publishing, retry, and dead outbox counts.
After inspecting/fixing the cause and verifying the immutable payload, requeue a
single dead event with `python3 -m videosim outbox-requeue --event-id <uuid>`;
do not bulk-reset dead rows without an incident replay plan.

## Backup and restore

Create and structurally verify a custom-format backup:

```sh
export VIDEOSIM_DATABASE_URL='postgresql://.../videosim'
scripts/postgres-backup.sh /secure/backups/videosim-$(date +%FT%H%M%S).dump
```

Restore only into an explicitly approved target database:

```sh
export VIDEOSIM_RESTORE_DATABASE_URL='postgresql://.../videosim_restore'
export VIDEOSIM_RESTORE_EXPECTED_TARGET='restore-db.example:5432/videosim_restore'
export VIDEOSIM_RESTORE_CONFIRM="DESTROY_AND_RESTORE $VIDEOSIM_RESTORE_EXPECTED_TARGET"
export VIDEOSIM_RESTORE_BROKER_MODE=empty  # or retained; decide from incident evidence
scripts/postgres-restore.sh /secure/backups/videosim-....dump
```

The script refuses a source/target identity match and requires the resolved
`host:port/database` identity plus an exact destructive confirmation before
`pg_restore --clean`. Before that destructive step, a read-only preflight rejects
a runtime/non-owner target credential and reused runtime/owner passwords. Export
the target deployment's distinct
`POSTGRES_APP_PASSWORD`, `POSTGRES_PUBLISHER_PASSWORD`, and
`POSTGRES_PRUNER_PASSWORD`; if the target URL uses `.pgpass`, also export
`POSTGRES_RUNTIME_OWNER_PASSWORD`. After migration, restore converges their
ownership/membership/direct grants and reapplies the narrow role policy that
`pg_restore --no-privileges` omits. It then expires restored
offered/active/draining leases, so old workers cannot retain authority after
promotion. `empty` broker mode requeues every non-dead immutable outbox event;
`retained` preserves delivery state. Never guess: for DB-only loss use the
retained broker and reconcile its stream watermark; for broker loss use `empty`
and rely on mandatory consumer inbox deduplication; for coordinated loss use a
matched backup/watermark and document the replay boundary. Compare source and
restored semantic state only while app, scheduler, and publisher writes are
quiesced:

```sh
scripts/verify-postgres-restore.py \
  --source-url "$VIDEOSIM_DATABASE_URL" \
  --restored-url "$VIDEOSIM_RESTORE_DATABASE_URL"
```

The verifier uses repeatable-read snapshots and compares migration
identities/checksums, canonical feed configs, result/alarm/audit/inbox/outbox
semantics, and table counts. Lease state, generation, and outbox delivery state
can differ by design after restore. Quiescence is still required because source
and target are separate snapshots. Encrypt backups, restrict credentials, test a restore on
the production PostgreSQL major version, and record duration/bytes/data-loss
window. The local test evidence is functional only; it is not PITR, RPO, or RTO
certification.

## Rollback

Before enabling PostgreSQL worker v2 in an environment, rollback is
operationally simple: stop the new app and publisher, restore the prior
Compose file/app image, and point it at the retained SQLite data. Preserve
PostgreSQL/NATS volumes for investigation; do not execute a down migration.

After the implemented direct PostgreSQL cutover, rollback is a planned
compatibility operation: quiesce workers, preserve/inspect durable reports,
monitor pending/alarm/event rows, and outbox state, route only a controlled lab
population to retained SQLite v1, and start replacement workers with new
lease epochs. Never allow old process-local tokens or restored leases to regain
authority. Consumer replay and HA rollback drills remain open F2/F4 gates.

## Verification

The integration suite is opt-in because it requires real services:

```sh
VIDEOSIM_TEST_POSTGRES_URL='postgresql://owner:.../videosim' \
VIDEOSIM_TEST_POSTGRES_APP_URL='postgresql://videosim_app:.../videosim' \
VIDEOSIM_TEST_POSTGRES_PUBLISHER_URL='postgresql://videosim_publisher:.../videosim' \
VIDEOSIM_TEST_POSTGRES_PRUNER_URL='postgresql://videosim_pruner:.../videosim' \
VIDEOSIM_TEST_NATS_URL='nats://...:4222' \
python3 -m unittest \
  tests.test_postgres_store tests.test_nats_publisher tests.test_worker_v2 -v
```

It proves migration idempotence/checksum/unknown-version rejection and
transactional reversal; feed import/versioning; offered/acknowledged leases,
heartbeat/incarnation/config/epoch/clock fencing; duplicate report/result
semantics; no false alarm clear on inconclusive evidence; immutable
audit/outbox IDs and database-enforced mutation rejection; atomic persistent
HTTP feed/profile audits; durable operator/worker/proxy denial records with
path sanitization; runtime-role convergence/least privilege; simultaneous report
and outbox-claim serialization; JetStream persistence/configuration/deduplication;
publish acknowledgement; and real HTTP worker-v2 offer/ack/report/retry/config-
fence/incarnation-restart behavior including one complete `run_worker --once`
flow; direct PostgreSQL `/state.json` reads; catalog monitor
pending/delay/repeat/clear/suppress transitions; omission/inconclusive
preservation; and bounded event retention.
See `docs/work-log.md` for the exact most recent run and restore evidence.
