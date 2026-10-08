# Dripp Media — Production Activation & Commercial Go-Live Checklist (Phase 10.6)

> [!CRITICAL]
> **MANDATORY SAFETY GOVERNANCE**:
> Technical readiness does **NOT** automatically grant permission to execute commercial outreach or send automated email.
> Commercial activation requires explicit, unambiguous **HUMAN OPERATOR CONFIRMATION** and manual unlock of production environment variables.

---

## 1. Governance Architecture & Operating Invariants

| Safety Invariant | Current State | Production Activation Requirement |
| :--- | :--- | :--- |
| **Travel Mode** | `ACTIVE` | Must be explicitly changed to `DISABLED` by operator |
| **Commercial Actions** | `LOCKED` | Must be explicitly set to `COMMERCIAL_ACTIONS_ENABLED=true` |
| **Automated Email** | `DISABLED` | Must be activated via `AUTOMATED_EMAIL_ENABLED=true` |
| **Scheduled Cron** | `DISABLED` | Workflows remain manual `workflow_dispatch` until cron un-commented |
| **Rule B Criteria** | `FROZEN` | 100% Frozen in V1 specification (zero threshold degradation) |
| **CRM Authority** | `Google Sheets` | Remains single source of truth; git never stores CRM state |
| **Human Activation** | `PENDING` | `HUMAN_ACTIVATION_CONFIRMED=true` required before live dispatches |

---

## 2. Staged Activation Checklist Sections

### Section A: Technical Readiness Gate
- [ ] **Deterministic Pre-flight**: Run `.venv/bin/python scripts/production_preflight.py` and verify all 20 gates report `PASS`.
- [ ] **Full Regression Suite**: Run `.venv/bin/pytest -q` and verify 100% test pass rate with 0 failures.
- [ ] **Storage Bootstrap**: Run `python -m lib.system.storage_bootstrap` to confirm all directories (`data/`, `backups/`, `secrets/`) are initialized.
- [ ] **Atomic Writers & Locking**: Verify process locks (`.crm_write.lock`, `.quota_lock.lock`) release cleanly.
- [ ] **Synthetic Staged Pipeline**: Run `StagedPipelineRunner` dry-run and confirm 0 unhandled exceptions.

### Section B: External Account & Provider Readiness
- [ ] **Tavily Research Provider**: Valid, funded API key stored in GitHub Secrets as `TAVILY_API_KEY` (monthly quota verified).
- [ ] **Google Sheets CRM**: Service account credentials configured with edit access to master production spreadsheet.
- [ ] **SMTP / Mail Provider**: Hostinger / Google Workspace SMTP credentials verified (`SMTP_HOST`, `SMTP_PORT=465`, `SMTP_USER`, `SMTP_PASSWORD`).
- [ ] **Meta Graph API**: If Instagram DM or Facebook Messenger is enabled, verify Meta App review status and `META_ACCESS_TOKEN` validity.
- [ ] **Quota Ceilings**: Confirm `DEFAULT_DAILY_LIMITS` in `lib/system/quota_governor.py` match contracted provider tier allowances.

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

### Section F: Commercial Outreach Readiness
- [ ] **Audience Eligibility**: Ensure all outreach-ready candidates strictly satisfy the frozen 8 criteria of Rule B.
- [ ] **Anti-Hallucination Copy QA**: Ensure dynamic outreach message copy contains no placeholder text or template errors.
- [ ] **Daily & Hourly Send Limits**: Verify `EmailGovernor` rate limits (`MAX_EMAIL_BATCH=5`, daily limit <= 20) are enforced.
- [ ] **PECR / GDPR Compliance**: Confirm lawful basis evidence (`LEGITIMATE_INTERESTS`) is attached to every queued recipient.
- [ ] **Suppression List Active**: Verify opt-outs and bounces are completely blocked from re-contact.

### Section G: Human Operator Approval & Activation Sequence
1. **Operator Sign-Off**: The human founder/operator conducts a final review of candidate lists and channel configurations.
2. **Set Activation Flags**:
   ```bash
   export TRAVEL_MODE=false
   export COMMERCIAL_ACTIONS_ENABLED=true
   export AUTOMATED_EMAIL_ENABLED=true
   export DRIPP_RUNTIME_MODE=PRODUCTION
   export HUMAN_ACTIVATION_CONFIRMED=true
   ```
3. **Execution Gate Token**: Arm the target campaign via operator UI or CLI to issue a time-limited (300s) single-use HMAC token.
4. **Controlled Pilot Send**: Execute exactly ONE test message to verified internal recipient address before general dispatch.
5. **Continuous Monitoring**: Inspect `data/incidents.json` and provider dashboards during initial run window.

---

> [!WARNING]
> If any unexpected provider behavior, bounce anomaly, or reconciliation error is observed, engage the emergency kill switch:
> ```bash
> export TRAVEL_MODE=true
> export COMMERCIAL_ACTIONS_ENABLED=false
> export EMAIL_AUTOMATION_KILL_SWITCH=true
> ```
