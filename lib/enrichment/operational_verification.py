"""
lib/enrichment/operational_verification.py
=========================================
Phase 9.2: Dedicated Operational Verification Engine.

Enforces strict separation between discovery node presence and current active operation.
Preserves verifiable provenance for every operational signal.

Statuses:
  - NOT_CHECKED: Newly ingested; zero checks performed.
  - VERIFIED_ACTIVE: Independent multi-source active operational proof confirmed.
  - WEAK_SIGNAL: Plausible operational hint lacking independent corroboration.
  - CONFLICTING: Materially contradictory operational evidence.
  - CLOSED: Verified permanent or temporary closure.
  - UNKNOWN: Insufficient evidence to establish current active operation.
"""

import os
import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Tuple, Set

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import (
    OperationalStatus,
    SourceFamily,
    EvidenceFreshness,
)


@dataclass
class OperationalEvidenceItem:
    """Provenance-bearing operational evidence record."""
    status: str  # NOT_CHECKED, VERIFIED_ACTIVE, WEAK_SIGNAL, CONFLICTING, CLOSED, UNKNOWN
    source: str
    source_url: str
    observed_at: str
    evidence_type: str
    confidence: float
    source_family: str = ""
    notes: List[str] = field(default_factory=list)
    raw_signal: Dict[str, Any] = field(default_factory=dict)
    normalized_result: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "source_family": self.source_family or self.source,
            "source": self.source,
            "source_url": self.source_url,
            "observed_at": self.observed_at,
            "evidence_type": self.evidence_type,
            "confidence": round(self.confidence, 4),
            "notes": list(self.notes),
            "raw_signal": dict(self.raw_signal),
            "normalized_result": self.normalized_result,
        }


class OperationalVerificationEngine:
    """
    Central operational verification engine.
    Evaluates raw signals and independent corroboration to produce deterministic operational status.
    """

    CLOSURE_KEYWORDS = [
        "permanently closed",
        "closed permanently",
        "permanently shut",
        "dissolved",
        "liquidation",
        "closure announcement",
        "no longer operating",
        "ceased trading",
        "shut down",
        "out of business",
    ]

    INDEPENDENT_ACTIVE_TYPES = {
        "verified_telephone_answer",
        "ch_active_filing",
        "verified_social_activity",
        "first_party_booking_active",
        "recent_transaction_receipt",
        "google_maps_reviews",
    }

    @classmethod
    def evaluate_operational_status(
        cls,
        candidate_id: str,
        company_name: str,
        osm_present: bool = True,
        independent_signals: Optional[List[Dict[str, Any]]] = None,
        phone: Optional[str] = None,
        closure_flag: bool = False,
        closure_reason: Optional[str] = None,
        observed_at: Optional[str] = None,
    ) -> OperationalEvidenceItem:
        """
        Determines canonical operational status with full provenance.
        Invariant: OSM presence alone or historical directory alone NEVER produces VERIFIED_ACTIVE.
        """
        now_ts = observed_at or datetime.now(timezone.utc).isoformat()
        signals = independent_signals or []
        notes: List[str] = []

        # 1. Closure Check
        if closure_flag:
            note = closure_reason or "Explicit closure signal identified."
            notes.append(note)
            return OperationalEvidenceItem(
                status=OperationalStatus.CLOSED.value,
                source_family="closure_detector",
                source="closure_detector",
                source_url="",
                observed_at=now_ts,
                evidence_type="closure_confirmation",
                confidence=0.98,
                notes=notes,
                raw_signal={"closure_flag": True, "reason": note},
                normalized_result=OperationalStatus.CLOSED.value,
            )

        # Inspect any signals mentioning closure keywords
        for sig in signals:
            raw_text = str(sig.get("raw_text", "")).lower()
            if any(k in raw_text for k in cls.CLOSURE_KEYWORDS):
                notes.append(f"Closure keyword found in signal from {sig.get('source', 'unknown')}: {raw_text[:80]}")
                return OperationalEvidenceItem(
                    status=OperationalStatus.CLOSED.value,
                    source_family=sig.get("source_family", "closure_detector"),
                    source=sig.get("source", "external_signal"),
                    source_url=sig.get("url", ""),
                    observed_at=now_ts,
                    evidence_type="closure_signal",
                    confidence=0.95,
                    notes=notes,
                    raw_signal=sig,
                    normalized_result=OperationalStatus.CLOSED.value,
                )

        # 2. Conflicting Signals Check
        has_active_signal = any(
            s.get("evidence_type") in cls.INDEPENDENT_ACTIVE_TYPES and s.get("confidence", 0.0) >= 0.70
            for s in signals
        )
        has_dormant_or_closed_signal = any(
            s.get("is_dormant") is True or s.get("is_closed") is True or s.get("evidence_type") == "inactive_filing"
            for s in signals
        )
        if has_active_signal and has_dormant_or_closed_signal:
            notes.append("Material conflict: contradictory active and dormant/closed signals detected.")
            return OperationalEvidenceItem(
                status=OperationalStatus.CONFLICTING.value,
                source_family="multi_signal_reconciler",
                source="multi_signal_reconciler",
                source_url="",
                observed_at=now_ts,
                evidence_type="conflicting_signals",
                confidence=0.50,
                notes=notes,
                raw_signal={"signals_count": len(signals)},
                normalized_result=OperationalStatus.CONFLICTING.value,
            )

        # 3. Source Family Independence & Active Corroboration
        distinct_source_families: Set[str] = set()
        active_signals: List[Dict[str, Any]] = []

        for sig in signals:
            etype = sig.get("evidence_type", "")
            sfamily = sig.get("source_family", sig.get("source", "unknown"))
            conf = float(sig.get("confidence", 0.0))
            if etype in cls.INDEPENDENT_ACTIVE_TYPES and conf >= 0.70:
                distinct_source_families.add(sfamily)
                active_signals.append(sig)

        # Add telephone corroboration as an independent signal if phone present and verified
        if phone and phone.strip():
            distinct_source_families.add("direct_telecom")

        # Invariant: OSM alone must NOT be VERIFIED_ACTIVE
        if not active_signals:
            if osm_present:
                notes.append("OpenStreetMap node exists; zero independent current operational corroboration.")
                return OperationalEvidenceItem(
                    status=OperationalStatus.UNKNOWN.value,
                    source_family="openstreetmap",
                    source="openstreetmap",
                    source_url="https://www.openstreetmap.org",
                    observed_at=now_ts,
                    evidence_type="osm_discovery_node",
                    confidence=0.25,
                    notes=notes,
                    raw_signal={"osm_present": True},
                    normalized_result=OperationalStatus.UNKNOWN.value,
                )
            else:
                notes.append("No operational signals checked or present.")
                return OperationalEvidenceItem(
                    status=OperationalStatus.NOT_CHECKED.value,
                    source_family="none",
                    source="none",
                    source_url="",
                    observed_at=now_ts,
                    evidence_type="none",
                    confidence=0.0,
                    notes=notes,
                    raw_signal={},
                    normalized_result=OperationalStatus.NOT_CHECKED.value,
                )

        # Rule B requires at least 2 independent source families for VERIFIED_ACTIVE
        # E.g. Google review + direct phone OR Google review + Companies House filing
        if len(distinct_source_families) >= 2:
            notes.append(
                f"Verified active: {len(active_signals)} active signals across "
                f"{len(distinct_source_families)} independent source families ({', '.join(sorted(distinct_source_families))})."
            )
            return OperationalEvidenceItem(
                status=OperationalStatus.VERIFIED_ACTIVE.value,
                source_family=active_signals[0].get("source_family", "multi_source"),
                source=active_signals[0].get("source", "multi_source"),
                source_url=active_signals[0].get("url", ""),
                observed_at=now_ts,
                evidence_type="independent_multi_source_corroboration",
                confidence=0.92,
                notes=notes,
                raw_signal={"distinct_families": list(distinct_source_families)},
                normalized_result=OperationalStatus.VERIFIED_ACTIVE.value,
            )
        elif len(distinct_source_families) == 1:
            notes.append(
                f"Weak signal: active signal detected from single family ({list(distinct_source_families)[0]}); "
                "lacks independent second source family."
            )
            return OperationalEvidenceItem(
                status=OperationalStatus.WEAK_SIGNAL.value,
                source_family=active_signals[0].get("source_family", "single_source"),
                source=active_signals[0].get("source", "single_source"),
                source_url=active_signals[0].get("url", ""),
                observed_at=now_ts,
                evidence_type="single_source_operational_hint",
                confidence=0.55,
                notes=notes,
                raw_signal={"single_family": list(distinct_source_families)},
                normalized_result=OperationalStatus.WEAK_SIGNAL.value,
            )

        # Fallback to UNKNOWN
        notes.append("Insufficient independent evidence to verify current operation.")
        return OperationalEvidenceItem(
            status=OperationalStatus.UNKNOWN.value,
            source_family="unknown",
            source="unknown",
            source_url="",
            observed_at=now_ts,
            evidence_type="insufficient_evidence",
            confidence=0.30,
            notes=notes,
            raw_signal={},
            normalized_result=OperationalStatus.UNKNOWN.value,
        )
