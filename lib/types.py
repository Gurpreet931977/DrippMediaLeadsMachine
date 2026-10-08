from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List
from enum import Enum

class CountryStatus(str, Enum):
    COUNTRY_MATCH = "COUNTRY_MATCH"
    COUNTRY_MISMATCH = "COUNTRY_MISMATCH"
    COUNTRY_UNCLEAR = "COUNTRY_UNCLEAR"

class WebsiteStatus(str, Enum):
    WEBSITE_EXISTS = "WEBSITE_EXISTS"
    NO_WEBSITE_CONFIRMED = "NO_WEBSITE_CONFIRMED"
    WEBSITE_UNCLEAR = "WEBSITE_UNCLEAR"
    WEBSITE_BROKEN = "WEBSITE_BROKEN"

class VerificationStatus(str, Enum):
    NO_WEBSITE_CONFIRMED = "NO_WEBSITE_CONFIRMED"
    WEBSITE_UNCLEAR = "WEBSITE_UNCLEAR"
    WEBSITE_EXISTS = "WEBSITE_EXISTS"
    WEBSITE_BROKEN = "WEBSITE_BROKEN"

class SocialStatus(str, Enum):
    SOCIAL_FOUND = "SOCIAL_FOUND"
    SOCIAL_NOT_FOUND = "SOCIAL_NOT_FOUND"
    SOCIAL_UNKNOWN = "SOCIAL_UNKNOWN"

class SocialOwnershipStatus(str, Enum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    UNKNOWN = "UNKNOWN"
    # Backward compatibility aliases
    VERIFIED_OWNED = "VERIFIED"
    POTENTIAL_MISMATCH = "UNVERIFIED"

class SocialActivityStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    UNKNOWN = "UNKNOWN"
    # Backward compatibility aliases
    SOCIAL_ACTIVE = "ACTIVE"
    SOCIAL_INACTIVE = "INACTIVE"
    SOCIAL_ACTIVITY_UNKNOWN = "UNKNOWN"

class SocialProfileStatus(str, Enum):
    ACCESSIBLE = "ACCESSIBLE"
    INACCESSIBLE = "INACCESSIBLE"
    INVALID_FORMAT = "INVALID_FORMAT"
    UNKNOWN = "UNKNOWN"

class OperationalStatus(str, Enum):
    # Phase 9.1 Canonical States
    NOT_CHECKED = "NOT_CHECKED"
    VERIFIED_ACTIVE = "VERIFIED_ACTIVE"
    WEAK_SIGNAL = "WEAK_SIGNAL"
    CONFLICTING = "CONFLICTING"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"

    # Legacy & Pipeline Backward Compatibility
    ACTIVE_CONFIRMED = "ACTIVE_CONFIRMED"
    ACTIVE_LIKELY = "ACTIVE_LIKELY"
    OPERATIONAL_UNKNOWN = "OPERATIONAL_UNKNOWN"
    CLOSED_OR_UNVERIFIED = "CLOSED_OR_UNVERIFIED"

    @classmethod
    def normalize(cls, val: Any) -> str:
        """Normalizes any operational status string to canonical Phase 9.1 values."""
        if not val or not isinstance(val, str):
            return cls.UNKNOWN.value
        s = val.strip().upper()
        if s in (cls.VERIFIED_ACTIVE.value, cls.ACTIVE_CONFIRMED.value):
            return cls.VERIFIED_ACTIVE.value
        if s in (cls.WEAK_SIGNAL.value, cls.ACTIVE_LIKELY.value):
            return cls.WEAK_SIGNAL.value
        if s in (cls.CLOSED.value, cls.CLOSED_OR_UNVERIFIED.value):
            return cls.CLOSED.value
        if s == cls.CONFLICTING.value:
            return cls.CONFLICTING.value
        if s == cls.NOT_CHECKED.value:
            return cls.NOT_CHECKED.value
        return cls.UNKNOWN.value

class OperationalConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

class EvidenceFreshness(str, Enum):
    RECENT = "RECENT"                 # within 90 days
    RECENT_ENOUGH = "RECENT_ENOUGH"   # 91-180 days
    STALE = "STALE"                   # 181+ days
    UNKNOWN = "UNKNOWN"               # date unavailable

class SourceFamily(str, Enum):
    GOOGLE = "GOOGLE"
    TRIPADVISOR = "TRIPADVISOR"
    RESTAURANT_GURU = "RESTAURANT_GURU"
    FACEBOOK = "FACEBOOK"
    INSTAGRAM = "INSTAGRAM"
    YELP = "YELP"
    OPENSTREETMAP = "OPENSTREETMAP"
    OFFICIAL_WEBSITE = "OFFICIAL_WEBSITE"
    BUSINESS_REGISTRY = "BUSINESS_REGISTRY"
    OTHER_DIRECTORY = "OTHER_DIRECTORY"
    UNKNOWN = "UNKNOWN"

class CreatorEvidenceStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    UNVERIFIED = "UNVERIFIED"
    REJECTED = "REJECTED"

class CreatorEvidenceConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"

class CreatorFreshness(str, Enum):
    CURRENT = "CURRENT"               # 0-90 days
    RECENT = "RECENT"                 # 91-180 days
    STALE = "STALE"                   # 181+ days
    UNKNOWN = "UNKNOWN"               # No date available

class CreatorReferenceType(str, Enum):
    ACCOUNT_TAG = "ACCOUNT_TAG"
    CAPTION_BUSINESS_MENTION = "CAPTION_BUSINESS_MENTION"
    CAPTION_LOCATION_MENTION = "CAPTION_LOCATION_MENTION"
    LOCATION_TAG = "LOCATION_TAG"
    HASHTAG_MENTION = "HASHTAG_MENTION"
    ON_CONTENT_TEXT = "ON_CONTENT_TEXT"
    TITLE_OR_DESCRIPTION = "TITLE_OR_DESCRIPTION"
    MULTI_SIGNAL = "MULTI_SIGNAL"

class CreatorEvidenceClassification(str, Enum):
    INVALID = "INVALID"
    AMBIGUOUS = "AMBIGUOUS"
    VALID_THIRD_PARTY_REFERENCE = "VALID_THIRD_PARTY_REFERENCE"

class HandleClassification(str, Enum):
    CANDIDATE_OFFICIAL_ACCOUNT = "CANDIDATE_OFFICIAL_ACCOUNT"
    CREATOR_HANDLE = "CREATOR_HANDLE"
    COMMENTER_HANDLE = "COMMENTER_HANDLE"
    AMBIGUOUS = "AMBIGUOUS"
    NONE = "NONE"

class CreatorTrustConcept(str, Enum):
    CREATOR_DISCOVERY_SIGNAL = "CREATOR_DISCOVERY_SIGNAL"
    CREATOR_VERIFIED_EVIDENCE = "CREATOR_VERIFIED_EVIDENCE"

class EvidenceTier(str, Enum):
    DISCOVERY_ONLY = "DISCOVERY_ONLY"
    CORROBORATED = "CORROBORATED"
    VERIFIED = "VERIFIED"

class SourceQualityTier(str, Enum):
    HIGH_VALUE = "HIGH_VALUE"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

@dataclass
class CreatorEvidenceItem:
    creator_evidence_status: str = CreatorEvidenceStatus.UNVERIFIED.value
    creator_name: str = ""
    creator_handle: str = ""
    platform: str = ""
    content_url: str = ""
    content_type: str = ""
    published_at: str = ""
    business_reference: str = ""
    location_reference: str = ""
    tagged_business_handle: str = ""
    caption_excerpt: str = ""
    evidence_confidence: str = CreatorEvidenceConfidence.UNKNOWN.value
    source_url: str = ""
    discovered_at: str = ""
    freshness: str = CreatorFreshness.UNKNOWN.value
    disqualification_reason: str = ""
    # Section 1 & 13 Advanced Evidence Fields
    reference_type: str = CreatorReferenceType.CAPTION_BUSINESS_MENTION.value
    business_name_mentioned: bool = False
    location_mentioned: str = ""
    location_tag: str = ""
    location_tag_name: str = ""
    location_tag_url: str = ""
    location_tag_text: str = ""
    business_tag: str = ""
    hashtags: List[str] = field(default_factory=list)
    content_text_reference: str = ""
    caption_reference: str = ""
    matching_signals: List[str] = field(default_factory=list)
    evidence_summary: str = ""
    # Part 2 & 3 Discovery V3 Evidence Metadata
    source_domain: str = ""
    discovery_query: str = ""
    business_name_match: bool = False
    location_match: bool = False
    evidence_score: float = 0.0
    candidate_official_handle: str = ""
    candidate_official_handles: List[str] = field(default_factory=list)
    # V3.1 Precision & Audit Classifications
    city_match: bool = False
    street_match: bool = False
    street_postcode_match: bool = False
    classification: str = CreatorEvidenceClassification.INVALID.value
    handle_classification: str = HandleClassification.NONE.value
    audit_judgment: str = ""
    audit_reason: str = ""
    # V3.2 Secondary Evidence & Quality Tiers
    trust_concept: str = CreatorTrustConcept.CREATOR_DISCOVERY_SIGNAL.value
    evidence_tier: str = EvidenceTier.DISCOVERY_ONLY.value
    source_quality: str = SourceQualityTier.LOW.value
    contextual_match: bool = False
    context_signals: List[str] = field(default_factory=list)
    canonical_url: str = ""


    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CreatorEvidenceSummary:
    creator_evidence_status: str = CreatorEvidenceStatus.NOT_FOUND.value
    creator_evidence_count: int = 0
    creator_evidence_confidence: str = CreatorEvidenceConfidence.UNKNOWN.value
    creator_latest_date: str = ""
    creator_evidence_summary: str = ""
    creator_evidence_urls: List[str] = field(default_factory=list)
    creator_discovered_at: str = ""
    items: List[CreatorEvidenceItem] = field(default_factory=list)
    discovered_official_handles: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "creator_evidence_status": self.creator_evidence_status,
            "creator_evidence_count": self.creator_evidence_count,
            "creator_evidence_confidence": self.creator_evidence_confidence,
            "creator_latest_date": self.creator_latest_date,
            "creator_evidence_summary": self.creator_evidence_summary,
            "creator_evidence_urls": self.creator_evidence_urls,
            "creator_discovered_at": self.creator_discovered_at,
            "items": [it.to_dict() for it in self.items],
            "discovered_official_handles": self.discovered_official_handles
        }


class QualificationState(str, Enum):
    OUTREACH_READY = "OUTREACH_READY"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    EXCLUDED = "EXCLUDED"

class Priority(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    RESEARCH_ONLY = "RESEARCH_ONLY"

class LeadStatus(str, Enum):
    NOT_CONTACTED = "NOT_CONTACTED"
    CONTACTED = "CONTACTED"
    REPLIED = "REPLIED"
    CALL_BOOKED = "CALL_BOOKED"
    PROPOSAL = "PROPOSAL"
    WON = "WON"
    LOST = "LOST"
    NOT_A_FIT = "NOT_A_FIT"

class OutreachStatus(str, Enum):
    NOT_READY = "NOT_READY"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    READY_FOR_SEND = "READY_FOR_SEND"
    QUEUED = "QUEUED"
    SENDING = "SENDING"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    BOUNCED = "BOUNCED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    REPLIED = "REPLIED"
    FOLLOW_UP_DUE = "FOLLOW_UP_DUE"
    DO_NOT_CONTACT = "DO_NOT_CONTACT"

class OutreachMode(str, Enum):
    MANUAL = "MANUAL"
    AUTO = "AUTO"

class CallOutcome(str, Enum):
    NONE = ""
    CONNECTED = "CONNECTED"
    VOICEMAIL = "VOICEMAIL"
    GATEKEEPER = "GATEKEEPER"
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"
    WRONG_NUMBER = "WRONG_NUMBER"
    NOT_INTERESTED = "NOT_INTERESTED"
    CALLBACK_REQUESTED = "CALLBACK_REQUESTED"
    MEETING_BOOKED = "MEETING_BOOKED"

class DisqualificationReason(str, Enum):
    NONE = ""
    WEBSITE_EXISTS = "WEBSITE_EXISTS"
    COUNTRY_MISMATCH = "COUNTRY_MISMATCH"
    WEBSITE_UNCLEAR = "WEBSITE_UNCLEAR"
    WEBSITE_BROKEN = "WEBSITE_BROKEN"
    NOT_QUALIFIED = "NOT_QUALIFIED"
    DUPLICATE = "DUPLICATE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    LOW_REVIEWS = "LOW_REVIEWS"
    LOW_RATING = "LOW_RATING"
    SOCIAL_MISMATCH = "SOCIAL_MISMATCH"

class ProcessingState(str, Enum):
    DISCOVERED = "DISCOVERED"
    COUNTRY_CHECK = "COUNTRY_CHECK"
    WEBSITE_CHECK = "WEBSITE_CHECK"
    VERIFYING_NO_WEBSITE = "VERIFYING_NO_WEBSITE"
    QUALIFYING = "QUALIFYING"
    QUALIFIED = "QUALIFIED"
    SAVED = "SAVED"
    EXCLUDED = "EXCLUDED"
    FAILED = "FAILED"

@dataclass
class DiscoveredBusiness:
    company_name: str
    category: str
    city: str
    target_country: str
    detected_country: str = ""
    country_status: str = CountryStatus.COUNTRY_UNCLEAR.value
    country_evidence: str = ""
    region: str = ""
    postcode: str = ""
    address: str = ""
    phone: str = ""
    email: str = ""
    raw_website: str = ""
    google_maps_url: str = ""
    instagram_url: str = ""
    facebook_url: str = ""
    tiktok_url: str = ""
    linkedin_url: str = ""
    other_social_url: str = ""
    social_status: str = SocialStatus.SOCIAL_UNKNOWN.value
    social_activity: str = ""
    social_profile_status: str = SocialProfileStatus.UNKNOWN.value
    review_count: Optional[int] = None
    rating: Optional[float] = None
    photo_count: Optional[int] = None
    opening_hours: str = ""
    is_permanently_closed: bool = False
    is_temporarily_closed: bool = False
    latest_review_date: str = ""
    latest_social_post_date: str = ""
    operational_status: str = OperationalStatus.OPERATIONAL_UNKNOWN.value
    operational_evidence: str = ""
    places_count: int = 1
    discovery_query: str = ""
    discovery_source: str = "APIFY"
    source_id: str = ""
    brand: str = ""
    amenity: str = ""
    cuisine: str = ""
    street: str = ""
    house_number: str = ""
    branch_identifier: str = ""
    address_completeness: str = ""
    street_source: str = ""
    house_number_source: str = ""
    postcode_source: str = ""
    parent_osm_id: Optional[str] = None
    address_conflict_status: str = "NO_CONFLICT"
    conflicting_address_data: Dict[str, Any] = field(default_factory=dict)
    lat: Optional[float] = None
    lon: Optional[float] = None
    osm_type: str = ""
    source_confidence: str = "HIGH"
    city_match: bool = True
    city_match_reason: str = ""
    boundary_source: str = ""
    boundary_validation_method: str = ""
    osm_website_status: str = ""
    phone_source_family: Optional[str] = None
    address_source_family: Optional[str] = None
    hours_source_family: Optional[str] = None
    evidence_sources: Dict[str, Any] = field(default_factory=dict)
    raw_data: Dict[str, Any] = field(default_factory=dict)

    @property
    def country(self) -> str:
        return self.detected_country or self.target_country

    @property
    def latitude(self) -> Optional[float]:
        return self.lat

    @latitude.setter
    def latitude(self, val: Optional[float]):
        self.lat = val

    @property
    def longitude(self) -> Optional[float]:
        return self.lon

    @longitude.setter
    def longitude(self, val: Optional[float]):
        self.lon = val

@dataclass
class Lead:
    lead_id: str
    company_name: str
    industry: str
    target_country: str
    country: str
    city: str
    region: str = ""
    postcode: str = ""
    address: str = ""
    phone: str = ""
    website: str = ""
    website_status: str = WebsiteStatus.NO_WEBSITE_CONFIRMED.value
    verification_status: str = VerificationStatus.NO_WEBSITE_CONFIRMED.value
    verification_reason: str = ""
    google_maps_url: str = ""
    instagram_url: str = ""
    facebook_url: str = ""
    tiktok_url: str = ""
    review_count: Optional[int] = None
    rating: Optional[float] = None
    social_status: str = SocialStatus.SOCIAL_UNKNOWN.value
    social_ownership_status: str = SocialOwnershipStatus.UNKNOWN.value
    social_profile_status: str = SocialProfileStatus.UNKNOWN.value
    social_activity: str = ""
    operational_status: str = OperationalStatus.OPERATIONAL_UNKNOWN.value
    operational_confidence: str = OperationalConfidence.LOW.value
    operational_evidence: str = ""
    evidence_freshness: str = EvidenceFreshness.UNKNOWN.value
    multiple_locations: str = "No"
    business_activity_signal: str = ""
    qualification_signals: str = ""
    lead_score: int = 0
    priority: str = Priority.LOW.value
    qualification_state: str = QualificationState.OUTREACH_READY.value
    qualification_reason: str = ""
    red_flags: str = ""
    outreach_angle: str = ""
    lead_status: str = LeadStatus.NOT_CONTACTED.value
    date_added: str = ""
    last_contact: str = ""
    next_followup: str = ""
    notes: str = ""
    discovery_source: str = "APIFY"
    city_match: bool = True
    city_match_reason: str = ""
    boundary_source: str = ""
    boundary_validation_method: str = ""
    
    # Outreach Architecture V3 Fields (Post-Pipeline Execution)
    campaign_id: str = ""
    pipeline_run_id: str = ""
    verified_social_platforms: str = ""
    verified_social_urls: str = ""
    outreach_mode: str = OutreachMode.MANUAL.value
    outreach_status: str = OutreachStatus.NOT_READY.value
    outreach_channel: str = ""
    outreach_message: str = ""
    outreach_generated_at: str = ""
    outreach_sent_at: str = ""
    outreach_attempt_count: int = 0
    outreach_message_id: str = ""
    outreach_block_reason: str = ""
    call_status: str = ""
    call_notes: str = ""
    call_outcome: str = ""
    next_follow_up: str = ""
    response_status: str = ""
    manual_outreach_notes: str = ""

    # Real Delivery / Bounce Tracking Fields
    bounce_code: str = ""
    bounce_reason: str = ""
    bounced_at: str = ""
    bounce_provider: str = ""
    email_suppressed: str = ""
    email_suppression_reason: str = ""

    # Creator / Influencer Evidence Fields
    creator_evidence_status: str = CreatorEvidenceStatus.NOT_FOUND.value
    creator_evidence_count: int = 0
    creator_evidence_confidence: str = CreatorEvidenceConfidence.UNKNOWN.value
    creator_latest_date: str = ""
    creator_evidence_summary: str = ""
    creator_evidence_urls: str = ""
    creator_discovered_at: str = ""

    # Internal metadata
    processing_state: str = ProcessingState.QUALIFIED.value
    evidence: Dict[str, Any] = field(default_factory=dict)
    score_breakdown: Dict[str, int] = field(default_factory=dict)
    contactability_status: str = "NOT_CONTACTABLE"
    contactability_reason: str = ""

    def __post_init__(self):
        if not self.outreach_status or not str(self.outreach_status).strip():
            self.outreach_status = OutreachStatus.NOT_READY.value

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_sheet_row(self, headers: List[str]) -> List[Any]:
        d = asdict(self)
        row = []
        for h in headers:
            val = d.get(h, "")
            if val is None:
                val = ""
            val_str = str(val).strip()
            # Prevent formula injection / formula parse error in Google Sheets for phone numbers starting with +
            if h == "phone" and val_str.startswith("+") and not val_str.startswith("'"):
                val_str = f"'{val_str}"
            elif h == "outreach_status" and not val_str:
                val_str = OutreachStatus.NOT_READY.value
            row.append(val_str)
        return row

@dataclass
class ResearchLogEntry:
    research_id: str
    company_name: str
    industry: str
    target_country: str
    detected_country: str
    country_status: str
    city: str
    region: str = ""
    postcode: str = ""
    address: str = ""
    phone: str = ""
    website: str = ""
    website_status: str = ""
    verification_status: str = ""
    review_count: Optional[int] = None
    rating: Optional[float] = None
    social_status: str = SocialStatus.SOCIAL_UNKNOWN.value
    social_ownership_status: str = SocialOwnershipStatus.UNKNOWN.value
    social_profile_status: str = SocialProfileStatus.UNKNOWN.value
    operational_status: str = OperationalStatus.OPERATIONAL_UNKNOWN.value
    operational_confidence: str = OperationalConfidence.LOW.value
    evidence_freshness: str = EvidenceFreshness.UNKNOWN.value
    qualification_state: str = QualificationState.EXCLUDED.value
    qualification_status: str = "EXCLUDED"
    disqualification_reason: str = ""
    red_flags: str = ""
    source_url: str = ""
    date_researched: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_sheet_row(self, headers: List[str]) -> List[Any]:
        d = asdict(self)
        row = []
        for h in headers:
            val = d.get(h, "")
            if val is None:
                val = ""
            val_str = str(val).strip()
            if h == "phone" and val_str.startswith("+") and not val_str.startswith("'"):
                val_str = f"'{val_str}"
            row.append(val_str)
        return row


class ResearchFailureState(str, Enum):
    """
    Phase 11.1 & 11.2 Differentiated Research & Review Failure States.
    Distinguishes failure states to prevent collapsing into generic 'missing reviews'.
    """
    NO_EVIDENCE_FOUND = "NO_EVIDENCE_FOUND"              # A. Genuinely no evidence found
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"        # B. Provider unavailable (connection refused / network)
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"  # C. Provider not configured (keys missing)
    PROVIDER_FAILED = "PROVIDER_FAILED"                  # D. Provider failed (circuit open / HTTP error)
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"                # E. Provider timed out
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"                    # F. Provider quota exhausted / budget reached
    EXTRACTION_FAILED = "EXTRACTION_FAILED"              # G. Evidence extraction failed (unparseable)
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"              # H. Identity mismatch (wrong branch / rejected)
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"              # I. Evidence conflict (reconciliation conflict)
    NONE = "NONE"                                        # Evidence recovered successfully


@dataclass
class ResearchTelemetry:
    """
    Phase 11.1 & 11.2 Candidate-level research telemetry.
    Records granular provider attempts, results, signals, failure reasons, and performance metrics.
    """
    candidate: str
    provider_attempted: List[str] = field(default_factory=list)
    provider_result: str = "NOT_ATTEMPTED"
    evidence_found: str = "NONE"
    operational_signal_found: str = "NONE"
    fallback_attempted: str = "NONE"
    fallback_result: str = "NOT_ATTEMPTED"
    failure_reason: str = ResearchFailureState.NONE.value
    query_count: int = 0
    usable_results_count: int = 0
    latency: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate": self.candidate,
            "provider_attempted": list(self.provider_attempted),
            "provider_result": self.provider_result,
            "evidence_found": self.evidence_found,
            "operational_signal_found": self.operational_signal_found,
            "fallback_attempted": self.fallback_attempted,
            "fallback_result": self.fallback_result,
            "failure_reason": self.failure_reason,
            "query_count": self.query_count,
            "usable_results_count": self.usable_results_count,
            "latency": round(self.latency, 3),
            "details": dict(self.details),
        }

