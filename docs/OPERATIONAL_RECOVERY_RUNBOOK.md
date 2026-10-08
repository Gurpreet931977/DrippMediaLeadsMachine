# Operational Disaster Recovery Runbook
**Dripp Media Technical Lead Acquisition System**

---

## 1. Overview & Core Recovery Principles
This document defines standard operating procedures for detecting state corruption or pipeline failures, inspecting valid backups, performing isolated test restores, running reconciliation against Google Sheets CRM authority, and safely restoring system operational health.

### Strict Safety Invariants:
- **`TRAVEL_MODE = ACTIVE`**: Must remain enabled during all diagnostic and recovery procedures.
- **`COMMERCIAL_ACTIONS_ENABLED = FALSE`**: Commercial operations, automated outreach, and email dispatch must remain strictly locked.
- **`Google Sheets Authority`**: Google Sheets remains the authoritative CRM source of truth.
- **`Zero Secret Exposure`**: Never log, echo, or write secrets into recovery logs or reports.

---

## 2. Detecting System Anomalies & Corruption
Run the system health and integrity check:
```bash
python scripts/system_health_check.py
```
Or check individual data store integrity via the Python API:
```python
from lib.system.state_integrity import StateIntegrityEngine
engine = StateIntegrityEngine()
audit = engine.audit_data_integrity()
print("Health Status:", audit["status"])
print("Corrupt stores:", audit["corrupt_files"])
```

### Anomaly Triggers:
1. **Unparseable JSON or Truncated Files**: Indicated by `CORRUPT` in store audit.
2. **Missing Authoritative Files**: Indicated by missing critical stores in `data/`.
3. **Cross-System Reconciliation Inconsistencies**: Discrepancies between Google Sheets and local cache.
4. **Monitoring Incidents**: Any incident of type `STATE_CORRUPTION`, `BACKUP_FAILURE`, or `RECONCILIATION_FAILURE` emitted in `data/incidents.json`.

---

## 3. Inspecting Available Backups
All automated snapshots reside in `data/backups/`.
To inspect and list backups from newest to oldest:
```python
from lib.system.backup_manager import BackupManager
manager = BackupManager()
backups = manager.list_backups()
for b in backups[:5]:
    print(f"ID: {b['backup_id']} | Created: {b['created_at']} | Status: {b.get('completion_status')} | Files: {b['files_count']}")
```
Verify the integrity of a specific backup:
```python
is_valid, errors = manager.verify_backup_integrity("backup_YYYYMMDD_HHMMSS_snapshot_RUN-...")
print("Valid:", is_valid, "Errors:", errors)
```

---

## 4. Performing Isolated Test Restores
**Never restore directly into active production `data/` without verifying in an isolated sandbox.**

Procedure:
```python
import tempfile
from lib.system.backup_manager import BackupManager
from lib.system.state_integrity import StateIntegrityEngine

# 1. Initialize BackupManager
manager = BackupManager()
latest_backup_id = manager.list_backups()[0]["backup_id"]

# 2. Restore into isolated temporary directory
with tempfile.TemporaryDirectory() as temp_sandbox:
    restore_plan = manager.restore_backup(
        backup_id=latest_backup_id,
        target_dir=temp_sandbox,
        dry_run=False,
    )
    print("Files restored to sandbox:", restore_plan["restored_count"])

    # 3. Audit restored sandbox integrity
    engine = StateIntegrityEngine(data_dir=temp_sandbox)
    audit = engine.audit_data_integrity()
    assert audit["is_healthy"], f"Sandbox restore audit failed: {audit['corrupt_files']}"
    print("Sandbox audit verified successfully.")
```

---

## 5. Running Post-Restore Reconciliation
Reconcile the restored state against Google Sheets CRM authority:
```python
from lib.system.state_integrity import StateIntegrityEngine
import json

# Load restored leads
with open("data/cache_sheets_leads.json", "r") as f:
    local_leads = json.load(f).get("leads", [])

# Reconcile against live Google Sheets
# (Fetch sheets_leads from Google Sheets or SheetsGateway)
reconciliation = StateIntegrityEngine.reconcile_sheets_and_local_cache(
    sheets_leads=sheets_leads,
    local_leads=local_leads,
)
print("Reconciliation Discrepancies:", reconciliation["total_discrepancies"])
```

---

## 6. Verifying Canonical IDs & Identity Consistency
Confirm canonical lead IDs:
```python
from lib.system.identity_integrity import IdentityIntegrityAuditor
auditor = IdentityIntegrityAuditor(data_dir="data")
result = auditor.run_identity_audit()
assert result["status"] == "PASS", f"Identity issues found: {result['issues']}"
```
- Verify all lead IDs match `^LEAD-[A-Z0-9]+-[A-F0-9]{6,}$`.
- Verify zero research IDs (`RES-*`, `OSM-*`, `GOSOM-*`) leaked into canonical lead ID fields.
- Verify zero accidental entity duplicates.

---

## 7. Confirming Protected Outreach & Suppression States
Ensure historical states were not compromised during restore:
```python
from lib.system.state_integrity import StateIntegrityEngine

# Verify that all historical records maintain immutable protected statuses
ok, violations = StateIntegrityEngine.verify_protected_outreach_safety(
    baseline_leads=baseline_snapshot_leads,
    evaluated_leads=restored_leads,
)
assert ok, f"Protected state violations: {violations}"
```
Mandatory checks:
- Leads with `SENT` remain `SENT`.
- Leads with `BOUNCED` remain `BOUNCED`.
- Leads with `SUPPRESSED` remain `SUPPRESSED`.
- Message history in `message_history.json` and suppression records in `suppression_list.json` are intact.
- Zero historical leads are promoted to `OUTREACH_READY` or active campaigns.

---

## 8. When NOT to Automatically Restore
**Stop and do not auto-restore if:**
1. **Google Sheets has newer authoritative rows** that were never captured in any local backup. In this case, re-bootstrap local cache from Google Sheets rather than restoring an older local snapshot.
2. **Backup checksum fails**: Any mismatch between manifest SHA-256 and stored file hashes.
3. **Lock conflict exists**: `data/.orchestrator.lock` or `data/.crm_write.lock` is actively held by a live process. Wait for completion or inspect PID.
4. **Unresolved Identity Conflict**: If multiple distinct businesses are mapped to the same ID, manual operator review is mandatory.

---

## 9. Enforcing Commercial Lock During Recovery
Always verify safety assertions before and after recovery actions:
```python
from lib.system.system_config import SystemConfig

assert SystemConfig.TRAVEL_MODE, "CRITICAL: TRAVEL_MODE must be ACTIVE"
assert not SystemConfig.COMMERCIAL_ACTIONS_ENABLED, "CRITICAL: Commercial actions must be LOCKED"
assert not SystemConfig.AUTOMATED_EMAIL_ENABLED, "CRITICAL: Automated email must be DISABLED"
assert not SystemConfig.can_execute_commercial_actions(), "CRITICAL: Outbound actions forbidden"
print("✓ Safety invariants verified: All commercial actions locked.")
```
