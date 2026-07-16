# ADR-0026: Redact rotated secrets at rotation time

- **Status:** Accepted
- **Date:** 2026-07-15
- **Resolves:** [issue #340](https://github.com/LunchBox951/Plex-Management/issues/340), split from [issue #292](https://github.com/LunchBox951/Plex-Management/issues/292)
- **Follow-up:** [issue #374](https://github.com/LunchBox951/Plex-Management/issues/374) tracks historical-row redaction when a user's `User.encrypted_plex_token` changes.

## Context

Read-time value redaction only knows currently configured values. When a secret is replaced or revoked, the old value leaves `SettingsStore.secret_values()` and durable rows containing that value can become readable through log history. Retaining former secrets, even encrypted or hashed, would create a new historical credential surface; hashes cannot redact arbitrary substrings.

## Decision

Use redact-at-rotation. A single process-local `secret_rotation_lock` serializes every durable log read/render, live-ring read, drain insert/commit, and in-scope secret mutation. While holding it, a mutation rewrites both `LogEvent.message` and every string leaf and mapping key in `LogEvent.context_json` using the existing value-first redaction grammar, writes or removes the secret in the same transaction, and commits once. After a successful commit, queued and ring records are synchronously redacted before releasing the lock. No historical secret, ciphertext, reversible metadata, hash, column, table, or migration is retained.

The in-scope mutations are generic settings-secret replacement through `PUT /settings`, existing app-key rotation, and app-key revocation. Initial configuration with no previous value is not a rotation. The optional environment setup token has no supported replacement/removal endpoint and retains existing capture/read redaction only.

`User.encrypted_plex_token` is explicitly excluded from this decision's mutation boundary. `SettingsStore.secret_values()` currently includes those user tokens, and `POST /api/v1/auth/sign-in` replaces them; [issue #374](https://github.com/LunchBox951/Plex-Management/issues/374) must choose the same locked transactional boundary or a consciously revised secret-source design. This ADR does not claim every source returned by `secret_values()` is rotation-safe.

## Failure and concurrency rules

No queued or ring record is destructively rewritten before the database commit. Before commit, the handler widens its in-memory snapshot to old-plus-current values so new emits are safe; a rewrite, secret write/removal, recovery-session revocation, or commit failure rolls back the transaction and restores the exact prior snapshot, leaving queued/ring contents untouched. After commit, queue and ring cleanup runs synchronously while the lock remains held and retains only current values. The lock is deliberately single-process, matching the documented one-worker deployment; it does not claim multi-worker protection.

The read lock covers dependency-transaction rollback, query, fresh secret-value read, redaction, and complete response/export serialization. Drain insertion and commit are also inside it, eliminating a fetch-before-rotation/value-read-after-rotation window.

## Rejected alternatives

- Retaining former plaintext-equivalent or reversible secret material at rest: expands the credential attack surface and requires new schema/storage.
- Hashes-only or substring matching: hashes cannot redact arbitrary message/context substrings.
- Best-effort or background rewriting: leaves a successful rotation with an exposure window and cannot provide transactional rollback.
- A schema migration or historical flag: unnecessary when rows can be rewritten while old values are available.

## Consequences

Successful replacement and removal erase old values from durable messages, nested JSON context, live ring records, and queued records. Existing endpoint response shapes, statuses, generated clients, and the schema remain unchanged. The deferred user-token path is tracked separately in issue #374.
