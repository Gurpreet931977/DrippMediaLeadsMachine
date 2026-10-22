# Dripp Media — Technical Production Readiness & Future Outreach Add-on Checklist

> [!CRITICAL]
> **CURRENT RELEASE SCOPE:** Technical production readiness only. Automated email and live outreach are deliberately excluded and must remain disabled.
> A green readiness check, deployment, backup, or smoke test does **NOT** authorize commercial messages.
> The outreach addon requires a separate, explicit authorization after the technical system is stable.

---

## 1. Governance Architecture & Operating Invariants

| Safety Invariant | Current State | Production Activation Requirement |
| :--- | :--- | :--- |
| **Travel Mode** | `ACTIVE` | Keep active during current technical rollout and smoke tests |
| **Commercial Actions** | `LOCKED` | Keep `COMMERCIAL_ACTIONS_ENABLED=false`; no live outreach in current release |
| **Automated Email** | `DISABLED` | Keep `AUTOMATED_EMAIL_ENABLED=false`; email is a final optional addon |
| **Discovery Job** | `DRY_RUN=true` | Discovery smoke tests only; no CRM writes or outreach dispatches |
| **Scheduled Cron** | `DISABLED` | Workflows remain manual `workflow_dispatch` until separately approved |
| **Rule B Criteria** | `FROZEN` | Zero threshold degradation |
| **CRM Authority** | `Google Sheets` | Remains single source of truth; git never stores CRM state |
| **Human Activation** | `NOT REQUIRED FOR TECHNICAL ROLLOUT` | Outreach confirmation is deferred to a separate addon review |

---

## 2. Staged Activation Checklist Sections

### Section A: Technical Readiness Gate
- [ ] **Deterministic Pre-flight**: Run `.venv/bin/python scripts/production_preflight.py` and verify all 20 gates report `PASS`.
- [ ] **Full Regression Suite**: Run `.venv/bin/pytest -q` and verify 100% test pass rate with 0 failures.
- [ ] **Storage Bootstrap**: Run `python -m lib.system.storage_bootstrap` to confirm all directories (`data/`, `backups/`, `secrets/`) are initialized.
- [ ] **Atomic Writers & Locking**: Verify process locks (`.crm_write.lock`, `.quota_lock.lock`) release cleanly.
- [ ] **Synthetic Staged Pipeline**: Run `StagedPipelineRunner` dry-run and confirm 0 unhandled exceptions.

### Section B: External Account & Provider Readiness
- [ ] **Web Research Provider**: Verify any configured research provider and quota caps without exposing secrets.
- [ ] **Google Sheets CRM**: Confirm least-privilege service-account access and read/write integrity checks.
- [ ] **Quota Ceilings**: Confirm configured search/research budgets match the available provider tier.
- [ ] **Deferred addon dependencies**: SMTP credentials and Meta Graph API access are intentionally out of scope. Do not configure or activate them as part of technical production readiness.

### Section C: Security Readiness
- [ ] **Secret Scan**: Run `python scripts/security_secret_scan.py` and confirm 0 unallowed credentials tracked in Git.
- [ ] **GitHub Actions Permissions**: Verify `.github/workflows/` declare least privilege (`permissions: contents: read`).
- [ ] **Log Redaction**: Confirm `sanitize_text()` actively redacts Tavily keys, GitHub tokens, Bearer/Basic headers, and passwords.
- [ ] **Artifact Hygiene**: Confirm GitHub Actions artifacts contain only diagnostic logs and JSON reports, zero secrets.
- [ ] **Repository Hygiene**: Verify `.gitignore` strictly protects `.env`, `credentials/`, `secrets/`, and local caches.

### Section D: Data & CRM Integrity Readiness
- [ ] **Google Sheets Reconciliation**: Run `lib.system.reconciliation_engine` and verify zero unresolved critical schema mismatches.
- [ ] **Canonical ID Stability**: Verify existing lead IDs (`LEAD-MAN-[A-F0-9]{6}`) remain stable across deduplication cycles.
- [ ] **Protected Historical States**: Confirm historical outreach records (`SENT`, `BOUNCED`, `SUPPRESSED`, `NEVER_CONFIRMED_SENT`) are immutable.
- [ ] **Verified Baseline Backup**: Verify that at least one backup with valid SHA-256 manifest exists in `data/backups/`.
- [ ] **Dual-Anchor Retention**: Verify backup pruning protects the newest valid and previous valid backup archives.

### Section E: Monitoring & Health Readiness
- [ ] **Zero Open Incidents**: Confirm `IncidentManager` reports 0 open `CRITICAL` or `ERROR` incidents in `data/incidents.json`.
- [ ] **Observability Snapshot**: Verify `SystemHealthMonitor.generate_snapshot()` writes valid machine-readable health metrics.
- [ ] **Alert Cooldowns**: Verify notification throttling rules (`ALERT_COOLDOWN_SECONDS=3600`) are enforced.
- [ ] **Disaster Recovery Sandbox**: Confirm operational procedures in `docs/OPERATIONAL_RECOVERY_RUNBOOK.md` are accessible.

### Section F: Future Outreach Add-on (DEFERRED — DO NOT EXECUTE NOW)

These checks are not prerequisites for the technical rollout, and none authorizes sending by itself. Keep this entire section deferred until the technical stack has passed deployment, backup/restore, safe smoke-test, CRM-integrity, and monitoring checks.

- [ ] **Audience Eligibility**: Review Rule B qualification and evidence for every proposed recipient.
- [ ] **Copy QA**: Confirm messages contain no invented claims, placeholder text, or unsupported website observations.
- [ ] **Send Limits**: Verify channel-specific daily/hourly limits and quota governor behavior.
- [ ] **Legal Review**: Confirm a documented lawful basis and applicable PECR/GDPR requirements for every channel.
- [ ] **Suppression Controls**: Prove opt-outs, bounces, and previous-contact history block repeat outreach.
- [ ] **Channel Ownership**: Verify sending domains, sender identities, Meta permissions, and platform-policy compliance.
- [ ] **Human Approval**: Obtain a separate explicit approval specifically for the outreach addon.

### Section G: Current Technical Rollout (Outreach Must Stay Locked)

1. **Use the guarded deployment workflow** only after the full source regression suite succeeds.
2. **Verify the deployed safety posture** remains:
   ```bash
   export TRAVEL_MODE=true
   export DRY_RUN=true
   export COMMERCIAL_ACTIONS_ENABLED=false
   export AUTOMATED_EMAIL_ENABLED=false
   export TECHNICAL_AUTOMATION_ENABLED=true
   ```
   These are the expected technical-rollout settings; do not flip any of the commercial flags.
3. **Verify durable recovery**: require a GCS backup archive and manifest, checksums, and a passing isolated restore drill.
4. **Run a controlled discovery smoke test** with bounded research. Confirm the execution status and funnel metrics, verify Shadow Mode bypassed Google Sheets/CRM writes, and verify zero outreach dispatches.
5. **Review monitoring and CRM integrity** reports. A failure or unresolved contradiction is a hold, not permission to bypass a gate.

### Section H: Final Add-on Activation (SEPARATE FUTURE DECISION ONLY)

This section is deliberately not part of current technical production activation. Do not run these steps until the founder explicitly requests the addon in a separate decision.

Only after approval, legal review, sender/channel verification, suppression checks, copy QA, and a verified internal pilot plan should a future runbook define any change to `TRAVEL_MODE`, `COMMERCIAL_ACTIONS_ENABLED`, `AUTOMATED_EMAIL_ENABLED`, or `HUMAN_ACTIVATION_CONFIRMED`. Until then, all production and CI workflows must keep commercial actions and automated email disabled, with no live messages.

---

> [!WARNING]
> If any unexpected provider behavior, bounce anomaly, or reconciliation error is observed, engage the emergency kill switch:
> ```bash
> export TRAVEL_MODE=true
> export COMMERCIAL_ACTIONS_ENABLED=false
> export EMAIL_AUTOMATION_KILL_SWITCH=true
> ```
