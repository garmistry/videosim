# Durable Control-Plane Foundation

This document describes the F2 durable foundation and its first HTTP cutover
slice. It adds a PostgreSQL authority model, checksum-verified migrations,
fenced transactions, a transactional outbox, a durable NATS JetStream event
stream, and a `videosim.worker/v2` lease/report path. It does **not** claim that
operator alarm reads, security audit writes, or event consumers have completed
their cutover.

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
- Lease and heartbeat freshness decisions use PostgreSQL time. New or changed
  authority is `offered` until the matching worker incarnation explicitly
  acknowledges its epoch/config tuple; stale workers cannot offer, acknowledge,
  renew, or ingest. A restart, owner/config change, or expiry advances the epoch.
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
  persistent volumes and required credentials. A one-shot migration service
  gates app startup; a one-shot stream initializer gates the outbox publisher.
- SQLite feed definitions have an idempotent cutover command.
- Backup, restore, restored-lease expiry fencing, explicit broker-recovery
  mode, and snapshot-consistent semantic comparison tools exist under `scripts/`.

Not yet confirmed or implemented:

- PostgreSQL-backed deployments now negotiate `videosim.worker/v2`: durable
  worker incarnation, offered/acknowledged lease epoch/config, per-lease
  epoch/config sequence, immutable report ID, bounded observation time, and deterministic
  result IDs. SQLite trusted-lab deployments retain worker v1 compatibility.
  V2 stores probe-check results durably first, then updates the legacy JSON
  monitor projection; that projection remains the operator alarm read path.
- Security audit events still go to structured stdout; the durable audit
  repository primitive is not yet connected to every HTTP authorization path.
- Operator alarm/event reads have not yet moved to a disposable PostgreSQL
  projection, actual per-monitor alarm snapshots are not yet projected from v2
  reports, and no JetStream consumer is deployed.
- PostgreSQL and NATS are single instances in Compose. There is no HA, PITR,
  multi-zone, or production RPO/RTO evidence.
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
6. Accepted immutable results update `current_check_state`. Only conclusive
   healthy evidence clears an alarm; unknown/stale/skipped/error/timeout
   evidence preserves alarm state. Alarm edges append `alarm_events`.
7. Events are appended to `outbox` before the transaction commits.
8. Publishers claim due rows with `FOR UPDATE SKIP LOCKED`, publish with the
   outbox `event_id` as `Nats-Msg-Id`, and persist the broker sequence.

A stale or mismatched result is recorded only in the report disposition. It
cannot update result, current-state, alarm/event, or outbox rows.

## Worker API v2 transition

When `VIDEOSIM_DATABASE_URL` selects PostgreSQL, worker endpoints require v2:

1. One worker process generates a UUID incarnation and sends it on assignment,
   heartbeat, acknowledgement, and report calls. A different incarnation cannot
   take over the same worker ID while the current one is heartbeat-fresh; crash
   replacement waits for freshness expiry, preventing old-process ABA revival.
2. Assignment reads use DB-fresh worker membership and deterministic
   round-robin placement. The scheduler revokes dropped leases and returns each
   stream's offered/active epoch, config version, and expiry.
3. The worker acknowledges the exact lease tuple before probing. Authenticated
   independent heartbeats extend only active leases for the same incarnation.
4. Each lease tuple maintains its own positive sequence, resetting to one when
   epoch/config changes and advancing only while that tuple persists. The worker
   also sends an immutable batch report UUID. The server deterministically
   derives result UUIDs for scoped probe checks, maps
   success/issue/error/timeout/skipped to explicit statuses, and lets PostgreSQL
   revalidate every fence.
5. PostgreSQL commits accepted results/outbox and a durable per-stream shadow
   projection fence first. The legacy JSON projection applies only a pending
   fence matching its report/epoch/config/sequence/payload hash; a newer report
   or reassignment makes an old duplicate retry a no-op. A JSON-write failure
   returns 503; retrying the same report ID can safely complete only its still
   current pending projection. A fence rejection returns 409 and causes a
   bounded assignment refetch.

SQLite keeps `videosim.worker/v1` for trusted-lab compatibility. V1 is not a
production fallback and cannot submit to the durable result path. V2 currently
stores `probe.*` checks as shadow durability evidence; actual monitor alarm
snapshot projection and PostgreSQL operator reads remain the next F2 gate. A
pending shadow write is no-regression fenced but has no independent replay
worker yet, so a crashed worker can leave `/state.json` temporarily behind the
committed result state.

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
`pg_restore --clean`. It then reapplies/verifies migrations and expires restored
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

After the implemented worker-v2 cutover, rollback is a planned compatibility
operation: quiesce workers, preserve/inspect the durable report and shadow
fence state, route only a controlled lab population to retained SQLite v1, and
start replacement workers with new durable lease epochs. Never allow old
process-local tokens, pending shadow callbacks, or restored leases to regain
authority. Dual-read alarm projection, consumer replay, and HA rollback drills
remain open F2/F4 gates.

## Verification

The integration suite is opt-in because it requires real services:

```sh
VIDEOSIM_TEST_POSTGRES_URL='postgresql://.../videosim' \
VIDEOSIM_TEST_NATS_URL='nats://...:4222' \
python3 -m unittest \
  tests.test_postgres_store tests.test_nats_publisher tests.test_worker_v2 -v
```

It proves migration idempotence/checksum/unknown-version rejection and
transactional reversal; feed import/versioning; offered/acknowledged leases,
heartbeat/incarnation/config/epoch/clock fencing; duplicate report/result
semantics; no false alarm clear on inconclusive evidence; immutable
audit/outbox IDs; simultaneous report and outbox-claim serialization;
JetStream persistence/configuration/deduplication; publish acknowledgement; and
real HTTP worker-v2 offer/ack/report/retry/config-fence/incarnation-restart
behavior including one complete `run_worker --once` flow.
See `docs/work-log.md` for the exact most recent run and restore evidence.
