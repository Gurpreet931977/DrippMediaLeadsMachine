#!/usr/bin/env python3
"""
scripts/parse_cloud_run_sa.py
=============================
Parser helper for extracting and normalizing runtime service account identities
from Google Cloud Run Job JSON descriptions.

Supported Schemas (Google Cloud Run Jobs API):
- v2 API standard: template.template.serviceAccount
- v1 API standard: spec.template.spec.template.spec.serviceAccountName

Safety Invariants:
- Fails closed (returns empty string) if unconfigured, missing, or malformed.
- Normalizes short-name service account IDs (e.g., 'runner' -> 'runner@${PROJECT_ID}.iam.gserviceaccount.com').
- No external dependencies beyond the standard library.
"""

import json
import sys
from typing import Any, Optional


def parse_runtime_service_account(data: Any, project_id: Optional[str] = None) -> str:
    """Extracts the runtime service account from Cloud Run Job JSON (string, bytes, or dict).

    Supports:
      1. Cloud Run Jobs v2 API standard:
         job.template.template.serviceAccount
      2. Cloud Run Jobs v1 API standard:
         job.spec.template.spec.template.spec.serviceAccountName

    Returns:
      Normalized full service account email address, or empty string on missing/invalid identity.
    """
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except Exception:
            return ""

    if not isinstance(data, dict):
        return ""

    sa = ""

    # 1. Cloud Run Jobs v2 API standard: template.template.serviceAccount
    template = data.get("template")
    if isinstance(template, dict):
        inner_template = template.get("template")
        if isinstance(inner_template, dict):
            val = inner_template.get("serviceAccount")
            if isinstance(val, str):
                sa = val

    # 2. Cloud Run Jobs v1 API standard: spec.template.spec.template.spec.serviceAccountName
    if not sa:
        spec = data.get("spec")
        if isinstance(spec, dict):
            t1 = spec.get("template")
            if isinstance(t1, dict):
                s1 = t1.get("spec")
                if isinstance(s1, dict):
                    t2 = s1.get("template")
                    if isinstance(t2, dict):
                        s2 = t2.get("spec")
                        if isinstance(s2, dict):
                            val = s2.get("serviceAccountName")
                            if isinstance(val, str):
                                sa = val

    sa = sa.strip()
    # Normalize short-name SA: if non-empty and lacks domain, append project IAM domain
    if sa and "@" not in sa and project_id:
        sa = f"{sa}@{project_id}.iam.gserviceaccount.com"

    return sa


def main() -> None:
    project_id = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        raw_input = sys.stdin.read()
        sa = parse_runtime_service_account(raw_input, project_id=project_id)
        print(sa)
    except Exception:
        print("")


if __name__ == "__main__":
    main()
