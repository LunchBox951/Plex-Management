# ADR-0028: Durable pre-add intent

- **Status:** Proposed
- **Date:** 2026-07-28

## Context

A torrent client can accept an add after the application has resolved its hash but
before a tracked `Download` is committed. A crash in that interval makes an
application-category torrent invisible to request recovery and leaves no
operator-safe proof of ownership.

## Decision

The application prepares each source to a stable info-hash before client mutation
and commits a hash-keyed `download_add_intents` record plus normalized title/season
scope rows before submission. A submission uses the derived category
`plex-manager-intent-{id}`. Recovery atomically replaces an intent with its tracked
download, scopes, coverage claims, and history; cancellation only removes a client
torrent when that category or an explicit operator adoption proves ownership.

Park and eviction compare-and-swap predicates treat a matching active intent scope
as ownership, ensuring no committed gap during intent-to-download conversion.

The reconciler inventories only the exact `plex-manager` category. Unmatched
items become bounded client-only correction observations which an administrator can
adopt into a selected request or remove with data through the web UI. No title-based
automatic adoption is permitted.

## Consequences

The port separates `prepare_add` from `add_prepared`; `add` remains a compatibility
composition. This is staged N/N-1: the substrate migration and recovery reader
land before production starts publishing intents, so the preceding stack layer can
recover rows written by the activation layer.
