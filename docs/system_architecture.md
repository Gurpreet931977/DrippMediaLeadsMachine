# Dripp Media International Lead System — System Architecture & Authoritative Data Model
**Phase 10.1 Production Hardening & Technical Operations Architecture**

---

## 1. System Overview

The Dripp Media International Lead System is an enterprise-grade customer acquisition, qualification, enrichment, and commercial pipeline platform for high-traction independent hospitality and restaurant businesses across international target markets.

```text
                                  ┌─────────────────────────────┐
                                  │      DISCOVERY ENGINE       │
                                  │ (OSM, Foursquare, Web, etc.)│
                                  └──────────────┬──────────────┘
                                                 │
                                                 ▼
                                  ┌─────────────────────────────┐
                                  │     IDENTITY RESOLVER       │
                                  │ (Canonical ID, Branch Guard)│
                                  └──────────────┬──────────────┘
                                                 │
                                                 ▼
                                  ┌─────────────────────────────┐
                                  │    QUALIFICATION ENGINE     │
                                  │ (Rule B: 50+ rev, 4.0★, web)│
                                  └──────────────┬──────────────┘
                                                 │
                                                 ▼
                                  ┌─────────────────────────────┐
                                  │     ENRICHMENT ENGINE       │
                                  │  (Gosom, Google, Social)    │
                                  └──────────────┬──────────────┘
                                                 │
                                                 ▼
                                  ┌─────────────────────────────┐
                                  │   CONTACTABILITY VERIFIER   │
                                  │ (Phone, IG, FB, Email, MX)  │
                                  └──────────────┬──────────────┘
                                                 │
                                                 ▼
                                  ┌─────────────────────────────┐
                                  │   CRM & TIMELINE LEDGER     │
                                  │ (Reconciled Canonical State)│
                                  └──────────────┬──────────────┘
                                                 │
                                                 ▼
                                  ┌─────────────────────────────┐
                                  │     OUTREACH ANALYTICS      │
                                  │ (Performance, Benchmarks)   │
                                  └──────────────┬──────────────┘
                                                 │
                         ┌───────────────────────┴───────────────────────┐
                         ▼                                               ▼
          ┌─────────────────────────────┐                 ┌─────────────────────────────┐
          │   COMMERCIAL CRM PIPELINE   │                 │     PROPOSAL WORKSPACE      │
          │(Prospect, Discussion, Close)│                 │ (Packages, Scope, WON/LOST) │
          └─────────────────────────────┘                 └─────────────────────────────┘
                         │                                               │
                         └───────────────────────┬───────────────────────┘
                                                 ▼
                                  ┌─────────────────────────────┐
                                  │   SAFE OPERATOR GATEWAY     │
                                  │  TRAVEL MODE: COMM LOCKED   │
                                  └─────────────────────────────┘
```

---

## 2. Component Architecture Mapping

| Component | Responsibility | Authoritative Data Source | Inputs | Outputs | Dependencies | Failure Behavior |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **DiscoveryEngine** (`lib/discovery/hybrid.py`) | Multi-source discovery across geographical bounding boxes | None (Stateless discovery) | MarketConfig, coordinates, queries | Raw candidate entities with geo-tags | OSM, Foursquare, SearXNG, Web Search | Graceful fallback to cached discovery; returns empty candidates on provider timeout |
| **IdentityMatcher** (`lib/crm/identity_matcher.py`) | Entity resolution, address normalization, branch separation | `data/cache_sheets_leads.json` (CRM Leads) | Raw candidates, existing CRM records | Canonical Lead ID (`LEAD-MKT-XXXXXX`), duplicate class | `lib/discovery/address_normalizer.py` | Marks unclear identity as `POSSIBLE_DUPLICATE` for review; never merges distinct branches |
| **LeadScoringEngine** (`lib/qualification/lead_scoring.py`) | Frozen Rule B qualification: >=50 reviews, >=4.0★, <=180d, no website | Authoritative Rule Engine logic | Raw candidate reviews, ratings, website checks | Qualification state (`OUTREACH_READY`, `DISQUALIFIED`, `REVIEW`) | Frozen qualification rule set | Disqualifies candidate on missing/ambiguous evidence; never guesses ratings |
| **EnrichmentEngine** (`lib/enrichment/phase_9_1_enrichment_engine.py`) | Deep review recovery, photo checks, operational verification | Review provider APIs / cache stores | Qualified candidates | Verified review count, rating, review dates, operational signal | Gosom, Google Places, Cache crawler | Fallback to multi-signal operational verification; flags `STALE_REVIEW` if unavailable |
| **ContactabilityEngine** (`lib/outreach/contactability.py`) | Multi-channel contact discovery, validation, and verification | `data/contact_history.json` | Candidates with social/phone/email data | Verified contact channels (phone, IG, FB, email, MX) | DNS resolver, social validators | Retains existing verified contacts; marks unreachable contacts as `INVALIDATED` with evidence |
| **CRMReconciler** (`lib/crm/reconciliation.py`) | Cross-record duplicate detection, canonical record preservation | `data/cache_sheets_leads.json` / Google Sheets | Sheet rows, candidates | Merge plans, canonical updates, deduplicated rows | `lib/crm/identity_matcher.py`, `fcntl` locking | Produces non-destructive dry-run plans; requires atomic locks before sheet mutation |
| **CommercialPipelineManager** (`lib/commercial/commercial_pipeline_manager.py`) | Commercial lifecycle: QUALIFIED -> CONTACTED -> ENGAGED -> PROPOSAL_SENT -> CLOSED | `data/commercial_records.json` | Operator commands, outcome events | Commercial records, commercial events, next actions | FileLock, AtomicWriter, Timelines | Never auto-advances stage; rejects invalid transitions with ValueError |
| **ProposalManager** (`lib/commercial/proposal_manager.py`) | Structured website proposal lifecycle, packages, revisions, deal close | `data/commercial_proposals.json` | Proposal creation requests, operator approvals | DRAFT proposals, shareable HTML, WON/LOST records | FileLock, AtomicWriter, CommercialRecords | Rejects editing sent proposals; requires operator confirmation for WON |
| **OutreachPerformanceEngine** (`lib/analytics/outreach_performance_engine.py`) | Deterministic computation of outreach metrics, funnel rates, channel stats | Event logs (`message_history.json`, `outreach_outcomes.json`) | Authoritative event streams | `data/outreach_performance_snapshot.json` | Event log stores | Recomputes snapshots deterministically from scratch; snapshots are strictly derived |
| **BackupManager** (`lib/system/backup_manager.py`) | Point-in-time state snapshots, SHA-256 verification, disaster recovery | Production state stores in `data/` | Pre-mutation triggers, scheduled tasks | Timestamped backup archives + `manifest.json` | File system, hashlib | Verifies backup checksums before confirming; raises `IntegrityError` on corruption |
| **TechnicalOrchestrator** (`lib/system/technical_orchestrator.py`) | Unattended execution of technical batch operations and refreshes | Run checkpoints & job state | Job requests, MarketConfig, Quotas | Structured job run history, updated records | QuotaGovernor, BackupManager, FreshnessEngine | Pauses runs on quota exhaustion; saves checkpoint for idempotent restart |
| **QuotaGovernor** (`lib/system/quota_governor.py`) | Central budget governance for external search, scraping, CRM writes | `data/.quota_state.json` | Resource consumption requests | Quota status (allowed/paused), usage counters | Named file lock | Rejects consumption exceeding budget; pauses job instead of allowing overrun |
| **FreshnessEngine** (`lib/system/freshness_engine.py`) | Evidence staleness detection per data family | State stores (Leads, Reviews, Social, Web) | Lead timestamps and evidence dates | Freshness flags (`STALE_REVIEW`, `STALE_WEBSITE`, etc.) | Configurable TTL thresholds | Flags stale evidence without downgrading qualification; queues refresh |
| **StateReconciliationEngine** (`lib/system/reconciliation_engine.py`) | Cross-system invariant enforcement (CRM vs Events vs Proposals) | Cross-store state matrix | CRM leads, message history, commercial records, proposals | Structured contradiction issues with severity & recommended actions | All authoritative stores | Never silently repairs inconsistencies; alerts operator and flags severity |
| **IdentityIntegrityAuditor** (`lib/system/identity_integrity.py`) | Cross-store identity consistency, canonical lead ID validation | All stores referencing `lead_id` | CRM, Research Log, Queue, Timelines, Commercial, Proposals | Cross-store audit report, duplicate counts | Regex rules, Canonical ID rules | Fails health check if research IDs masquerade as canonical lead IDs |
| **SystemHealthMonitor** (`lib/system/system_health.py`) | Global technical health aggregation for `GET /api/system/health` | System logs, stores, quotas, scheduler | Orchestrator runs, backups, locks, errors | Comprehensive system health snapshot (`HEALTHY`/`DEGRADED`/`FAILED`) | All system modules | Degrades health status on failed jobs or stale backups; fails on file corruption |

---

## 3. Authoritative Data Model Matrix

The platform strictly enforces the following data ownership boundaries. Snapshots and derived caches are **never** treated as authoritative state.

| Data Domain | Authoritative Source | Read-Only Derivatives | Write Path | Sync Path |
| :--- | :--- | :--- | :--- | :--- |
| **Lead Identity** | `data/cache_sheets_leads.json` (CRM Leads Tab) | Research log, Review queue, Market run summaries, UI views | `LeadGenerationPipeline` -> `IdentityMatcher` -> Atomic CRM Writer | `sheets_sync.py` / Local atomic JSON |
| **Qualification State** | Rule B Qualification Engine (`lib/qualification/lead_scoring.py`) | CRM `qualification_state`, Scorecard snapshots, Research log | Canonical rule evaluation -> Atomic CRM Writer | Re-evaluation pipeline only |
| **Contactability State** | `data/contact_history.json` & CRM Lead Contact Fields | Activation queue, Controlled batch queue, UI contact views | `ContactabilityEnricher` -> `data/contact_history.json` | Atomic contact update |
| **Outreach State** | `data/controlled_batch_state.json` & `data/campaigns.json` | CRM `outreach_status`, Campaign dashboard, Queue views | `ManualOutreachController` / `ControlledBatchExecutor` | Named lock write path |
| **Outreach Events** | `data/message_history.json` & `data/outreach_outcomes.json` | `data/lead_timelines.json`, `data/outreach_performance_snapshot.json` | `OutcomeTracker._save_outcomes()` (Append-only) | Atomic append stream |
| **Commercial State** | `data/commercial_records.json` | Commercial pipeline snapshot, CRM commercial columns, UI pipeline | `CommercialPipelineManager._save_commercial_records()` | Event emission to timeline |
| **Commercial Events** | `data/commercial_events.json` | `data/lead_timelines.json`, Deal audit log | `CommercialPipelineManager._append_commercial_event()` | Append-only event log |
| **Proposal State** | `data/commercial_proposals.json` | `data/proposal_pipeline_snapshot.json`, Shareable HTML preview | `ProposalManager._save_proposals()` | Atomic proposal update |
| **Analytics Snapshots** | Event Logs (`message_history.json`, `outcomes.json`, `commercial_events.json`) | `outreach_performance_snapshot.json`, `commercial_pipeline_snapshot.json`, `proposal_pipeline_snapshot.json` | Deterministic generation engines | Regeneration from event streams |
| **Technical Run State** | `data/market_runs/RUN-*.json` & `data/.orchestrator_checkpoints.json` | UI Technical Operations tab, Run history table, Scorecards | `TechnicalOrchestrator._persist_checkpoint()` | Atomic checkpoint write |

---

## 4. State Stores & JSON File Registry

| File Path | Domain | Mutability | Authoritative | Description |
| :--- | :--- | :--- | :--- | :--- |
| `data/cache_sheets_leads.json` | CRM Leads | Read/Write | Yes | Primary local cache of all qualified and discovered leads |
| `data/cache_sheets_research_log.json` | CRM Research | Append-Only | Yes | Log of all investigated businesses and qualification outcomes |
| `data/cache_sheets_review_queue.json` | CRM Review | Read/Write | Yes | Businesses flagged for operator or reviewer inspection |
| `data/commercial_records.json` | Commercial | Read/Write | Yes | Commercial stages, notes, follow-up records, and preview links |
| `data/commercial_events.json` | Commercial | Append-Only | Yes | Immutable commercial transition event audit log |
| `data/commercial_proposals.json` | Commercial | Read/Write | Yes | Full proposal documents, revisions, packages, and statuses |
| `data/proposal_packages.json` | Commercial | Read-Only Config | Yes | Standard proposal packages (STARTER, STANDARD, PREMIUM, CUSTOM) |
| `data/message_history.json` | Outreach | Append-Only | Yes | Sent message audit log (channel, recipient, content, timestamp) |
| `data/contact_history.json` | Outreach | Append-Only | Yes | Verification history of phone, IG, FB, email, and MX |
| `data/outreach_outcomes.json` | Outreach | Append-Only | Yes | Recorded prospect responses, callbacks, objections, bounces |
| `data/campaigns.json` | Outreach | Read/Write | Yes | Outreach campaign definitions and execution statuses |
| `data/execution_gate.json` | Outreach | Read/Write | Yes | Rate limits, cooldowns, and channel safety gates |
| `data/suppression_list.json` | Outreach | Read/Write | Yes | Opted-out, competitor, or disqualified suppression records |
| `data/controlled_batch_state.json` | Outreach | Read/Write | Yes | Controlled pilot batch execution state and progression cursor |
| `data/lead_timelines.json` | System Ledger | Append-Only | Derived | Unified chronological timeline of events per lead ID |
| `data/outreach_performance_snapshot.json`| Analytics | Overwrite | Derived | Deterministically generated metrics on outreach conversion |
| `data/commercial_pipeline_snapshot.json` | Analytics | Overwrite | Derived | Deterministically generated pipeline counts, values, stages |
| `data/proposal_pipeline_snapshot.json`   | Analytics | Overwrite | Derived | Funnel conversion rates, proposal totals, win/loss rates |
| `data/system_health_snapshot.json`      | System Health | Overwrite | Derived | Machine-readable health snapshot for operations monitoring |
| `data/backups/`                         | Recovery | Immutable | Yes | Point-in-time snapshots with SHA-256 integrity manifests |

---

## 5. Failure Behaviors & Error Handling Strategy

1. **Transient Failures (HTTP timeouts, temporary DNS blips, provider 503s)**:
   - Handled with bounded exponential backoff (maximum 3 retries).
   - Candidate is preserved in current state without corruption.
2. **Quota Exhaustion**:
   - `QuotaGovernor` detects limit reached.
   - Active job transitions to `PAUSED` with explicit reason; does NOT attempt to override limits or corrupt state.
3. **Data Quality Contradictions**:
   - `StateReconciliationEngine` flags issue with severity (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`).
   - Discrepancy logged; system rejects destructive write until contradiction is acknowledged or resolved.
4. **Concurrent Access Violations**:
   - `FileLock` enforces single-writer exclusivity across all critical files.
   - Competing processes wait up to timeout before failing cleanly with `LockTimeoutError`.
5. **Mid-Write Crashes**:
   - `AtomicWriter` guarantees that state is either 100% written or unchanged (write to temp file in same directory + `fsync` + atomic `os.replace`).
   - Zero partially written or corrupt JSON files.

---

## 6. Safety & Travel Mode Controls

```text
                  TRAVEL MODE ACTIVATION
                            │
               ┌────────────┴────────────┐
               │                         │
     TECHNICAL AUTOMATION          COMMERCIAL ACTIONS
          [ENABLED]                    [LOCKED]
               │                         │
      ┌────────┴────────┐       ┌────────┴────────┐
      │  Discovery      │       │  Phone Calls    │  --> 403 Forbidden
      │  Enrichment     │       │  Instagram DMs  │  --> 403 Forbidden
      │  Verification   │       │  Facebook DMs   │  --> 403 Forbidden
      │  Deduplication  │       │  Email Sends    │  --> 403 Forbidden
      │  Reconciliation │       │  Proposals Send │  --> 403 Forbidden
      │  Snapshots      │       │  Auto Callbacks │  --> 403 Forbidden
      │  Backups        │       │  Payment Setup  │  --> 403 Forbidden
      └─────────────────┘       └─────────────────┘
```

1. **Central Commercial Kill Switch**:
   `COMMERCIAL_ACTIONS_ENABLED = False` (default False).
   Blocks all automated or manual outbound communication: phone calls, social DMs, emails, proposal dispatch, automated callback scheduling, payment requests, and contracts.
2. **Safe Travel Mode**:
   `TRAVEL_MODE = True` (default True).
   Allows technical background operations to run unattended while strictly locking all commercial actions.
3. **Market Run Controls**:
   Only enabled markets (`MANCHESTER_UK` by default) may run.
   New markets require explicit operator enablement.
