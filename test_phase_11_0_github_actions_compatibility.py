"""
test_phase_11_0_github_actions_compatibility.py
===============================================
Comprehensive Test Suite for Phase 11.0: GitHub Actions Production Readiness.

Tests:
  1.  Actions environment detection
  2.  Ubuntu / cross-platform path handling
  3.  Required environment variable validation
  4.  Missing secret graceful handling
  5.  Dry-run safety invariants (zero mutations, zero sends)
  6.  Travel Mode enforcement
  7.  Commercial action kill switch
  8.  Automated email outreach locked
  9.  Concurrency controls & cross-process locking
  10. Ephemeral runner persistence & baseline bootstrapping
  11. Gosom scraper availability & compatibility
  12. Google Sheets authentication configuration (JSON string & file)
  13. Artifact & report generation (JSON & Markdown)
  14. Failure behavior & non-zero exit codes
  15. Secret scanning & redaction
  16. Workflow file schema validity & least-privilege permissions
"""

import os
import sys
import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import DiscoveredBusiness

from lib.system.system_config import (
    SystemConfig,
    CommercialActionForbiddenError,
    EmailAutomationBlockedError,
)
from lib.system.config_validator import ConfigValidator
from lib.system.file_lock import FileLock, orchestrator_lock, LockTimeoutError
from lib.system.storage_bootstrap import bootstrap_storage_baseline, CRITICAL_STORE_DEFAULTS
from lib.system.system_health import SystemHealthMonitor
from scripts.security_secret_scan import scan_file, check_gitignore, PATTERNS
from scripts.run_technical_pipeline import assert_safety_invariants, run_pipeline
from scripts.run_freshness_check import run_freshness
from scripts.parse_cloud_run_sa import parse_runtime_service_account
from sheets_sync import get_sheet_client, get_worksheet


def _make_synthetic_candidate() -> DiscoveredBusiness:
    return DiscoveredBusiness(
        company_name="Rudy's Pizza Napoletana",
        category="Restaurant",
        city="Manchester",
        target_country="United Kingdom",
        detected_country="United Kingdom",
        country_status="COUNTRY_MATCH",
        address="9 Cotton St, Ancoats, Manchester M4 5BF",
        postcode="M4 5BF",
        phone="01618343456",
        raw_website="",
        lat=53.4831,
        lon=-2.2312,
        discovery_source="FIXTURE",
        operational_status="ACTIVE_CONFIRMED",
        review_count=120,
        rating=4.6,
        latest_review_date=datetime.now(timezone.utc).date().isoformat(),
    )


class TestGitHubActionsCompatibility(unittest.TestCase):

    def setUp(self):
        # Enforce baseline production safety state
        SystemConfig.TRAVEL_MODE = True
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = False
        SystemConfig.AUTOMATED_EMAIL_ENABLED = False
        SystemConfig.EMAIL_AUTOMATION_KILL_SWITCH = False
        SystemConfig.TECHNICAL_AUTOMATION_ENABLED = True

    # -------------------------------------------------------------------------
    # 1. Actions Environment Detection
    # -------------------------------------------------------------------------
    def test_01_actions_environment_detection(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_OS": "Linux"}):
            self.assertEqual(os.environ.get("GITHUB_ACTIONS"), "true")
            self.assertEqual(os.environ.get("RUNNER_OS"), "Linux")

    # -------------------------------------------------------------------------
    # 2. Ubuntu / Cross-Platform Path Handling
    # -------------------------------------------------------------------------
    def test_02_ubuntu_path_handling(self):
        from lib.website.detector import NodeWebsiteDetectionProvider
        provider = NodeWebsiteDetectionProvider()
        # Verify node_auditor_dir does not contain hardcoded user path
        self.assertNotIn("/Users/metagurpreet/.gemini", provider.node_auditor_dir)
        self.assertTrue(provider.node_auditor_dir.endswith(os.path.join("scratch", "apify-workspace")))

    # -------------------------------------------------------------------------
    # 3. Required Environment Variable Validation
    # -------------------------------------------------------------------------
    def test_03_required_environment_variables(self):
        audit = ConfigValidator.validate_all()
        self.assertIn("services", audit)
        self.assertIn("storage", audit["services"])
        self.assertIn("google_sheets", audit["services"])
        self.assertIn("gosom", audit["services"])
        self.assertIn("scheduler", audit["services"])
        self.assertTrue(audit["travel_mode"])
        self.assertFalse(audit["commercial_actions_enabled"])

    # -------------------------------------------------------------------------
    # 4. Missing Secret Graceful Handling
    # -------------------------------------------------------------------------
    def test_04_missing_secret_detection(self):
        with patch.dict(os.environ, {}, clear=True):
            # When secrets are absent, config validator reports MISSING, not fatal crash
            audit = ConfigValidator.validate_all()
            self.assertIn(audit["services"]["google_sheets"]["status"], ["MISSING", "CONFIGURED"])
            self.assertFalse(audit["services"]["google_sheets"]["critical"])

    # -------------------------------------------------------------------------
    # 5. Dry-Run Safety Invariants
    # -------------------------------------------------------------------------
    def test_05_dry_run_safety(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            bootstrap_storage_baseline(data_dir=tmp_dir)
            with patch(
                "lib.discovery.osm.OpenStreetMapProvider.search_businesses",
                return_value=[_make_synthetic_candidate()],
            ):
                report = run_pipeline(
                    market_id="MANCHESTER_UK",
                    lead_limit=2,
                    dry_run=True,
                    report_path=os.path.join(tmp_dir, "test_report.json"),
                    data_dir=tmp_dir,
                )
            self.assertTrue(report["dry_run"])
            self.assertGreater(report["stats"]["processed_count"], 0)
            self.assertEqual(report["stats"]["crm_written_count"], 0)
            self.assertEqual(report["stats"]["outreach_dispatched_count"], 0)
            self.assertEqual(report["travel_mode_active"], True)
            self.assertEqual(report["commercial_actions_enabled"], False)

    # -------------------------------------------------------------------------
    # 6. Travel Mode Enforcement
    # -------------------------------------------------------------------------
    def test_06_travel_mode_enforcement(self):
        SystemConfig.TRAVEL_MODE = True
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = False
        self.assertTrue(SystemConfig.TRAVEL_MODE)
        self.assertFalse(SystemConfig.can_execute_commercial_actions())

        # If someone improperly sets TRAVEL_MODE = False in safety gate, it raises
        SystemConfig.TRAVEL_MODE = False
        with self.assertRaises(PermissionError):
            assert_safety_invariants()
        SystemConfig.TRAVEL_MODE = True

    # -------------------------------------------------------------------------
    # 7. Commercial Kill Switch
    # -------------------------------------------------------------------------
    def test_07_commercial_kill_switch(self):
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = False
        with self.assertRaises(CommercialActionForbiddenError):
            SystemConfig.assert_commercial_actions_allowed("dispatch_outreach")

        # If someone sets COMMERCIAL_ACTIONS_ENABLED = True, safety assertion blocks
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = True
        with self.assertRaises(PermissionError):
            assert_safety_invariants()
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = False

    # -------------------------------------------------------------------------
    # 8. Automated Email Outreach Locked
    # -------------------------------------------------------------------------
    def test_08_automated_email_disabled(self):
        SystemConfig.AUTOMATED_EMAIL_ENABLED = False
        with self.assertRaises(EmailAutomationBlockedError):
            SystemConfig.assert_automated_email_allowed("send_campaign_email")

        # If someone sets AUTOMATED_EMAIL_ENABLED = True, safety assertion blocks
        SystemConfig.AUTOMATED_EMAIL_ENABLED = True
        with self.assertRaises(PermissionError):
            assert_safety_invariants()
        SystemConfig.AUTOMATED_EMAIL_ENABLED = False

    # -------------------------------------------------------------------------
    # 9. Concurrency Controls & Cross-Process Locking
    # -------------------------------------------------------------------------
    def test_09_concurrency_and_file_locking(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock1 = FileLock("test_proc", timeout=0.5, lock_dir=tmp_dir)
            self.assertTrue(lock1.acquire())

            # Second lock attempt must timeout cleanly
            lock2 = FileLock("test_proc", timeout=0.2, lock_dir=tmp_dir)
            with self.assertRaises(LockTimeoutError):
                lock2.acquire()

            lock1.release()

            # Now second lock succeeds
            self.assertTrue(lock2.acquire())
            lock2.release()

    # -------------------------------------------------------------------------
    # 10. Ephemeral Runner Persistence & Baseline Bootstrapping
    # -------------------------------------------------------------------------
    def test_10_persistence_and_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            res = bootstrap_storage_baseline(data_dir=tmp_dir, sync_from_sheets_if_available=False)
            self.assertEqual(res["status"], "BOOTSTRAPPED")
            self.assertGreaterEqual(res["initialized_files_count"], len(CRITICAL_STORE_DEFAULTS))

            # Verify all critical files exist and are valid JSON
            monitor = SystemHealthMonitor(data_dir=tmp_dir)
            integrity = monitor.check_file_integrity()
            self.assertEqual(integrity["status"], "PASS")

    # -------------------------------------------------------------------------
    # 11. Gosom Scraper Availability & Compatibility
    # -------------------------------------------------------------------------
    def test_11_gosom_availability(self):
        from scripts.setup_gosom import is_binary_runnable, SCRAPER_BIN
        # In this environment, the binary exists
        if os.path.exists(SCRAPER_BIN):
            # Check runnable function doesn't crash
            _ = is_binary_runnable(SCRAPER_BIN)

        # Config validator discovers binary
        audit = ConfigValidator.validate_all()
        self.assertIn(audit["services"]["gosom"]["status"], ["CONFIGURED", "DISABLED"])

    # -------------------------------------------------------------------------
    # 12. Google Sheets Authentication Configuration
    # -------------------------------------------------------------------------
    def test_12_google_sheets_auth_configuration(self):
        # Test JSON string payload config
        mock_pk = "-----BEGIN " + "PRIVATE KEY-----\n" + "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC3\n" + "-----END " + "PRIVATE KEY-----\n"
        mock_sa = {
            "type": "service_account",
            "project_id": "mock-project",
            "private_key_id": "mock123",
            "private_key": mock_pk,
            "client_email": "mock-sa@mock-project.iam.gserviceaccount.com",
            "client_id": "123456789",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        with patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps(mock_sa)}):
            audit = ConfigValidator.validate_all()
            self.assertEqual(audit["services"]["google_sheets"]["status"], "CONFIGURED")
            self.assertIn("mock***", audit["services"]["google_sheets"]["details"])

        # Test individual env vars
        with patch.dict(os.environ, {
            "GOOGLE_SERVICE_ACCOUNT_EMAIL": "test-bot@dripp.iam.gserviceaccount.com",
            "GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY": "-----BEGIN " + "PRIVATE KEY-----\\nMIIE\\n-----END " + "PRIVATE KEY-----"
        }, clear=True):
            audit = ConfigValidator.validate_all()
            self.assertEqual(audit["services"]["google_sheets"]["status"], "CONFIGURED")


    # -------------------------------------------------------------------------
    # 13. Artifact & Report Generation
    # -------------------------------------------------------------------------
    def test_13_artifact_generation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            bootstrap_storage_baseline(data_dir=tmp_dir)
            report_file = os.path.join(tmp_dir, "custom_report.json")
            with patch(
                "lib.discovery.osm.OpenStreetMapProvider.search_businesses",
                return_value=[_make_synthetic_candidate()],
            ):
                report = run_pipeline(
                    market_id="MANCHESTER_UK",
                    lead_limit=1,
                    dry_run=True,
                    report_path=report_file,
                    data_dir=tmp_dir,
                )
            self.assertTrue(os.path.exists(report_file))
            with open(report_file, "r", encoding="utf-8") as rf:
                loaded = json.load(rf)
            self.assertEqual(loaded["run_id"], report["run_id"])
            self.assertEqual(loaded["status"], "COMPLETED")

    # -------------------------------------------------------------------------
    # 14. Failure Behavior & Non-Zero Exit Codes
    # -------------------------------------------------------------------------
    def test_14_failure_exit_codes(self):
        # Invalid market should raise ValueError
        with self.assertRaises(ValueError):
            run_pipeline(market_id="NON_EXISTENT_MARKET")

        # Commercial action enabled should raise PermissionError
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = True
        with self.assertRaises(PermissionError):
            run_pipeline(market_id="MANCHESTER_UK")
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = False

    # -------------------------------------------------------------------------
    # 15. Secret Scanning & Redaction
    # -------------------------------------------------------------------------
    def test_15_secret_redaction(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_file = os.path.join(tmp_dir, "fake_leak.txt")
            mock_sg = "SG." + "1234567890123456789012" + "." + "1234567890123456789012345678901234567890123"
            with open(test_file, "w") as f:
                f.write(f"api_key = '{mock_sg}'\n")

            with patch("scripts.security_secret_scan.PROJECT_ROOT", tmp_dir):
                findings = scan_file("fake_leak.txt")
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0]["secret_type"], "SendGrid API Key")
                # Ensure the full secret is NOT in masked_value
                self.assertNotIn("1234567890123456789012", findings[0]["masked_value"])
                self.assertTrue(findings[0]["masked_value"].endswith("chars)"))

    # -------------------------------------------------------------------------
    # 16. Workflow Files Validity & Least Privilege
    # -------------------------------------------------------------------------
    def test_16_workflow_files_validity_and_least_privilege(self):
        import yaml
        import glob

        workflow_files = glob.glob(os.path.join(PROJECT_ROOT, ".github", "workflows", "*.yml"))
        self.assertGreaterEqual(len(workflow_files), 4)

        for wfile in workflow_files:
            with open(wfile, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)

            # Least privilege permissions
            perms = data.get("permissions")
            if "05-deploy-cloud-run" in wfile:
                self.assertEqual(
                    perms,
                    {"contents": "read", "id-token": "write"},
                    f"Violated least privilege in {wfile}"
                )
            else:
                self.assertEqual(perms, {"contents": "read"}, f"Violated least privilege in {wfile}")

            # Concurrency control present with cancel-in-progress: false
            concurrency = data.get("concurrency")
            self.assertIsNotNone(concurrency, f"Missing concurrency in {wfile}")
            self.assertFalse(concurrency.get("cancel-in-progress", True), f"cancel-in-progress must be false in {wfile}")

    # -------------------------------------------------------------------------
    # 17. Importing lib.system Succeeds Without apify_client
    # -------------------------------------------------------------------------
    def test_17_import_lib_system_without_apify_client(self):
        import sys
        # Artificially mask apify_client from import system
        sys.modules["apify_client"] = None
        try:
            import importlib
            import lib.system
            importlib.reload(lib.system)
            from lib.system.storage_bootstrap import bootstrap_storage_baseline
            self.assertTrue(callable(bootstrap_storage_baseline))
        finally:
            sys.modules.pop("apify_client", None)

    # -------------------------------------------------------------------------
    # 18. Importing lib.discovery Succeeds Without apify_client
    # -------------------------------------------------------------------------
    def test_18_import_lib_discovery_without_apify_client(self):
        import sys
        sys.modules["apify_client"] = None
        try:
            import importlib
            import lib.discovery
            importlib.reload(lib.discovery)
            from lib.discovery import OpenStreetMapProvider, HybridDiscoveryEngine
            self.assertIsNotNone(OpenStreetMapProvider)
            self.assertIsNotNone(HybridDiscoveryEngine)
        finally:
            sys.modules.pop("apify_client", None)

    # -------------------------------------------------------------------------
    # 19. Normal Production Discovery Does Not Require Apify
    # -------------------------------------------------------------------------
    def test_19_normal_production_discovery_does_not_require_apify(self):
        from lib.discovery.hybrid import HybridDiscoveryEngine, DiscoveryMode
        engine = HybridDiscoveryEngine(mode=DiscoveryMode.FREE_LOCAL)
        self.assertFalse(engine.apify_enabled)
        self.assertIsNone(engine.apify)
        with patch(
            "lib.discovery.osm.OpenStreetMapProvider.health_check",
            return_value={"provider": "OpenStreetMap", "status": "CONNECTED", "endpoint": "mock://overpass"},
        ):
            health = engine.health_check()
        self.assertEqual(health["apify_enabled"], False)

    # -------------------------------------------------------------------------
    # 20. Normal Technical Pipeline Does Not Require Apify
    # -------------------------------------------------------------------------
    def test_20_normal_technical_pipeline_does_not_require_apify(self):
        import sys
        sys.modules["apify_client"] = None
        try:
            from scripts.run_technical_pipeline import run_pipeline
            with tempfile.TemporaryDirectory() as tmp_dir:
                with patch(
                    "lib.discovery.osm.OpenStreetMapProvider.search_businesses",
                    return_value=[_make_synthetic_candidate()],
                ):
                    report = run_pipeline(
                        market_id="MANCHESTER_UK",
                        lead_limit=1,
                        dry_run=True,
                        report_path=os.path.join(tmp_dir, "apify_free_report.json"),
                        data_dir=tmp_dir,
                    )
                self.assertTrue(report["dry_run"])
                self.assertEqual(report["stats"]["crm_written_count"], 0)
        finally:
            sys.modules.pop("apify_client", None)

    # -------------------------------------------------------------------------
    # 21. Gosom Path Operates Without Apify
    # -------------------------------------------------------------------------
    def test_21_gosom_path_operates_without_apify(self):
        from lib.enrichment.gosom_fallback import GosomReviewFreshnessFallback
        fallback = GosomReviewFreshnessFallback()
        self.assertIsNotNone(fallback)
        self.assertFalse(hasattr(fallback, "apify_client"))

    # -------------------------------------------------------------------------
    # 22. OpenStreetMap Path Operates Without Apify
    # -------------------------------------------------------------------------
    def test_22_openstreetmap_path_operates_without_apify(self):
        from lib.discovery.osm import OpenStreetMapProvider
        osm = OpenStreetMapProvider()
        self.assertIsNotNone(osm)
        self.assertEqual(osm.name, "OpenStreetMap")
        self.assertFalse(hasattr(osm, "apify_client"))

    # -------------------------------------------------------------------------
    # 23. Apify Adapter Fails Gracefully When Dependency Is Missing
    # -------------------------------------------------------------------------
    def test_23_apify_adapter_fails_gracefully_when_invoked_without_dependency(self):
        import sys
        sys.modules["apify_client"] = None
        try:
            from lib.discovery.apify import ApifyDiscoveryProvider
            provider = ApifyDiscoveryProvider(enabled=True)
            self.assertIsNone(provider.client)
            self.assertFalse(provider.enabled)
            health = provider.health_check()
            self.assertEqual(health["status"], "DISABLED")
            self.assertIn("disabled", health["reason"].lower())
            
            # Discover round returns empty without exception
            results, queries = provider.discover_round(
                round_number=1,
                country="United Kingdom",
                cities=["Manchester"],
                industry="restaurant",
                limit=5
            )
            self.assertEqual(results, [])
            self.assertEqual(queries, [])
        finally:
            sys.modules.pop("apify_client", None)

    # -------------------------------------------------------------------------
    # 24. CI Guard: No Top-Level apify_client Imports in Production Code
    # -------------------------------------------------------------------------
    def test_24_ci_guard_no_top_level_apify_imports_in_production_code(self):
        import ast
        lib_dir = os.path.join(PROJECT_ROOT, "lib")
        offending_files = []

        for root, _, files in os.walk(lib_dir):
            for file in files:
                if file.endswith(".py"):
                    file_path = os.path.join(root, file)
                    with open(file_path, "r", encoding="utf-8") as f:
                        tree = ast.parse(f.read(), filename=file_path)
                    
                    # Inspect only module-level statements (body of the module)
                    for node in tree.body:
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                if "apify" in alias.name:
                                    offending_files.append((file_path, alias.name, node.lineno))
                        elif isinstance(node, ast.ImportFrom):
                            if node.module and "apify" in node.module:
                                offending_files.append((file_path, node.module, node.lineno))

        self.assertEqual(
            offending_files,
            [],
            f"Found forbidden top-level apify imports in production code: {offending_files}"
        )

    # -------------------------------------------------------------------------
    # 25. CI Guard: Typing Annotations Evaluatable Without NameError
    # -------------------------------------------------------------------------
    def test_25_review_rating_enricher_typing_annotations(self):
        import typing
        from lib.enrichment.review_rating_enricher import ReviewRatingEnricher
        hints = typing.get_type_hints(ReviewRatingEnricher.parse_date_and_freshness)
        self.assertIn("as_of", hints)
        self.assertIn("return", hints)

    # -------------------------------------------------------------------------
    # 26. CI Guard: No Undefined Typing Imports Across Entire Repository
    # -------------------------------------------------------------------------
    def test_26_ci_guard_no_undefined_typing_imports_in_entire_repository(self):
        import ast
        import builtins
        import typing

        typing_names = {k for k in dir(typing) if not k.startswith("_")}
        missing_typing = []

        for root, _, files in os.walk(PROJECT_ROOT):
            if any(p in root for p in [".venv", ".git", "scratch", "__pycache__", "build", "dist"]):
                continue
            for f in files:
                if not f.endswith(".py"):
                    continue
                filepath = os.path.join(root, f)
                try:
                    with open(filepath, "r", encoding="utf-8") as fp:
                        tree = ast.parse(fp.read(), filename=filepath)
                except Exception:
                    continue

                defined_names = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            defined_names.add(alias.asname or alias.name)
                    elif isinstance(node, ast.ImportFrom):
                        for alias in node.names:
                            defined_names.add(alias.asname or alias.name)
                    elif isinstance(node, ast.ClassDef):
                        defined_names.add(node.name)
                    elif isinstance(node, ast.FunctionDef):
                        defined_names.add(node.name)
                    elif isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                defined_names.add(target.id)

                class AnnotationChecker(ast.NodeVisitor):
                    def visit_Name(self, node):
                        if node.id in typing_names and node.id not in defined_names and not hasattr(builtins, node.id):
                            missing_typing.append((filepath, node.lineno, node.id))

                class FunctionVisitor(ast.NodeVisitor):
                    def visit_FunctionDef(self, node):
                        if node.returns:
                            AnnotationChecker().visit(node.returns)
                        for arg in node.args.args + node.args.kwonlyargs:
                            if arg.annotation:
                                AnnotationChecker().visit(arg.annotation)
                        if node.args.vararg and node.args.vararg.annotation:
                            AnnotationChecker().visit(node.args.vararg.annotation)
                        if node.args.kwarg and node.args.kwarg.annotation:
                            AnnotationChecker().visit(node.args.kwarg.annotation)
                        self.generic_visit(node)

                    def visit_AsyncFunctionDef(self, node):
                        self.visit_FunctionDef(node)

                    def visit_AnnAssign(self, node):
                        if node.annotation:
                            AnnotationChecker().visit(node.annotation)
                        self.generic_visit(node)

                FunctionVisitor().visit(tree)

        self.assertEqual(
            missing_typing,
            [],
            f"Found missing typing imports in repository: {missing_typing}"
        )

    # -------------------------------------------------------------------------
    # 27. CI Guard: Production Cloud Run Deployment Workflow Hardening
    # -------------------------------------------------------------------------
    def test_27_production_deployment_workflow_hardening(self):
        import yaml

        deploy_wf_path = os.path.join(PROJECT_ROOT, ".github", "workflows", "05-deploy-cloud-run.yml")
        self.assertTrue(os.path.exists(deploy_wf_path), "05-deploy-cloud-run.yml must exist")

        with open(deploy_wf_path, "r", encoding="utf-8") as f:
            raw_text = f.read()
            data = yaml.safe_load(raw_text)

        # 1. Trigger must strictly be manual workflow_dispatch (no push, no pr, no cron)
        on_data = data.get("on") or data.get(True) or {}
        triggers = list(on_data.keys()) if isinstance(on_data, dict) else []
        self.assertEqual(triggers, ["workflow_dispatch"], "Deployment workflow must ONLY trigger on manual workflow_dispatch")

        # 2. Required confirmation input must exist
        inputs = on_data.get("workflow_dispatch", {}).get("inputs", {})
        self.assertIn("confirm_deployment", inputs, "Must define confirm_deployment input safety gate")
        self.assertTrue(inputs["confirm_deployment"].get("required", False), "confirm_deployment must be required")

        # 3. Mutable image_tag_override input must NOT exist
        self.assertNotIn("image_tag_override", inputs, "image_tag_override input must be removed to enforce immutable commit SHA tags")

        # 4. Input values must NOT be directly interpolated in shell scripts without env mapping
        self.assertNotIn("${{ github.event.inputs.confirm_deployment }}", raw_text.split("env:")[0] if "env:" in raw_text else "")
        # Confirm that the confirmation step uses env:
        steps = data.get("jobs", {}).get("deploy-cloud-run-job", {}).get("steps", [])
        validate_step = next((s for s in steps if s.get("name") == "Validate Safety Confirmation Gate"), None)
        self.assertIsNotNone(validate_step, "Must have Validate Safety Confirmation Gate step")
        self.assertIn("CONFIRM_TOKEN", validate_step.get("env", {}), "Input must be mapped through environment variables")

        # 5. Workflow must NOT create Artifact Registry repository (must verify existence only)
        self.assertNotIn("artifacts repositories create", raw_text, "Workflow must not attempt to create repository; administrator pre-provisioning required")

        # 6. Deployment verification must strictly assert all required settings
        self.assertIn("ENABLE_DISTRIBUTED_LOCK", raw_text)
        self.assertIn("FIRESTORE_ENABLED", raw_text)
        self.assertIn("TRAVEL_MODE", raw_text)
        self.assertIn("COMMERCIAL_ACTIONS_ENABLED", raw_text)
        self.assertIn("AUTOMATED_EMAIL_ENABLED", raw_text)
        self.assertIn("TECHNICAL_AUTOMATION_ENABLED", raw_text)

        # 6a. Deployment must explicitly configure and verify the correct job runner/identity
        # and keep the job in dry-run mode until an operator changes the safety posture.
        self.assertIn("RUNTIME_SERVICE_ACCOUNT", raw_text)
        self.assertIn("--service-account", raw_text)
        self.assertIn("--command=python3", raw_text)
        self.assertIn("--args=scripts/run_cloud_run_job.py", raw_text)
        self.assertIn("DRY_RUN=true", raw_text)
        self.assertIn("expected_runtime_sa", raw_text)
        self.assertIn("Container Command", raw_text)
        self.assertIn("DRY_RUN must be 'true'", raw_text)

        # 7. Safety invariant: workflow must NEVER automatically execute the discovery job
        self.assertNotIn("run jobs execute", raw_text, "Deployment workflow must NEVER automatically execute the discovery job")

        # 8. Immutable Tagging: workflow must NOT build or push mutable tags (e.g. latest, short sha)
        self.assertNotIn(":latest", raw_text, "Deployment workflow must not build or push mutable :latest image tag")
        self.assertNotIn("short_sha", raw_text, "Deployment workflow must not build or push mutable short_sha image tag")

        # 9. Pinned Checkout: checkout step must strictly checkout ${{ github.sha }} rather than resolving main again
        checkout_step = next((s for s in steps if s.get("name") == "Checkout Merged Codebase"), None)
        self.assertIsNotNone(checkout_step, "Must have Checkout Merged Codebase step")
        checkout_ref = str(checkout_step.get("with", {}).get("ref", ""))
        self.assertIn("${{ github.sha }}", checkout_ref, "Checkout step must pin to exact dispatched commit ${{ github.sha }}")

    # -------------------------------------------------------------------------
    # 28. CI Guard: GCP Administrator Setup Script Hardening & Security Checks
    # -------------------------------------------------------------------------
    def test_28_administrator_setup_script_hardening(self):
        import subprocess

        setup_script_path = os.path.join(PROJECT_ROOT, "scripts", "setup_gcp_deployment_iam.sh")
        self.assertTrue(os.path.exists(setup_script_path), "setup_gcp_deployment_iam.sh must exist")

        with open(setup_script_path, "r", encoding="utf-8") as f:
            script_text = f.read()

        # 1. Repository ID immutable claim must be defined and equal to 1405742084
        self.assertIn('REPOSITORY_ID="1405742084"', script_text, "Setup script must define immutable REPOSITORY_ID 1405742084")

        # 2. Attribute mapping must map attribute.repository_id=assertion.repository_id and attribute.workflow_ref=assertion.workflow_ref
        self.assertIn("attribute.repository_id=assertion.repository_id", script_text)
        self.assertIn("attribute.workflow_ref=assertion.workflow_ref", script_text)

        # 3. job_workflow_ref must NOT be in mapping or condition (reserved for reusable workflows)
        self.assertNotIn("job_workflow_ref", script_text, "Ordinary workflow_dispatch must use workflow_ref, not job_workflow_ref")

        # 4. Workload Identity User role binding must use attribute.repository_id
        self.assertIn("attribute.repository_id/${REPOSITORY_ID}", script_text)

        # 5. Attribute condition must restrict by repository_id, repository, ref, and EXACT workflow_ref equality
        self.assertIn("assertion.repository_id == '${REPOSITORY_ID}'", script_text)
        self.assertIn("assertion.repository == '${GITHUB_REPO}'", script_text)
        self.assertIn("assertion.ref == 'refs/heads/main'", script_text)
        self.assertIn("assertion.workflow_ref == '${GITHUB_REPO}/${WORKFLOW_PATH}@refs/heads/main'", script_text)

        # 6. Must support both create-oidc and update-oidc with identical mapping & condition
        self.assertIn("create-oidc", script_text)
        self.assertIn("update-oidc", script_text)
        # Ensure update-oidc uses the same mapping and condition
        update_section = script_text.split("update-oidc")[1].split("fi")[0]
        self.assertIn("--attribute-mapping=\"${MAPPING}\"", update_section)
        self.assertIn("--attribute-condition=\"${CONDITION}\"", update_section)

        # 7. Fail-closed Runtime SA Discovery: must stop with exit 1 if unresolved (no guessing)
        self.assertIn("if [ -z \"${ACTUAL_RUNTIME_SA}\" ]; then", script_text)
        self.assertIn("exit 1", script_text)
        # Verify script does not fall back to guessing an SA if job returns empty
        self.assertNotIn("ACTUAL_RUNTIME_SA=\"${COMPUTE_SA}\"", script_text)
        self.assertNotIn("ACTUAL_RUNTIME_SA=\"${DISCOVERY_SA}\"", script_text)

        # 8. Validate Production Runtime SA Parser with realistic Google Cloud Run Jobs responses
        # Avoid duplicate parser logic: test the production parse_runtime_service_account helper directly.

        # Fixture 1: Documented Google Cloud Run Jobs v1 API response
        # Schema: spec.template.spec.template.spec.serviceAccountName
        v1_doc_fixture = {
            "apiVersion": "run.googleapis.com/v1",
            "kind": "Job",
            "metadata": {
                "name": "dripp-discovery-job",
                "namespace": "poised-eye-509816-a4",
                "uid": "1111-2222-3333",
            },
            "spec": {
                "template": {
                    "metadata": {"name": "dripp-discovery-job-run"},
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "image": "us-central1-docker.pkg.dev/poised-eye-509816-a4/dripp-media/dripp-discovery-job:pinned",
                                        "resources": {"limits": {"cpu": "2000m", "memory": "2Gi"}},
                                    }
                                ],
                                "serviceAccountName": "dripp-international-leads@poised-eye-509816-a4.iam.gserviceaccount.com",
                                "timeoutSeconds": 1800,
                            }
                        }
                    },
                }
            },
        }
        res_v1 = parse_runtime_service_account(v1_doc_fixture, project_id="poised-eye-509816-a4")
        self.assertEqual(res_v1, "dripp-international-leads@poised-eye-509816-a4.iam.gserviceaccount.com")

        # Fixture 2: Documented Google Cloud Run Jobs v2 API response
        # Schema: template.template.serviceAccount
        v2_doc_fixture = {
            "name": "projects/poised-eye-509816-a4/locations/us-central1/jobs/dripp-discovery-job",
            "uid": "4444-5555-6666",
            "generation": "1",
            "template": {
                "template": {
                    "containers": [
                        {
                            "image": "us-central1-docker.pkg.dev/poised-eye-509816-a4/dripp-media/dripp-discovery-job:pinned",
                            "resources": {"limits": {"cpu": "2", "memory": "2Gi"}},
                        }
                    ],
                    "maxRetries": 0,
                    "timeout": "1800s",
                    "serviceAccount": "dripp-international-leads@poised-eye-509816-a4.iam.gserviceaccount.com",
                }
            },
        }
        res_v2 = parse_runtime_service_account(v2_doc_fixture, project_id="poised-eye-509816-a4")
        self.assertEqual(res_v2, "dripp-international-leads@poised-eye-509816-a4.iam.gserviceaccount.com")

        # Fixture 3: Short-name normalization for v1 and v2 responses
        v1_short = {
            "spec": {
                "template": {
                    "spec": {
                        "template": {
                            "spec": {
                                "serviceAccountName": "custom-runner"
                            }
                        }
                    }
                }
            }
        }
        self.assertEqual(
            parse_runtime_service_account(v1_short, project_id="poised-eye-509816-a4"),
            "custom-runner@poised-eye-509816-a4.iam.gserviceaccount.com",
        )

        v2_short = {
            "template": {
                "template": {
                    "serviceAccount": "custom-runner"
                }
            }
        }
        self.assertEqual(
            parse_runtime_service_account(v2_short, project_id="poised-eye-509816-a4"),
            "custom-runner@poised-eye-509816-a4.iam.gserviceaccount.com",
        )

        # Fixture 4: Missing identity in v1 and v2 (fail closed)
        v1_empty = {"spec": {"template": {"spec": {"template": {"spec": {"serviceAccountName": "   "}}}}}}
        self.assertEqual(parse_runtime_service_account(v1_empty, project_id="poised-eye-509816-a4"), "")

        v2_empty = {"template": {"template": {"serviceAccount": ""}}}
        self.assertEqual(parse_runtime_service_account(v2_empty, project_id="poised-eye-509816-a4"), "")

        unconfigured = {"spec": {"template": {}}}
        self.assertEqual(parse_runtime_service_account(unconfigured, project_id="poised-eye-509816-a4"), "")

        # Fixture 5: Malformed JSON and invalid types (fail closed without exception)
        self.assertEqual(parse_runtime_service_account("{ invalid: json }", project_id="poised-eye-509816-a4"), "")
        self.assertEqual(parse_runtime_service_account("", project_id="poised-eye-509816-a4"), "")
        self.assertEqual(parse_runtime_service_account(None, project_id="poised-eye-509816-a4"), "")
        self.assertEqual(parse_runtime_service_account([], project_id="poised-eye-509816-a4"), "")

        # Fixture 6: CLI subprocess execution of parse_cloud_run_sa.py via stdin
        helper_path = os.path.join(PROJECT_ROOT, "scripts", "parse_cloud_run_sa.py")
        proc_v1 = subprocess.run(
            [sys.executable, helper_path, "poised-eye-509816-a4"],
            input=json.dumps(v1_doc_fixture),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc_v1.stdout.strip(), "dripp-international-leads@poised-eye-509816-a4.iam.gserviceaccount.com")

        proc_v2 = subprocess.run(
            [sys.executable, helper_path, "poised-eye-509816-a4"],
            input=json.dumps(v2_doc_fixture),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc_v2.stdout.strip(), "dripp-international-leads@poised-eye-509816-a4.iam.gserviceaccount.com")

        # Fixture 7: Ensure setup_gcp_deployment_iam.sh invokes helper and includes documented fallback
        self.assertIn('PARSER_HELPER="${SCRIPT_DIR}/parse_cloud_run_sa.py"', script_text)
        self.assertIn("data.get('template', {}).get('template', {}).get('serviceAccount')", script_text)
        self.assertIn("spec.template.spec.template.spec.serviceAccountName", script_text)
        self.assertIn(".get('serviceAccountName')", script_text)

        # Fixture 8: Verify embedded fallback in setup_gcp_deployment_iam.sh has 100% parity with helper
        fallback_code = script_text.split('python3 -c "')[1].split('\n" "${PROJECT_ID}")')[0]
        for fixture, expected in [
            (v1_doc_fixture, "dripp-international-leads@poised-eye-509816-a4.iam.gserviceaccount.com"),
            (v2_doc_fixture, "dripp-international-leads@poised-eye-509816-a4.iam.gserviceaccount.com"),
            (v1_short, "custom-runner@poised-eye-509816-a4.iam.gserviceaccount.com"),
            (v2_short, "custom-runner@poised-eye-509816-a4.iam.gserviceaccount.com"),
            (v1_empty, ""),
            (v2_empty, ""),
            (unconfigured, ""),
        ]:
            proc_fallback = subprocess.run(
                [sys.executable, "-c", fallback_code, "poised-eye-509816-a4"],
                input=json.dumps(fixture),
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(proc_fallback.stdout.strip(), expected)

        # 9. Validate bash syntax
        res = subprocess.run(["bash", "-n", setup_script_path], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Bash syntax check failed: {res.stderr}")

    # -------------------------------------------------------------------------
    # 29. CI Guard: Administrator Setup Script Read-Only Preflight Ordering & Isolation
    # -------------------------------------------------------------------------
    def test_29_administrator_setup_script_preflight_ordering_and_isolation(self):
        """Enforces that setup_gcp_deployment_iam.sh executes job and runtime identity
        discovery in a read-only preflight phase before ANY resource creation,
        API enablement, or IAM policy modifications."""
        setup_script_path = os.path.join(PROJECT_ROOT, "scripts", "setup_gcp_deployment_iam.sh")
        with open(setup_script_path, "r", encoding="utf-8") as f:
            script_text = f.read()

        # 1. Step markers must confirm 8-step structure with preflight at step 2
        self.assertIn("[1/8] Configuring active GCP project", script_text)
        self.assertIn("[2/8] Running read-only preflight", script_text)
        self.assertIn("[3/8] Enabling minimal required GCP Service APIs", script_text)
        self.assertIn("[4/8] Ensuring Artifact Registry repository", script_text)
        self.assertIn("[5/8] Ensuring Workload Identity Pool", script_text)
        self.assertIn("[6/8] Ensuring Workload Identity Provider", script_text)
        self.assertIn("[7/8] Ensuring Deployer Service Account exists", script_text)
        self.assertIn("[8/8] Granting scoped, least-privilege deployment permissions", script_text)

        # 2. Strict Ordering Enforcement: Preflight must occur BEFORE all mutative commands
        idx_preflight = script_text.index("[2/8] Running read-only preflight")
        idx_api_enable = script_text.index("gcloud services enable")
        idx_artifact_repo = script_text.index("gcloud artifacts repositories create")
        idx_wif_pool = script_text.index("gcloud iam workload-identity-pools create")
        idx_wif_provider = script_text.index("gcloud iam workload-identity-pools providers create-oidc")
        idx_sa_create = script_text.index("gcloud iam service-accounts create")
        idx_first_iam_add = script_text.index("add-iam-policy-binding")

        self.assertLess(idx_preflight, idx_api_enable, "Preflight must precede API enablement")
        self.assertLess(idx_preflight, idx_artifact_repo, "Preflight must precede Artifact Registry creation")
        self.assertLess(idx_preflight, idx_wif_pool, "Preflight must precede WIF Pool creation")
        self.assertLess(idx_preflight, idx_wif_provider, "Preflight must precede WIF Provider creation")
        self.assertLess(idx_preflight, idx_sa_create, "Preflight must precede Deployer SA creation")
        self.assertLess(idx_preflight, idx_first_iam_add, "Preflight must precede any IAM policy bindings")

        # 3. Preflight Fail-Closed Verification
        # Extract preflight section
        preflight_section = script_text[idx_preflight:idx_api_enable]
        self.assertIn('JOB_JSON=$(gcloud run jobs describe "${JOB_NAME}"', preflight_section)
        self.assertIn('if [ -z "${JOB_JSON}" ]; then', preflight_section)
        self.assertIn('if [ -z "${ACTUAL_RUNTIME_SA}" ]; then', preflight_section)
        self.assertIn('if ! gcloud iam service-accounts describe "${ACTUAL_RUNTIME_SA}"', preflight_section)
        # All 3 failure branches in preflight must exit 1
        self.assertEqual(preflight_section.count("exit 1"), 3, "Preflight must have 3 fail-closed exit 1 guards")

        # 4. Scoped Alternative SA Cleanup: must target only DEPLOYER_EMAIL and roles/iam.serviceAccountUser
        step8_section = script_text[script_text.index("[8/8]"): ]
        self.assertIn('gcloud iam service-accounts remove-iam-policy-binding "${ALT_SA}"', step8_section)
        self.assertIn('--member="serviceAccount:${DEPLOYER_EMAIL}"', step8_section)
        self.assertIn('--role="roles/iam.serviceAccountUser"', step8_section)

        # 5. Short-name SA normalization test using production helper
        res_short = parse_runtime_service_account(
            '{"template": {"template": {"serviceAccount": "custom-runner"}}}',
            project_id="poised-eye-509816-a4",
        )
        self.assertEqual(res_short, "custom-runner@poised-eye-509816-a4.iam.gserviceaccount.com")


    # -------------------------------------------------------------------------
    # 29. Technical Pipeline Workflow Inputs Must Match Supported Runner Flags
    # -------------------------------------------------------------------------
    def test_29_technical_pipeline_workflow_controls_match_runner_capabilities(self):
        import yaml

        workflow_path = os.path.join(
            PROJECT_ROOT, ".github", "workflows", "02-technical-pipeline.yml"
        )
        with open(workflow_path, "r", encoding="utf-8") as f:
            workflow = yaml.safe_load(f)

        on_data = workflow.get("on") or workflow.get(True) or {}
        inputs = on_data.get("workflow_dispatch", {}).get("inputs", {})
        self.assertIn("dry_run", inputs)
        self.assertIn("enable_research", inputs)
        self.assertNotIn(
            "refresh_existing", inputs,
            "Do not expose a refresh_existing control until the runner implements it.",
        )
        self.assertNotIn(
            "enable_contactability", inputs,
            "Do not expose an enable_contactability control until the runner implements it.",
        )

        steps = workflow.get("jobs", {}).get("run-technical-pipeline", {}).get("steps", [])
        execute_step = next(
            (s for s in steps if s.get("name") == "Execute Technical Acquisition Pipeline"),
            None,
        )
        self.assertIsNotNone(execute_step)
        run_script = execute_step.get("run", "")
        self.assertIn("inputs.enable_research", run_script)
        self.assertIn("--no-enable-research", run_script)

        runner_path = os.path.join(PROJECT_ROOT, "scripts", "run_technical_pipeline.py")
        with open(runner_path, "r", encoding="utf-8") as f:
            runner = f.read()
        self.assertIn("action=argparse.BooleanOptionalAction", runner)
        self.assertNotIn('parser.add_argument("--refresh-existing"', runner)
        self.assertNotIn('parser.add_argument("--enable-contactability"', runner)


    # -------------------------------------------------------------------------
    # 30. API Service Deployment Must Be Preflighted and Verified
    # -------------------------------------------------------------------------
    def test_30_cloud_run_api_service_deployment_is_guarded(self):
        import os
        import yaml

        root = os.path.dirname(os.path.abspath(__file__))
        workflow_path = os.path.join(root, ".github", "workflows", "05-deploy-cloud-run.yml")
        with open(workflow_path, "r", encoding="utf-8") as f:
            raw = f.read()
            workflow = yaml.safe_load(raw)

        steps = workflow.get("jobs", {}).get("deploy-cloud-run-job", {}).get("steps", [])
        names = [step.get("name") for step in steps]
        self.assertIn("Preflight Cloud Run API Service Runtime Identity & Security", names)
        self.assertIn("Deploy & Update Cloud Run API Service", names)
        self.assertIn("Read-Back Inspection & API Health Verification", names)
        self.assertIn("Deploy & Update Cloud Run Job", names)
        self.assertLess(names.index("Preflight Cloud Run API Service Runtime Identity & Security"), names.index("Build Container Image"))
        self.assertLess(names.index("Push Image to Artifact Registry"), names.index("Deploy & Update Cloud Run API Service"))
        self.assertLess(names.index("Read-Back Inspection & API Health Verification"), names.index("Deploy & Update Cloud Run Job"))
        self.assertIn("verify_cloud_run_api_service.py preflight", raw)
        self.assertIn("verify_cloud_run_api_service.py deployed", raw)
        with open(os.path.join(root, "scripts", "verify_cloud_run_api_service.py"), encoding="utf-8") as f:
            verifier = f.read()
        self.assertIn("OPERATOR_KEY", verifier)
        self.assertIn("GOOGLE_SHEET_ID", verifier)
        self.assertIn("GCS_BACKUP_BUCKET", verifier)
        self.assertIn("API_RUNTIME_SERVICE_ACCOUNT", raw)
        with open(os.path.join(root, "scripts", "setup_gcp_api_service_deploy_iam.sh"), encoding="utf-8") as f:
            iam_setup = f.read()
        self.assertIn("roles/run.developer", iam_setup)
        self.assertIn("roles/iam.serviceAccountUser", iam_setup)
        self.assertNotIn("run jobs execute", raw.lower(), "Deployment must not execute the discovery job")

if __name__ == "__main__":
    unittest.main()



