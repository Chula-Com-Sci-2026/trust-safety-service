# Trust & Safety Service

Keeps the Can't Run Alone community safe: it owns verified badges, user reports, moderation and account suspensions (FR-3.4, FR-4.5, FR-7.5, FR-10.1, FR-10.2). Python, FastAPI and PostgreSQL, following ADR-0020.

## Processes

The same image runs three processes against one database:

| Process | Command | Job |
|---|---|---|
| API | default image command | REST operations below |
| Consumer | `python worker.py` | Grants the badge on `PhoneVerified` |
| Publisher | `python publisher.py` | Sends outbox events to RabbitMQ |

## API

All routes need a valid access token. `/moderation` routes also need the caller's account ID in `MODERATOR_IDS`.

| Operation | Route |
|---|---|
| `getVerificationStatus` | `GET /api/v1/verification/{runnerId}` |
| `reportRunner` | `POST /api/v1/reports/runners` |
| `reportReview` | `POST /api/v1/reports/reviews` |
| `getModerationQueue` | `GET /api/v1/moderation/queue` |
| `reviewVerification` | `POST /api/v1/moderation/verifications/{runnerId}/review` |
| Resolve a report | `POST /api/v1/moderation/reports/{reportId}/resolution` |
| `suspendAccount` | `POST /api/v1/moderation/suspensions` |
| Audit log | `GET /api/v1/moderation/audit-log` |

Resolving a report and reading the audit log are additions to the design's operation list. The queue could not be emptied without the first, and ADR-0018 lists an audit log. `GET /openapi.json` has the request and response schemas.

## Rules

- A rejection needs a reason, and only the runner sees it. Other runners see `unverified`.
- Redelivered `PhoneVerified` events are safe and cannot undo a rejection.
- A runner has one open report per target, and cannot report themselves.
- Suspending a runner closes their open reports as `actioned`. Moderators cannot suspend themselves.
- State, its event and its audit entry commit in one transaction.

## Events

Envelope and exchange follow `running-buddy-integration/contracts/events.md`: exchange `cra.events`, routing key equal to `eventType`, version 1, at-least-once delivery. Every payload is `{"runnerId": "UUID"}`.

- Consumes `PhoneVerified`
- Publishes `RunnerVerified`, `VerificationRejected`, `AccountSuspended`

Failed payloads go to the `trust-safety.dead` queue.

## Configuration

See `.env.example`. `MODERATOR_IDS` empty means nobody can moderate. `IDENTITY_LOCK_URL` empty means a suspension is recorded and published, but the runner can still sign in; the response shows this as `identityLocked: false`. If the URL is set and Identity fails, the suspension is rolled back.

## Tests

Needs PostgreSQL. Run with Python 3.13 and `requirements.lock` installed:

```sh
JWT_SECRET=test-only DATABASE_URL=postgresql://localhost/trust_safety pytest -q
```

## Not connected yet

- **k8s:** Compose runs this service through `running-buddy-integration`, but the k8s manifests do not include it.
- **`PhoneVerified`:** Identity does not publish it, so no badge is granted automatically.
- **Identity lock:** `lockAccount` exists in Identity but has no route, so `IDENTITY_LOCK_URL` has nothing to point at. The request body matches that controller.
- **Event consumers:** nothing binds the three published events. Unrouted events stay in the outbox and are retried, per ADR-0021.
- **Contracts:** `running-buddy-contracts` still says this service has no contract; the integration repo has the current snapshot.
- **Review reports:** the review ID is not checked against Reputation.
- **Broker tests:** `pytest` uses fakes for RabbitMQ. The flow was checked once by hand against real RabbitMQ through the gateway.
