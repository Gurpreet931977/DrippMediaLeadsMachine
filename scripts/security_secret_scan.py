#!/usr/bin/env python3
"""
scripts/security_secret_scan.py
===============================
Lightweight, Zero-Dependency Repository Secret Scanner.
Scans repository and Git commit history for accidental credential leaks:
  - Private keys
  - Service account keys
  - API tokens (SendGrid, GitHub, Meta, OpenAI, Google)
  - Hardcoded passwords

INVARIANTS:
  - NEVER prints full secret values to stdout, logs, or reports.
  - Exits non-zero (1) if unallowed secrets are discovered in tracked files.
  - Verifies .gitignore contains protection for .env, secrets, credentials, backups, logs.
"""

import os
import re
import sys
import json
import subprocess
from typing import Dict, Any, List, Tuple

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Signatures for secret detection
PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("Private Key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("Google Service Account Key", re.compile(r"\"type\":\s*\"service_account\"[\s\S]*?\"private_key\":\s*\"-----BEGIN")),
    ("SendGrid API Key", re.compile(r"\bSG\.[0-9A-Za-z-_]{22}\.[0-9A-Za-z-_]{43}\b")),
    ("GitHub Personal Access Token", re.compile(r"\b(?:gh[pousr]_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{22,})\b")),
    ("Tavily API Key", re.compile(r"\btvly-[A-Za-z0-9_\-]{30,}\b")),
    ("Authorization Header Token", re.compile(r"(?i)\bAuthorization\s*:\s*(?:Bearer|Basic)\s+[A-Za-z0-9_\-\.\+/=]{20,}\b")),
    ("SMTP Password Assignment", re.compile(r"(?i)\b(?:smtp[_\-]?pass(?:word)?)\s*[:=]\s*['\"][^'\"\s]{6,}['\"]")),
    ("Hardcoded Password Assignment", re.compile(r"(?i)\b(?:password|passwd)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]")),
    ("AWS Access Key ID", re.compile(r"\b(AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b")),
    ("Stripe Live Secret Key", re.compile(r"\bsk_live_[0-9a-zA-Z]{24,}\b")),
    ("Meta / Facebook Access Token", re.compile(r"\bEAACEdEose0cBA[0-9A-Za-z]+\b")),
    ("OpenAI Live API Key", re.compile(r"\bsk-[a-zA-Z0-9]{48,}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z-_]{35}\b")),
]

# Patterns allowed in synthetic test files and templates
ALLOWLIST_PATTERNS = [
    re.compile(r"AIzaSyMockTestKeyForCanaryValidation123"),
    re.compile(r"AIzaSyMock"),
    re.compile(r"example"),
    re.compile(r"test_key"),
    re.compile(r"test_password"),
    re.compile(r"mock_token"),
    re.compile(r"mock_secret"),
    re.compile(r"\[REDACTED"),
    re.compile(r"\*\*\*"),
]

# Required entries in .gitignore
REQUIRED_GITIGNORE_ENTRIES = [
    ".env",
    "credentials",
    "secrets",
    "data/",
    "logs",
    "*.log",
    "node_modules/",
    ".venv/",
    "__pycache__/",
]


def check_gitignore() -> List[str]:
    gitignore_path = os.path.join(PROJECT_ROOT, ".gitignore")
    if not os.path.exists(gitignore_path):
        return ["Missing .gitignore file!"]

    missing = []
    with open(gitignore_path, "r", encoding="utf-8") as f:
        content = f.read()

    for entry in REQUIRED_GITIGNORE_ENTRIES:
        # Check if entry or base word is covered
        base = entry.rstrip("/*")
        if base not in content:
            missing.append(f"Missing {entry} pattern in .gitignore")

    return missing


def get_git_tracked_files() -> List[str]:
    try:
        out = subprocess.check_output(["git", "ls-files"], cwd=PROJECT_ROOT, text=True)
        return [f.strip() for f in out.splitlines() if f.strip()]
    except Exception as e:
        print(f"Warning: git ls-files failed: {e}")
        tracked = []
        for root, dirs, files in os.walk(PROJECT_ROOT):
            if any(p in root for p in [".git", ".venv", "node_modules"]):
                continue
            for f in files:
                tracked.append(os.path.relpath(os.path.join(root, f), PROJECT_ROOT))
        return tracked


def scan_file(filepath: str) -> List[Dict[str, Any]]:
    full_path = os.path.join(PROJECT_ROOT, filepath)
    if not os.path.isfile(full_path):
        return []

    # Skip known non-text/large files
    ext = os.path.splitext(filepath)[1].lower()
    if ext in [".png", ".jpg", ".jpeg", ".ico", ".bin", ".tar", ".gz", ".zip"]:
        return []

    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return []

    matches = []
    for label, pattern in PATTERNS:
        for m in pattern.finditer(content):
            matched_str = m.group(0)

            # Check allowlist
            if any(al.search(matched_str) for al in ALLOWLIST_PATTERNS):
                continue

            # Special case: Google client key embedded in public static map URLs
            if "google_res.html" in filepath or "maps_init_state.json" in filepath:
                # Public static client web asset URL
                continue

            # Redact match: only show first 4 chars and length
            redacted = matched_str[:4] + "***" + f"({len(matched_str)} chars)"
            matches.append({
                "file": filepath,
                "secret_type": label,
                "masked_value": redacted,
                "line": content[:m.start()].count("\n") + 1,
            })

    return matches


def run_security_scan() -> Dict[str, Any]:
    print("=" * 72)
    print("  DRIPP MEDIA REPOSITORY SECURITY & CREDENTIAL SCAN")
    print("=" * 72)

    gitignore_issues = check_gitignore()
    tracked_files = get_git_tracked_files()

    print(f"\n[1/3] Checking .gitignore coverage...")
    if gitignore_issues:
        for iss in gitignore_issues:
            print(f"  ✗ {iss}")
    else:
        print("  ✓ .gitignore properly protects sensitive credentials, logs, and state.")

    print(f"\n[2/3] Scanning {len(tracked_files)} Git-tracked repository files...")
    findings = []
    for rel_path in tracked_files:
        hits = scan_file(rel_path)
        if hits:
            findings.extend(hits)

    if findings:
        print(f"  ✗ Found {len(findings)} potential credential occurrences:")
        for hit in findings:
            print(f"    - {hit['file']}:{hit['line']} [{hit['secret_type']}] -> {hit['masked_value']}")
    else:
        print(f"  ✓ Zero unallowed credentials detected in tracked files.")

    print("\n[3/3] Scanning Git commit history...")
    git_history_findings = 0
    try:
        log_diff = subprocess.check_output(
            ["git", "log", "-n", "30", "-p"],
            cwd=PROJECT_ROOT,
            text=True,
            errors="ignore"
        )
        for label, pat in PATTERNS:
            for m in pat.finditer(log_diff):
                val = m.group(0)
                if not any(al.search(val) for al in ALLOWLIST_PATTERNS):
                    git_history_findings += 1
        if git_history_findings == 0:
            print("  ✓ Git history is clean of credential additions.")
        else:
            print(f"  ⚠ Notice: Found {git_history_findings} possible secret patterns in git history.")
    except Exception as e:
        print(f"  - Git history scan skipped: {e}")

    overall_clean = len(findings) == 0 and len(gitignore_issues) == 0

    print("\n" + "=" * 72)
    print(f"  SECURITY AUDIT RESULT: [{'PASS' if overall_clean else 'FAIL'}]")
    print(f"  Total Tracked Secrets Found: {len(findings)}")
    print("=" * 72 + "\n")

    return {
        "status": "PASS" if overall_clean else "FAIL",
        "secrets_found_count": len(findings),
        "findings": findings,
        "gitignore_issues": gitignore_issues,
    }


def main():
    res = run_security_scan()
    sys.exit(0 if res["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
