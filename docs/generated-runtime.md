# Generated Feed Runtime

This is the first F4 data-plane split. It removes PostgreSQL-backed generated
feed processes from GUI/API ownership. It is not 1,000-stream admission or a
complete multi-host endpoint allocator.

## Runtime Contract

- PostgreSQL feed config stores `desired_state` as `running` or `stopped`.
- Versioned start, fault-profile, update, alert, stop, and delete requests may
  reach any API replica. PostgreSQL compare-and-swap plus the existing success
  audit remains the mutation authority.
- API replicas do not launch PostgreSQL-backed generated feeds. Detail reads
  report `runtimeKnown=false` and show durable desired state without claiming
  observed process health.
- `python -m videosim.generated_runtime` polls desired generated feeds and
  holds PostgreSQL session advisory locks for both feed ID and SRT port while a
  child process is alive.
- A config-version change stops the old child before relaunch. Stop/delete
  removes the desired row from reconciliation and terminates the child.
- Loss of the lock-holding database session is fatal: the runtime terminates
  all children and exits so a supervisor can restart it. `--max-feeds` bounds
  one process's owned child count.
- The generated DASH overlay writes to a data-plane volume served by a separate
  Nginx origin. The API no longer has to serve those worker-facing URLs when
  `VIDEOSIM_GENERATED_DASH_BASE_URL` is configured.

Existing durable generated rows without `desired_state` are treated as
stopped. Request start after rollout; do not overlap this service with an old
API release that still owns local generated children.

## Production Overlay

Set the existing production secrets, then render and start the data-plane
overlay with the control plane:

```sh
docker compose \
  -f docker-compose.production.yml \
  -f docker-compose.generated-runtime.yml \
  config --quiet

docker compose \
  -f docker-compose.production.yml \
  -f docker-compose.generated-runtime.yml \
  up -d postgres migrate app generated-feed-runtime generated-dash-origin
```

Relevant settings are `VIDEOSIM_GENERATED_MAX_FEEDS`,
`VIDEOSIM_GENERATED_SRT_PORT_START`, `VIDEOSIM_GENERATED_SRT_PORT_END`,
`VIDEOSIM_GENERATED_SRT_PORT_RANGE`, `VIDEOSIM_GENERATED_SRT_HOST`,
`VIDEOSIM_GENERATED_DASH_BASE_URL`, and `VIDEOSIM_GENERATED_DASH_PORT`.
The SRT range must match the host firewall and published range.

Inspect lifecycle and media-origin logs with:

```sh
docker compose \
  -f docker-compose.production.yml \
  -f docker-compose.generated-runtime.yml \
  logs --timestamps generated-feed-runtime generated-dash-origin app
```

## Marked Startup Validation

The marked API workflow boots PostgreSQL, migration, two API replicas, and the
generated runtime. It creates through API A, starts through API B, proves real
SRT video/audio/captions, kills and recreates the runtime, proves media again,
stops through API A, proves the endpoint unreachable, and captures Docker state
and logs.

```sh
VIDEOSIM_GENERATED_RUNTIME_INTEGRATION=1 \
  python3 -m unittest tests.test_generated_runtime_startup
```

To reuse a previously built project image while bind-mounting the exact source:

```sh
VIDEOSIM_GENERATED_RUNTIME_INTEGRATION=1 \
VIDEOSIM_GENERATED_VALIDATION_NO_BUILD=1 \
VIDEOSIM_VALIDATION_IMAGE=videosim-startup-validation-app:latest \
  python3 -m unittest tests.test_generated_runtime_startup
```

Set `VIDEOSIM_GENERATED_VALIDATION_ARTIFACT_DIR` to retain `result.json`,
`compose-ps.txt`, `docker.log`, both passing media reports, the expected stopped
report, and the pre-restart runtime log.

## Open Gates

- Generated SRT port selection during API create is still replica-local. The
  runtime rejects duplicate port ownership, but PostgreSQL does not yet allocate
  a unique host/port endpoint atomically. Do not scale generated creates across
  APIs until that allocator exists.
- Advisory locks fence process ownership; observed owner, process health,
  restart count, and endpoint metadata are not yet projected for operator reads.
- A stable SRT address across separate runtime hosts still needs explicit
  host/IP allocation or a proven UDP routing design. The validation recreates
  one service at the same Docker DNS name.
- The DASH origin is one shared-volume Nginx service, not HA object storage or
  a measured cache tier.
- First-claim placement and static `--max-feeds` are not cost-aware scheduling.
  Independent media CPU, memory, descriptor, network, and failure-domain
  headroom remain unmeasured.
- No 24-hour run, multi-host failure test, production load balancer, HA
  PostgreSQL/broker, or F5 1,000-stream admission is claimed.
