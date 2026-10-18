# Per-fan message debounce

Batch a fan's rapid-fire messages into **one** reply, generated in order, exactly
once, correctly across multiple Cloud Run instances. Shipped behind
`DEBOUNCE_ENABLED` (default **false** → the webhook replies synchronously exactly
as before; flip to `true` to turn it on).

## Why

A fan often sends 2–4 messages in a burst ("hey" / "you up?" / "?!"). Replying to
each one separately is robotic and triples cost. We want to wait until they've
stopped typing, then answer the whole burst in a single, in-context reply — and we
must do it safely when two messages land on two different Cloud Run instances.

## The two fields (on `fans/{fanUuid}`)

| field | value | role |
|---|---|---|
| `processingLease` | `null` or `{owner, expiresAt}` | **the lock** — one worker per fan at a time; self-expires so a dead worker can't freeze a fan |
| `lastProcessedAt` | ISO timestamp | **the watermark** — newest fan message we've already replied to; anything newer is pending |

Messages live in `fans/{fanUuid}/messages/{messageUuid}` (doc id = Fanvue
`messageUuid` → idempotent). Each carries `role` (`fan`/`jenny`), `ts`, and `via`
(`webhook` live vs `reconcile` backfill).

```
unanswered = messages where role == "fan" AND ts > lastProcessedAt   (oldest first)
```

## The flow

**Webhook (inbound).** Record the fan message (`store.record_inbound`, idempotent
by uuid) → enqueue a Cloud Tasks job for this fan at `now + SETTLE` → return 200.
If the queue isn't wired up or the enqueue fails, fall back to the old synchronous
reply (never drop the message).

**Worker (`POST /admin/process_fan`, fired by Cloud Tasks).** See `agent/debounce.py`:

1. read `unanswered`; empty → done
2. **quiet check**: newest message arrived `< SETTLE` ago → bail; that message's own
   task will do the work (only the *last* message's task finds the fan quiet, so the
   timer effectively "resets" on every new message)
3. **acquire the lease**; held by a live worker → re-enqueue in a few seconds + bail
4. **re-read under the lock**; fix `through_ts = newest unanswered fan ts`
5. **retry-only read-back**: if this is a Cloud Tasks redelivery and Fanvue already
   shows a Jenny reply after `through_ts` (a prior crashed attempt did send), skip the
   send and just advance
6. **generate → send → record** one reply for the whole batch (`graph.handle_batch`;
   send-FIRST ordering)
7. **commit**: advance `lastProcessedAt = through_ts` + release the lease (one txn)
8. **tail check**: messages that landed during generation → enqueue a follow-up

### Ordering guarantees

- *Within a batch*: unanswered messages are fed oldest-first.
- *Across batches*: the lease serialises work per fan — batch N fully commits
  (sends + advances the watermark + releases) before N+1 can start. Advancing the
  watermark permanently excludes everything at/below it, so nothing is answered twice.
- A message that arrives mid-generation (Scenario C) is excluded from the running
  batch by the `through_ts` cap and answered by its own task *after* the current
  batch commits — so it's always answered knowing the previous reply went out.

## Idempotency & crash safety

The full flow spans Firestore + the LLM + Fanvue, so it **cannot** be one atomic
transaction. Instead each step is idempotent/retryable and the ordering is chosen
so failures only ever go the *safe* direction:

- **Inbound never lost** — written before the 200, keyed by `messageUuid` (no dup);
  also swept by the nightly reconcile if a webhook is missed entirely.
- **Watermark never advances without a confirmed send** — it moves only in the final
  commit, *after* the send succeeds. Crash before commit → batch looks unanswered →
  a retry regenerates.
- **Never recorded-but-unsent** — send happens before the DB write, so a crash can
  only leave "sent but not yet recorded" (recoverable), never "recorded but silently
  never sent".
- **Dead worker self-heals** — the lease has a TTL (`PROCESS_LEASE_TTL_SECONDS`);
  once it expires a retried task may steal it.
- **Residual** — a crash in the sub-second gap after Fanvue accepts but before the
  retry's read-back can see it (Fanvue read-after-write lag) can cause one *duplicate*
  reply, never a lost one. We accept that: in a chat, a rare double-text beats silence.

## Config (`agent/config.py`)

| env | default | meaning |
|---|---|---|
| `DEBOUNCE_ENABLED` | `false` | master switch |
| `DEBOUNCE_SETTLE_SECONDS` | `3` | quiet window before answering a batch (total time-to-reply ≈ this + ~3-6s generation) |
| `PROCESS_LEASE_TTL_SECONDS` | `90` | lock lifetime (must exceed worst-case generation) |
| `PROCESS_BUSY_RETRY_SECONDS` | `5` | re-check delay when the fan is busy |
| `TASKS_QUEUE` / `TASKS_LOCATION` | `jenny-process` / region | Cloud Tasks queue |
| `SERVICE_BASE_URL` | — | the service's own URL (task POSTs back here) |
| `TASKS_INVOKER_SA` | — | SA for the task's OIDC token (future hardening) |

## Reconcile interaction

The nightly reconcile backfills history (`via: "reconcile"`) and **never enqueues a
reply**. After importing a chat it advances the watermark to our **last reply's**
timestamp (`store.advance_watermark_to`), so:

- a brand-new backfilled fan isn't treated as having an entire conversation pending;
- a *buried* missed message (older than our last reply) stays below the watermark and
  is never answered out of order;
- a genuinely-last missed fan message (newer than our last reply) stays pending and is
  swept by the next webhook-triggered batch.

## Provisioning

Terraform (`infra/main.tf`) creates the queue + grants the runtime SA
`roles/cloudtasks.enqueuer`. Manual equivalent:

```sh
gcloud tasks queues create jenny-process --location=us-central1 \
  --max-attempts=8 --min-backoff=5s --max-backoff=120s

gcloud projects add-iam-policy-binding capsule-487202 \
  --member="serviceAccount:jenny-runtime@capsule-487202.iam.gserviceaccount.com" \
  --role="roles/cloudtasks.enqueuer"
```

To turn it on: set `DEBOUNCE_ENABLED=true` (and ensure `SERVICE_BASE_URL` points at
the live service) on the Cloud Run service, then redeploy.
