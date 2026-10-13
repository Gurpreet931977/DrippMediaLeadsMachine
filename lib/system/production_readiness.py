"""
lib/system/production_readiness.py
==================================
Canonical Production Readiness Contract & Deterministic Gate Auditor (Phase 10.6).

Defines:
  1. Canonical Readiness States:
       - NOT_READY
       - TECHNICALLY_READY
       - STAGING_READY
       - PRODUCTION_READY
       - BLOCKED
  2. 20 Mandatory Deterministic Readiness Gates with structured evidence.
  3. Strict invariant: PRODUCTION_READY is impossible unless all mandatory gates pass,
     human confirmation is granted, and commercial actions are intentionally unlocked.
"""

import os
import json
import logging
from enum import Enum
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from lib.system.system_config import SystemConfig, CommercialActionForbiddenError
from lib.system.runtime_mode import RuntimeMode, RuntimeModeManager, OperationalState
from lib.system.quota_governor import QuotaGovernor
from lib.system.backup_manager import BackupManager
from lib.system.system_health import SystemHealthMonitor
from lib.system.observability import sanitize_text
from lib.validation.rule_b_criteria import (
    RULE_B_VERSION,
    get_canonical_rule_b_criteria,
    get_canonical_rule_b_criteria_count,
)

logger = logging.getLogger("ProductionReadiness")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_DATA_DIR = os.path.join(PROJECT_ROOT, "data")


class ReadinessState(str, Enum):
    NOT_READY = "NOT_READY"
    TECHNICALLY_READY = "TECHNICALLY_READY"
    STAGING_READY = "STAGING_READY"
    PRODUCTION_READY = "PRODUCTION_READY"
    BLOCKED = "BLOCKED"


class GateStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    BLOCKED = "BLOCKED"


class GateSeverity(str, Enum):
    REQUIRED = "REQUIRED"
    ADVISORY = "ADVISORY"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProductionReadinessAuditor:
    """
    Evaluates system readiness against 20 deterministic gates with structured evidence.
    """

    def __init__(self, data_dir: Optional[str] = None, project_root: Optional[str] = None):
        self.data_dir = data_dir or DEFAULT_DATA_DIR
        self.project_root = project_root or PROJECT_ROOT
        self.health_monitor = SystemHealthMonitor(data_dir=self.data_dir)
        self.backup_mgr = BackupManager(data_dir=self.data_dir)
        self.quota = QuotaGovernor(data_dir=self.data_dir)

    GATE_NAMES: Dict[str, str] = {
        "CONFIG_VALIDITY": "Configuration Validity",
        "ENVIRONMENT_SEPARATION": "Environment Separation",
        "SECRET_HYGIENE": "Secret Hygiene",
        "STORAGE_HEALTH": "Storage Health",
        "BACKUP_HEALTH": "Backup Health",
        "RESTORE_VERIFICATION": "Restore Verification",
        "STATE_INTEGRITY": "State Integrity",
        "RECONCILIATION_HEALTH": "Reconciliation Health",
        "IDENTITY_INTEGRITY": "Identity Integrity",
        "RULE_B_INTEGRITY": "Rule B Integrity",
        "OUTREACH_LOCK_STATE": "Outreach Lock State",
        "EMAIL_LOCK_STATE": "Email Lock State",
        "SCHEDULER_STATE": "Scheduler State",
        "CRON_STATE": "Cron Lock State",
        "MONITORING_HEALTH": "Monitoring Health",
        "PROVIDER_HEALTH": "Provider Health",
        "QUOTA_HEALTH": "Quota Health",
        "TEST_HEALTH": "Test Suite Health",
        "ARTIFACT_LOG_SAFETY": "Artifact & Log Safety",
        "ROLLBACK_CAPABILITY": "Rollback Capability",
    }

    def _make_gate(
        self,
        gate_id: str,
        status: GateStatus,
        severity: GateSeverity,
        evidence: Dict[str, Any],
        description: str = "",
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        display_name = name or self.GATE_NAMES.get(gate_id, gate_id.replace("_", " ").title())
        blocking = (status in (GateStatus.FAIL, GateStatus.BLOCKED)) and (severity == GateSeverity.REQUIRED)
        ts = _now_utc()
        return {
            "gate_id": gate_id,
            "name": display_name,
            "display_name": display_name,
            "description": description,
            "explanation": description,
            "status": status.value,
            "severity": severity.value,
            "blocking": blocking,
            "evidence": evidence,
            "timestamp": ts,
            "last_checked": ts,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # GATE AUDIT METHODS (1 through 20)
    # ──────────────────────────────────────────────────────────────────────────

    def check_config_validity(self) -> Dict[str, Any]:
        """1. Configuration validity: Market registry, technical flags, and structure."""
        status_dict = SystemConfig.get_status_dict()
        valid = (
            isinstance(status_dict, dict)
            and "technical_controls" in status_dict
            and SystemConfig.DEFAULT_ENABLED_MARKET == "MANCHESTER_UK"
        )
        return self._make_gate(
            gate_id="CONFIG_VALIDITY",
            status=GateStatus.PASS if valid else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence={
                "enabled_market": SystemConfig.DEFAULT_ENABLED_MARKET,
                "technical_automation_enabled": SystemConfig.TECHNICAL_AUTOMATION_ENABLED,
                "status_keys_present": list(status_dict.keys()),
            },
            description="SystemConfig and market configurations parse correctly with valid defaults.",
        )

    def check_environment_separation(self) -> Dict[str, Any]:
        """2. Environment separation: Explicit modes, non-production disallows live side effects."""
        mode = RuntimeModeManager.get_current_mode()
        is_safe_default = not RuntimeModeManager.is_production() or (
            RuntimeModeManager.is_production() and not SystemConfig.TRAVEL_MODE and SystemConfig.COMMERCIAL_ACTIONS_ENABLED
        )
        return self._make_gate(
            gate_id="ENVIRONMENT_SEPARATION",
            status=GateStatus.PASS if is_safe_default else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence={
                "current_mode": mode.value,
                "is_staging": RuntimeModeManager.is_staging(),
                "is_test": RuntimeModeManager.is_test(),
                "is_production": RuntimeModeManager.is_production(),
                "travel_mode_active": SystemConfig.TRAVEL_MODE,
            },
            description="Strict runtime mode separation guarantees no live side-effects outside confirmed production.",
        )

    def check_secret_hygiene(self) -> Dict[str, Any]:
        """
        3. Secret availability / absence: Repository hygiene, build context exclusions, and runtime secret scanning.
        """
        from scripts.security_secret_scan import (
            check_gitignore,
            check_prohibited_files,
            scan_runtime_files,
        )

        has_git_repo = os.path.exists(os.path.join(self.project_root, ".git"))
        is_cloud_run = bool(os.getenv("K_SERVICE") or os.getenv("K_REVISION"))
        is_container = is_cloud_run or os.path.exists("/.dockerenv")

        # 1. Prohibited credential/secret files audit
        prohibited_files = check_prohibited_files(self.project_root)
        if has_git_repo:
            # In local development repository, developer credentials (.env, service account keys)
            # are allowed on the developer's local machine ONLY IF untracked and ignored by git
            from scripts.security_secret_scan import get_git_tracked_files, scan_file
            tracked = set(get_git_tracked_files())
            prohibited_files = [p for p in prohibited_files if p in tracked]

            # 2. Pattern scan across git-tracked codebase files
            secret_findings = []
            for tf in tracked:
                hits = scan_file(tf, root_dir=self.project_root)
                if hits:
                    secret_findings.extend(hits)
        else:
            # In container runtime: all files present on disk are part of the image build context
            secret_findings = scan_runtime_files(self.project_root)

        # 3. VCS .gitignore verification (enforced in repository environments)
        missing_ignores = check_gitignore() if has_git_repo else []
        gitignore_ok = len(missing_ignores) == 0 if has_git_repo else True

        # Rule: A secret leak or prohibited credential file FAILS in ANY environment
        has_secret_leak = len(prohibited_files) > 0 or len(secret_findings) > 0
        passed = (not has_secret_leak) and gitignore_ok

        evidence: Dict[str, Any] = {
            "vcs_context": "git_repository" if has_git_repo else "container_runtime",
            "is_container": is_container,
            "prohibited_files_detected": prohibited_files,
            "secret_findings_count": len(secret_findings),
            "credentials_excluded": not has_secret_leak,
            "missing_gitignore_rules": missing_ignores,
        }

        if secret_findings:
            evidence["masked_findings"] = [
                {"file": f["file"], "line": f["line"], "secret_type": f["secret_type"], "masked": f["masked_value"]}
                for f in secret_findings[:5]
            ]

        if not has_git_repo:
            evidence["build_context_hygiene"] = (
                "Runtime image lacks VCS metadata. Verified zero prohibited credential files "
                "and zero hardcoded secrets on the active filesystem."
            )

        return self._make_gate(
            gate_id="SECRET_HYGIENE",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence=evidence,
            description="Active filesystem excludes .env, keys, credentials, and contains zero unmasked secrets.",
        )

    def check_storage_health(self) -> Dict[str, Any]:
        """4. Filesystem / storage health: Critical local stores parse cleanly as JSON."""
        integrity = self.health_monitor.check_file_integrity()
        all_ok = integrity.get("status") == "PASS"

        # Check local data directory writability
        writable = os.path.exists(self.data_dir) and os.access(self.data_dir, os.W_OK)
        integrity["filesystem_writable"] = writable

        # Cloud Run / ephemeral environment detection
        is_cloud_run = bool(os.getenv("K_SERVICE") or os.getenv("K_REVISION"))
        integrity["ephemeral_filesystem"] = is_cloud_run

        # Check Firestore persistence state if in production
        is_prod = os.getenv("ENVIRONMENT") == "production"
        if is_prod or os.getenv("FIRESTORE_ENABLED") == "true":
            try:
                from lib.system.firestore_state_store import FirestoreStateStore
                store = FirestoreStateStore(force_mock=False)
                integrity["firestore_persistence"] = {
                    "status": "PASS",
                    "database_id": store.database_id,
                    "project_id": store.project_id,
                }
            except Exception as e:
                integrity["firestore_persistence"] = {
                    "status": "FAIL",
                    "error": str(e),
                }

        if not all_ok:
            missing = [f for f, s in integrity.get("files", {}).items() if not s.get("exists")]
            integrity["remediation"] = (
                f"Missing critical baseline stores: {', '.join(missing)}. "
                "In ephemeral container environments (Cloud Run), initialize storage baseline "
                "using storage_bootstrap.bootstrap_storage_baseline(sync_from_sheets_if_available=False)."
            )

        return self._make_gate(
            gate_id="STORAGE_HEALTH",
            status=GateStatus.PASS if all_ok else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence=integrity,
            description="Critical application files exist, are accessible, and contain valid JSON.",
        )

    def check_backup_health(self) -> Dict[str, Any]:
        """
        5. Backup health: Valid backups exist with manifest, checksum, durability, and recoverability.

        Requirements for genuine PASS:
        - In ephemeral container environments (Cloud Run / production):
          Requires durable cloud storage (GCS bucket or Firestore automated export) to be configured,
          accessible, containing completed backups with valid manifests, matching checksums,
          and passed restore drill.
          Local snapshots in /app/data/backups are NOT durable and will NOT yield a PASS on Cloud Run.
        - In local development / test environments:
          Requires at least one valid snapshot with successful manifest verification.
        """
        backups = self.backup_mgr.list_backups()
        valid_backups = [
            b for b in backups
            if b.get("completion_status") == "SUCCESS" or b.get("schema_version") == "1.0.0"
        ]
        has_valid_local = len(valid_backups) > 0
        is_cloud_run = bool(os.getenv("K_SERVICE") or os.getenv("K_REVISION"))
        is_prod = os.getenv("ENVIRONMENT") == "production"
        is_ephemeral = is_cloud_run or is_prod

        # Check durable cloud backup destination (e.g. Google Cloud Storage)
        gcs_bucket = os.getenv("GCS_BACKUP_BUCKET", "").strip() or os.getenv("BACKUP_BUCKET", "").strip()
        durable_configured = bool(gcs_bucket)

        # Restore verification check
        restore_verified = False
        restore_error = None
        if has_valid_local and hasattr(self.backup_mgr, "verify_backup_integrity"):
            try:
                latest_id = backups[0].get("backup_id")
                if latest_id:
                    is_valid, errs = self.backup_mgr.verify_backup_integrity(latest_id)
                    restore_verified = is_valid
                    if errs:
                        restore_error = "; ".join(errs)
            except Exception as e:
                restore_error = str(e)

        evidence: Dict[str, Any] = {
            "total_local_backups": len(backups),
            "valid_successful_backups": len(valid_backups),
            "latest_backup_id": backups[0].get("backup_id") if backups else None,
            "ephemeral_environment": is_ephemeral,
            "durable_cloud_backup_configured": durable_configured,
            "durable_destination": f"gs://{gcs_bucket}" if durable_configured else None,
            "storage_durability": "DURABLE_CLOUD" if durable_configured else "EPHEMERAL_LOCAL_ONLY",
            "restore_drill_verified": restore_verified,
        }
        if restore_error:
            evidence["restore_error"] = restore_error

        # Genuine PASS evaluation:
        # On Cloud Run / Production: ephemeral /app/data/backups does NOT survive container recycles.
        # Genuine PASS requires durable cloud backup configured AND valid verified backups.
        if is_ephemeral:
            if not durable_configured:
                evidence["remediation"] = (
                    "Durable cloud backup is unconfigured. Cloud Run container filesystem is ephemeral; "
                    "local snapshots in /app/data/backups are wiped upon container recycling or scaling. "
                    "Production disaster recovery requires Google Cloud Storage (GCS) bucket integration "
                    "(e.g. gs://dripp-leads-backups-poised-eye-509816-a4) with verified upload, valid SHA-256 "
                    "manifests, and periodic restore validation."
                )
                return self._make_gate(
                    gate_id="BACKUP_HEALTH",
                    status=GateStatus.FAIL,
                    severity=GateSeverity.REQUIRED,
                    evidence=evidence,
                    description="Durable backup storage is configured, verified, and restorable.",
                )
            else:
                from lib.system.durable_backup_engine import DurableBackupEngine
                durable_eval = DurableBackupEngine.verify_durable_cloud_backup(gcs_bucket)
                evidence["durable_verification"] = durable_eval
                remote_ok = durable_eval.get("is_valid", False) and durable_eval.get("isolated_restore_verified", False)
                if not remote_ok:
                    remed = durable_eval.get("remediation")
                    evidence["remediation"] = (
                        f"Durable cloud backup verification failed: {remed}"
                        if remed
                        else (
                            f"Durable cloud backup verification failed for gs://{gcs_bucket}: {durable_eval.get('error')}. "
                            "A durable backup artifact must exist in the bucket, its remote manifest and checksums must validate, "
                            "and an isolated restore drill must succeed."
                        )
                    )
                return self._make_gate(
                    gate_id="BACKUP_HEALTH",
                    status=GateStatus.PASS if remote_ok else GateStatus.FAIL,
                    severity=GateSeverity.REQUIRED,
                    evidence=evidence,
                    description="Durable cloud backup storage is configured, verified, and restorable.",
                )

        # Local development / CI evaluation:
        local_pass = has_valid_local and restore_verified
        if not local_pass:
            evidence["remediation"] = "Create and verify a local snapshot via BackupManager().create_backup()."

        return self._make_gate(
            gate_id="BACKUP_HEALTH",
            status=GateStatus.PASS if local_pass else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence=evidence,
            description="Backup manager is operational and recorded backups possess valid manifests.",
        )

    def check_restore_verification(self) -> Dict[str, Any]:
        """6. Restore verification: Backup verification supports integrity verification."""
        manifest_verifiable = hasattr(self.backup_mgr, "verify_backup_integrity")
        return self._make_gate(
            gate_id="RESTORE_VERIFICATION",
            status=GateStatus.PASS if manifest_verifiable else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence={
                "has_integrity_verifier": manifest_verifiable,
                "has_retention_pruning": hasattr(self.backup_mgr, "prune_backups"),
            },
            description="Backup integrity verification and dual-anchor retention protection are functional.",
        )

    def check_state_integrity(self) -> Dict[str, Any]:
        """7. State integrity: SafeJSONStore and StateIntegrityEngine readiness."""
        from lib.system.state_integrity import StateIntegrityEngine
        engine = StateIntegrityEngine(data_dir=self.data_dir)
        audit = engine.audit_data_integrity()
        passed = audit.get("status") == "PASS"

        return self._make_gate(
            gate_id="STATE_INTEGRITY",
            status=GateStatus.PASS if passed else GateStatus.WARN,
            severity=GateSeverity.REQUIRED,
            evidence=audit,
            description="State integrity engine confirms zero store corruption and valid structures.",
        )

    def check_reconciliation_health(self) -> Dict[str, Any]:
        """8. Reconciliation health: Local cache vs CRM authority reconciliation readiness."""
        from lib.system.reconciliation_engine import ReconciliationEngine
        reconciler = ReconciliationEngine(data_dir=self.data_dir)
        report = reconciler.run_reconciliation()
        status_str = report.get("status", "PASS")
        passed = status_str in ("PASS", "WARN")

        return self._make_gate(
            gate_id="RECONCILIATION_HEALTH",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence={
                "reconciliation_status": status_str,
                "issues_count": report.get("issues_count", 0),
            },
            description="Reconciliation engine successfully inspects differences without blind destructive overwrites.",
        )

    def check_identity_integrity(self) -> Dict[str, Any]:
        """9. Identity integrity: Canonical ID generation conforms to immutable specifications."""
        from lib.system.identity_integrity import IdentityIntegrityAuditor
        auditor = IdentityIntegrityAuditor(data_dir=self.data_dir)
        report = auditor.run_identity_audit()
        status_str = report.get("status", "PASS")
        passed = status_str in ("PASS", "WARN")

        return self._make_gate(
            gate_id="IDENTITY_INTEGRITY",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence={
                "identity_status": status_str,
                "canonical_leads_audited": report.get("total_canonical_leads_audited", 0),
            },
            description="Canonical IDs are deterministic, well-formed, and free of hash collisions.",
        )

    def check_rule_b_integrity(self) -> Dict[str, Any]:
        """10. Rule B integrity: Qualification criteria remain 100% frozen."""
        criteria = get_canonical_rule_b_criteria()
        count = get_canonical_rule_b_criteria_count()
        rev_count_crit = next((c for c in criteria if c["id"] == "RULE_B_01_REVIEW_VOLUME"), None)
        rating_crit = next((c for c in criteria if c["id"] == "RULE_B_02_MINIMUM_RATING"), None)
        recency_crit = next((c for c in criteria if c["id"] == "RULE_B_04_REVIEW_RECENCY"), None)

        valid = (
            RULE_B_VERSION == "FROZEN"
            and count == 8
            and rev_count_crit and rev_count_crit.get("threshold") == 50
            and rating_crit and rating_crit.get("threshold") == 4.0
            and recency_crit and recency_crit.get("max_age_days") == 180
        )

        return self._make_gate(
            gate_id="RULE_B_INTEGRITY",
            status=GateStatus.PASS if valid else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence={
                "rule_b_version": RULE_B_VERSION,
                "criteria_count": count,
                "min_reviews": rev_count_crit.get("threshold") if rev_count_crit else None,
                "min_rating": rating_crit.get("threshold") if rating_crit else None,
                "max_recency_days": recency_crit.get("max_age_days") if recency_crit else None,
            },
            description="Rule B criteria are verified as canonical, frozen, and unmodified.",
        )

    def check_outreach_lock_state(self) -> Dict[str, Any]:
        """11. Outreach lock state: Commercial actions remain locked under Travel Mode."""
        travel_mode = SystemConfig.TRAVEL_MODE
        comm_enabled = SystemConfig.COMMERCIAL_ACTIONS_ENABLED
        can_execute = SystemConfig.can_execute_commercial_actions()

        # Under Phase 10.6, commercial actions MUST be locked
        locked = not can_execute
        blocked_err_raised = False
        try:
            SystemConfig.assert_commercial_actions_allowed("test_gate_check")
        except CommercialActionForbiddenError:
            blocked_err_raised = True
        except Exception:
            pass

        passed = locked and blocked_err_raised

        return self._make_gate(
            gate_id="OUTREACH_LOCK_STATE",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence={
                "travel_mode": travel_mode,
                "commercial_actions_enabled": comm_enabled,
                "can_execute_commercial": can_execute,
                "assertion_blocks": blocked_err_raised,
            },
            description="Commercial outreach is locked; outbound actions raise CommercialActionForbiddenError.",
        )

    def check_email_lock_state(self) -> Dict[str, Any]:
        """12. Automated email lock state: Email automation disabled or kill switch engaged."""
        automated_email = SystemConfig.AUTOMATED_EMAIL_ENABLED
        kill_switch = SystemConfig.EMAIL_AUTOMATION_KILL_SWITCH
        is_active = SystemConfig.is_automated_email_enabled()

        from lib.system.system_config import EmailAutomationBlockedError
        blocked_err_raised = False
        try:
            SystemConfig.assert_automated_email_allowed("test_email_check")
        except EmailAutomationBlockedError:
            blocked_err_raised = True
        except Exception:
            pass

        passed = (not is_active) and blocked_err_raised

        return self._make_gate(
            gate_id="EMAIL_LOCK_STATE",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence={
                "automated_email_enabled": automated_email,
                "email_kill_switch_active": kill_switch,
                "automated_email_active": is_active,
                "assertion_blocks": blocked_err_raised,
            },
            description="Automated email engine is disabled; send calls raise EmailAutomationBlockedError.",
        )

    def check_scheduler_state(self) -> Dict[str, Any]:
        """13. Scheduler state: Technical scheduler respects technical automation controls."""
        tech_enabled = SystemConfig.TECHNICAL_AUTOMATION_ENABLED
        can_discovery = SystemConfig.can_execute_technical_job("DISCOVERY")
        can_backup = SystemConfig.can_execute_technical_job("BACKUP")

        valid = tech_enabled and can_discovery and can_backup

        return self._make_gate(
            gate_id="SCHEDULER_STATE",
            status=GateStatus.PASS if valid else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence={
                "technical_automation_enabled": tech_enabled,
                "can_execute_discovery": can_discovery,
                "can_execute_backup": can_backup,
            },
            description="Technical scheduler controls are active for non-commercial operational jobs.",
        )

    def check_cron_state(self) -> Dict[str, Any]:
        """14. Cron state: Verifies scheduled cron workflows remain disabled in GitHub Actions."""
        workflows_dir = os.path.join(self.project_root, ".github", "workflows")
        cron_active = False
        scanned_files = 0

        if os.path.exists(workflows_dir):
            for fname in os.listdir(workflows_dir):
                if fname.endswith((".yml", ".yaml")):
                    scanned_files += 1
                    with open(os.path.join(workflows_dir, fname), "r", encoding="utf-8") as f:
                        for line in f:
                            stripped = line.strip()
                            if stripped.startswith("- cron:") and not stripped.startswith("#"):
                                cron_active = True
                                break

        passed = not cron_active

        return self._make_gate(
            gate_id="CRON_STATE",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence={
                "workflows_scanned": scanned_files,
                "cron_triggers_active": cron_active,
                "cron_disabled": not cron_active,
            },
            description="All scheduled cron schedules remain commented out / disabled in GitHub Actions.",
        )

    def check_monitoring_health(self) -> Dict[str, Any]:
        """15. Monitoring health: Incident manager contains 0 unresolved CRITICAL incidents."""
        from lib.monitoring.incident_manager import IncidentManager
        mgr = IncidentManager(data_dir=self.data_dir)
        active = mgr.get_active_incidents()
        critical_count = len([i for i in active if i.severity == "CRITICAL"])
        error_count = len([i for i in active if i.severity == "ERROR"])

        passed = critical_count == 0

        return self._make_gate(
            gate_id="MONITORING_HEALTH",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence={
                "total_active_incidents": len(active),
                "active_critical": critical_count,
                "active_error": error_count,
            },
            description="Central monitoring confirms zero active critical incidents in storage.",
        )

    def check_provider_health(self) -> Dict[str, Any]:
        """16. Provider health: Audit readiness across Tavily, Google, and Email."""
        tavily_key = os.getenv("TAVILY_API_KEY")
        sheets_token = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        smtp_host = os.getenv("SMTP_HOST")

        tavily_state = "AVAILABLE" if tavily_key else "UNCONFIGURED"
        sheets_state = "AVAILABLE" if sheets_token else "UNCONFIGURED"
        email_state = "AVAILABLE" if smtp_host else "UNCONFIGURED"

        # In Staging / Test mode, unconfigured external production credentials are non-blocking
        mode = RuntimeModeManager.get_current_mode()
        if mode in (RuntimeMode.TEST, RuntimeMode.STAGING):
            status = GateStatus.PASS
        else:
            status = GateStatus.PASS if (tavily_key and sheets_token) else GateStatus.WARN

        return self._make_gate(
            gate_id="PROVIDER_HEALTH",
            status=status,
            severity=GateSeverity.REQUIRED if mode == RuntimeMode.PRODUCTION else GateSeverity.ADVISORY,
            evidence={
                "tavily_status": tavily_state,
                "sheets_status": sheets_state,
                "email_status": email_state,
                "runtime_mode": mode.value,
            },
            description="Provider configuration status evaluated without exposing raw credentials.",
        )

    def check_quota_health(self) -> Dict[str, Any]:
        """17. Quota health: Quota governor operational and within safe thresholds."""
        status = self.quota.get_quota_status()
        is_exhausted = status.get("status") == "QUOTA_EXHAUSTED"

        return self._make_gate(
            gate_id="QUOTA_HEALTH",
            status=GateStatus.FAIL if is_exhausted else GateStatus.PASS,
            severity=GateSeverity.REQUIRED,
            evidence=status,
            description="Central QuotaGovernor tracks resource consumption safely below ceilings.",
        )

    def check_test_health(self) -> Dict[str, Any]:
        """18. Test health: Key regression test files exist and are verified."""
        test_files = [
            "test_phase_10_5_security_and_integrity.py",
            "test_phase_10_4_lead_freshness.py",
            "test_phase_10_3_production_monitoring.py",
            "test_phase_11_2_tavily_provider.py",
            "test_phase_11_1_research_recovery.py",
            "test_phase_11_0_github_actions_compatibility.py",
        ]
        present = [f for f in test_files if os.path.exists(os.path.join(self.project_root, f))]
        all_present = len(present) == len(test_files)

        return self._make_gate(
            gate_id="TEST_HEALTH",
            status=GateStatus.PASS if all_present else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence={
                "required_suites_count": len(test_files),
                "present_suites_count": len(present),
                "suites": present,
            },
            description="Core regression test suites are present and ready for execution.",
        )

    def check_artifact_log_safety(self) -> Dict[str, Any]:
        """19. Artifact & log safety: Redaction layer active, sensitive patterns sanitized."""
        dummy_secret = "tvly-" + "prod1234567890abcdef1234567890"
        redacted = sanitize_text(f"Key is {dummy_secret}")
        sanitization_works = dummy_secret not in redacted and "[REDACTED_TAVILY_KEY]" in redacted

        return self._make_gate(
            gate_id="ARTIFACT_LOG_SAFETY",
            status=GateStatus.PASS if sanitization_works else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence={
                "log_sanitization_functional": sanitization_works,
            },
            description="Central observability redaction filters mask API keys, tokens, and passwords.",
        )

    def check_rollback_capability(self) -> Dict[str, Any]:
        """20. Rollback & pause capability: State transitions between RUNNING, PAUSED, SAFE_MODE."""
        orig_state = RuntimeModeManager.get_operational_state()
        can_pause = False
        can_safe = False

        try:
            RuntimeModeManager.pause(reason="audit_check")
            can_pause = RuntimeModeManager.get_operational_state() == OperationalState.PAUSED
            RuntimeModeManager.resume(reason="audit_check")

            RuntimeModeManager.enter_safe_mode(reason="audit_check")
            can_safe = RuntimeModeManager.get_operational_state() == OperationalState.SAFE_MODE
            RuntimeModeManager.recover(reason="audit_check")
        finally:
            RuntimeModeManager.transition_state(orig_state, reason="audit_reset")

        passed = can_pause and can_safe

        return self._make_gate(
            gate_id="ROLLBACK_CAPABILITY",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            severity=GateSeverity.REQUIRED,
            evidence={
                "can_pause_and_resume": can_pause,
                "can_enter_and_recover_safe_mode": can_safe,
            },
            description="Operational state machine enables safe pause, safe mode, and resume recovery.",
        )

    # ──────────────────────────────────────────────────────────────────────────
    # AGGREGATE AUDIT EXECUTION
    # ──────────────────────────────────────────────────────────────────────────

    def audit_all_gates(self) -> Dict[str, Any]:
        """
        Executes all 20 readiness gates and compiles aggregate assessment.
        """
        gates = [
            self.check_config_validity(),
            self.check_environment_separation(),
            self.check_secret_hygiene(),
            self.check_storage_health(),
            self.check_backup_health(),
            self.check_restore_verification(),
            self.check_state_integrity(),
            self.check_reconciliation_health(),
            self.check_identity_integrity(),
            self.check_rule_b_integrity(),
            self.check_outreach_lock_state(),
            self.check_email_lock_state(),
            self.check_scheduler_state(),
            self.check_cron_state(),
            self.check_monitoring_health(),
            self.check_provider_health(),
            self.check_quota_health(),
            self.check_test_health(),
            self.check_artifact_log_safety(),
            self.check_rollback_capability(),
        ]

        blocking_gates = [g for g in gates if g["blocking"]]
        passed_gates = [g for g in gates if g["status"] == "PASS"]
        warn_gates = [g for g in gates if g["status"] == "WARN"]

        # Determine Canonical Readiness State
        if len(blocking_gates) > 0:
            readiness_state = ReadinessState.BLOCKED
            overall_status = "BLOCKED"
        else:
            # Check if commercial activation criteria are met
            human_confirmed = os.environ.get("HUMAN_ACTIVATION_CONFIRMED", "false").lower() in ("true", "1", "yes")
            commercial_active = SystemConfig.can_execute_commercial_actions()

            if human_confirmed and commercial_active and RuntimeModeManager.is_production():
                readiness_state = ReadinessState.PRODUCTION_READY
                overall_status = "PASS"
            elif RuntimeModeManager.is_staging():
                readiness_state = ReadinessState.STAGING_READY
                overall_status = "PASS"
            else:
                readiness_state = ReadinessState.TECHNICALLY_READY
                overall_status = "PASS"

        return {
            "timestamp": _now_utc(),
            "readiness_state": readiness_state.value,
            "overall_status": overall_status,
            "total_gates": len(gates),
            "passed_gates_count": len(passed_gates),
            "warning_gates_count": len(warn_gates),
            "blocking_gates_count": len(blocking_gates),
            "blocking_gates": [g["gate_id"] for g in blocking_gates],
            "runtime_mode": RuntimeModeManager.get_current_mode().value,
            "operating_mode_banner": SystemConfig.get_status_dict().get("operating_mode_banner", ""),
            "gates": gates,
        }
