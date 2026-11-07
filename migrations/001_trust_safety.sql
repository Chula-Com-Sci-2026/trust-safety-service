CREATE TABLE verifications (
 runner_id uuid PRIMARY KEY,
 status text NOT NULL CHECK(status IN ('verified','rejected')),
 reason text, reviewed_by uuid, reviewed_at timestamptz,
 granted_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE reports (
 id uuid PRIMARY KEY, reporter_id uuid NOT NULL,
 target_type text NOT NULL CHECK(target_type IN ('runner','review')), target_id uuid NOT NULL,
 category text NOT NULL, details text NOT NULL,
 status text NOT NULL DEFAULT 'open' CHECK(status IN ('open','dismissed','actioned')),
 resolved_by uuid, resolved_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now()
);
-- A runner can have one open report per target, so repeats cannot flood the queue.
CREATE UNIQUE INDEX one_open_report ON reports(reporter_id,target_type,target_id) WHERE status='open';
CREATE INDEX open_reports ON reports(created_at) WHERE status='open';
CREATE TABLE suspensions (
 runner_id uuid PRIMARY KEY, reason text NOT NULL, suspended_by uuid NOT NULL,
 identity_locked boolean NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE audit_log (
 id uuid PRIMARY KEY, moderator_id uuid NOT NULL, action text NOT NULL, target_id uuid NOT NULL,
 detail text, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE outbox_events (
 id uuid PRIMARY KEY, event_type text NOT NULL, correlation_id uuid NOT NULL, payload jsonb NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz
);
CREATE INDEX unpublished_events ON outbox_events(occurred_at) WHERE published_at IS NULL;
