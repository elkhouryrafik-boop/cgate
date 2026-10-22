"""
cgate.lexicon — the taxonomy of what we are looking for, and the surface forms
procurement documents actually use to say it.

Fifteen categories, R1-R15. They are not a general legal ontology; they are the
provisions that decide whether a bid is admissible and whether the firm can
perform without integrity failures. R3 covers procurement-
integrity checks on the firm's own conduct and its counterparties.

Precision discipline
--------------------
Each category has ANCHORS (specific, near-unambiguous phrasing) and CONTEXT
(supporting terms). A clause matches on an anchor alone, or on two independent
context terms. Bare keyword matching on words like "conduct" or "integrity"
produces a flag on every second clause of a general-conditions document and is worse than
nothing, because a reviewer who learns to ignore the output has no control at
all.
"""

from __future__ import annotations

import re

from . import settings

# ---------------------------------------------------------------------------
# The procuring organisation: role words, plus optional aliases from config
# (``agency_aliases`` in config/procedure.example.yaml).
# ---------------------------------------------------------------------------

_ROLE_WORDS = ("Purchaser", "Employer", "Organization", "Fund", "Client", "Contracting Authority")
_ALIASES = tuple(re.escape(a) for a in settings.load().get("agency_aliases") or [])
# Matches "the Purchaser", "the Fund", ... or a configured alias.
AGENCY = r"(?:the (?:" + "|".join(_ROLE_WORDS) + r")" + "".join("|" + a for a in _ALIASES) + r")"
_AG = AGENCY[3:-1]  # the alternation without the outer group

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

CATEGORIES: dict[str, dict] = {
    "R1": {
        "name": "eligibility_and_debarment",
        "checklist_items": [3, 8],
        "anchors": [
            r"\bdebarr?(?:ed|ment)\b",
            r"\bineligib(?:le|ility)\b",
            r"\bsuspend(?:ed|sion) from (?:eligibility|doing business)\b",
            r"\bremoved from the[^.;]{0,40}(?:vendor|supplier) (?:roster|database)\b",
            r"\bnot been found guilty\b",
            r"\b(?:vendor|supplier) (?:registration|marketplace) (?:portal|platform)\b",
            r"\bexcluded from (?:eligibility|participation)\b",
            r"\btemporary suspension\b",
            r"\b(?:sanction|suspension)s?\b[^.;]{0,80}\bimposed by\b",
        ],
        "context": [
            r"\beligib\w+\b", r"\bvendor (?:roster|database|registration)\b",
            r"\bsanctions? (?:list|regime)\b", r"\bblacklist\w*\b",
        ],
    },
    "R2": {
        "name": "conflict_of_interest",
        "checklist_items": [4],
        "anchors": [
            r"\bconflicts? of interest\b",
            r"\bpotential (?:or actual )?conflict\b",
            r"\bunfair competitive advantage\b",
        ],
        "context": [r"\bdisclos\w+\b", r"\bimpartial\w*\b", r"\bperceived\b"],
    },
    "R3": {
        "name": "collusion_and_independent_bidding",
        "checklist_items": [6, 7, 9],
        "anchors": [
            r"\bcollusi(?:on|ve)\b",
            r"\bbid[- ]rigging\b",
            r"\bindependently(?:,| and) without (?:any )?consultation\b",
            r"\bmore than one (?:proposal|bid|offer|tender)\b",
            r"\bsubmit(?:ted|ting)? (?:only )?one (?:proposal|bid|offer)\b",
            r"\bin (?:any|more than one) capacity\b.{0,80}\b(?:bid|proposal|tender)\b",
            r"\barrangement with (?:any )?(?:other )?(?:bidder|offeror|tenderer|competitor)\b",
            r"\bprice[- ]fixing\b",
            r"\banti[- ]competitive\b",
        ],
        "context": [
            r"\bcompetitor\w*\b", r"\bother bidders?\b", r"\bconsortium\b",
            r"\bjoint venture\b", r"\bindependent(?:ly)?\b",
        ],
    },
    "R4": {
        "name": "subcontracting_and_third_parties",
        "checklist_items": [6, 7, 8, 9],
        "anchors": [
            r"\bsub-?contract(?:or|ing|ed)?s?\b",
            r"\bprior written (?:approval|consent) of " + AGENCY + r"\b.{0,80}\bsub-?contract",
            r"\bagents?(?:,| and) intermediar(?:y|ies)\b",
            r"\bconsortium (?:member|partner)s?\b",
            r"\bjoint venture partners?\b",
        ],
        "context": [
            r"\bremain(?:s)? responsible\b", r"\bflow[- ]down\b",
            r"\bthird part(?:y|ies)\b", r"\bapproval\b",
        ],
    },
    "R5": {
        "name": "anti_bribery_gifts_and_facilitation",
        "checklist_items": [10, 11],
        "anchors": [
            r"\bbriber?y\b",
            r"\bcorrupt (?:practice|conduct|act)s?\b",
            r"\bkickbacks?\b",
            r"\bfacilitation payments?\b",
            r"\b(?:gift|hospitality|entertainment)s?\b.{0,60}\b(?:offer|accept|provide|solicit)\w*\b",
            r"\b(?:offer|accept|provide|solicit)\w*\b.{0,60}\b(?:gift|hospitality|gratuit\w+)\b",
            r"\bimproper (?:advantage|inducement|payment)s?\b",
            r"\bthing of value\b",
            r"\bcorrupt\w*\b[^.;]{0,60}\b(?:conduct|practices?)\b",
            r"\bdirect or indirect benefit\b",
            r"\bgifts?,\s+(?:favours?|favors?)\s+or\s+hospitality\b",
        ],
        "context": [r"\bpublic official\b", r"\bcorruption\b", r"\bundue\b"],
    },
    "R6": {
        "name": "sanctions_and_counter_terrorism",
        "checklist_items": [3, 8],
        "anchors": [
            r"\bSecurity Council (?:Committee|resolutions?)\b",
            r"\bconsolidated (?:\w+ ){0,4}sanctions list\b",
            r"\b1267\b|\b1988\b|\b1989\b|\b2253\b",
            r"\bterroris(?:m|t)\b",
            r"\bcounter[- ]terrorism\b",
            r"\basset freeze\b",
            r"\bmoney laundering\b",
        ],
        "context": [r"\bsanctions?\b", r"\blisted (?:individual|entity|entities)\b"],
    },
    "R7": {
        "name": "fraud_and_misrepresentation",
        "checklist_items": [15],
        "anchors": [
            r"\bfraud(?:ulent)?\b",
            r"\bmisrepresent\w+\b",
            r"\bfalse (?:statement|information|declaration)s?\b",
            r"\bmaterial omission\b",
            r"\bknowingly (?:provid|submit|mak)\w+\b.{0,60}\b(?:false|inaccurate|misleading)\b",
            r"\bobstructive (?:practice|conduct)\b",
            r"\bcoercive (?:practice|conduct)\b",
        ],
        "context": [r"\baccurate\b", r"\bcomplete and correct\b", r"\bmisleading\b"],
    },
    "R8": {
        "name": "audit_investigation_and_cooperation",
        "checklist_items": [12],
        "anchors": [
            r"\bright to audit\b",
            r"\baudits? and investigations?\b",
            r"\bcooperat\w+ (?:fully )?(?:with|in)\b.{0,60}\b(?:audit|investigat)\w+",
            r"\baccess to (?:its )?(?:relevant )?(?:books|records|premises|personnel)\b",
            r"\binvestigat(?:ion|ors?|ive)\b",
            r"\bOffice of Internal (?:Audit|Oversight)\b",
            r"\bpost-?payment audit\b",
            r"\b(?:internal|external) auditors?\b",
        ],
        "context": [r"\brecords?\b", r"\bexamine\b", r"\bunfettered\b"],
    },
    "R9": {
        "name": "confidentiality_and_data",
        "checklist_items": [12],
        "anchors": [
            r"\bconfidential information\b",
            r"\bpersonal data\b",
            r"\bdata protection\b",
            r"\bnon-?disclosure\b",
        ],
        "context": [r"\bdisclos\w+\b", r"\bprocessing\b", r"\bsecurity\b"],
    },
    "R10": {
        "name": "safeguarding_psea_and_conduct",
        "checklist_items": [12],
        "anchors": [
            r"\bsexual exploitation(?: and abuse)?\b",
            r"\bPSEA\b",
            r"\bsafeguarding\b",
            r"\bchild (?:protection|safeguarding)\b",
            r"\bharassment\b",
            r"\bmine action\b",
            r"\bhuman rights\b",
            r"\bchild lab(?:ou)?r\b",
            r"\bforced lab(?:ou)?r\b",
            r"\bhuman trafficking\b",
            r"\bworst forms of child lab(?:ou)?r\b",
        ],
        "context": [r"\bbeneficiar\w+\b", r"\bvulnerable\b", r"\bzero tolerance\b"],
    },
    "R11": {
        "name": "ongoing_disclosure_obligations",
        "checklist_items": [11, 12],
        "anchors": [
            r"\b(?:promptly|immediately) (?:notify|inform|disclose)\b",
            r"\bduty to (?:disclose|report)\b",
            r"\bnotify " + AGENCY + r"\b",
            r"\bany change in\b.{0,60}\b(?:circumstance|status|ownership|control)\b",
        ],
        "context": [r"\bwithin \d+ (?:days|working days)\b", r"\bin writing\b"],
    },
    "R12": {
        "name": "un_personnel_and_officials",
        "checklist_items": [4, 10],
        "anchors": [
            r"\b(?:" + _AG + r"|UN) (?:official|staff member|personnel|employee)s?\b"
            r".{0,80}\b(?:offer|employ|engage|benefit|gift|advantage)\w*\b",
            r"\bformer (?:UN|" + _AG + r") (?:official|staff)\b",
            r"\bno (?:official|staff member) of\b",
        ],
        "context": [r"\bemployment\b", r"\brecruit\w*\b", r"\bpost-?employment\b"],
    },
    "R13": {
        "name": "termination_and_remedies_for_integrity",
        "checklist_items": [12],
        "anchors": [
            r"\bterminate the Contract immediately\b",
            r"\btermination for (?:cause|default)\b",
            r"\bentitle(?:s|d)? " + AGENCY + r" to terminate\b",
            r"\bdisqualif(?:y|ied|ication)\b",
            r"\brescind\b",
            r"\brecover (?:any )?(?:amounts?|sums?|damages)\b",
        ],
        "context": [r"\bbreach\b", r"\bremed(?:y|ies)\b", r"\bwithout (?:any )?liability\b"],
    },
    "R14": {
        "name": "record_retention",
        "checklist_items": [12],
        "anchors": [
            r"\bretain\b.{0,60}\brecords?\b.{0,60}\b(?:\d+|five|six|seven|ten) years?\b",
            r"\brecords?\b.{0,40}\bfor (?:a period of )?(?:\d+|five|six|seven|ten) years?\b",
            r"\bretention period\b",
            r"\bfor (?:a period of )?(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve)\s*(?:\(\d+\)\s*)?years? (?:after|following|from)\b",
        ],
        "context": [r"\bbooks and records\b", r"\barchive\w*\b"],
    },
    "R15": {
        "name": "supplier_code_and_policy_compliance",
        "checklist_items": [12],
        "anchors": [
            r"\bSupplier Code of Conduct\b",
            r"\bCode of Conduct\b",
            r"\bcompl(?:y|iance) with\b.{0,60}\bpolic(?:y|ies)\b",
            r"\brepresents that it has reviewed\b",
            r"\bGlobal Compact\b",
        ],
        "context": [r"\bpolicies applicable\b", r"\bstandards\b"],
    },
}

# ---------------------------------------------------------------------------
# Modality
# ---------------------------------------------------------------------------

PROHIBITION = re.compile(
    r"\b(?:shall|will|must|may|can)\s+(?:not|never|in no (?:event|circumstances))\b"
    r"|\bis (?:strictly )?prohibited\b"
    r"|\bare (?:strictly )?prohibited\b"
    r"|\bno\s+\w+\s+(?:shall|will|may)\b"
    r"|\brefrain from\b"
    r"|\bshall (?:in no event|under no circumstances)\b"
    r"|\bwill not engage\b"
    r"|\bnot (?:be )?(?:entitled|permitted|allowed) to\b",
    re.IGNORECASE,
)

OBLIGATION = re.compile(
    r"\b(?:shall|must)\b"
    r"|\bwill\s+(?:be\s+)?\w+"
    r"|\bundertakes? to\b"
    r"|\bagrees? to\b"
    r"|\bis (?:required|obliged|obligated) to\b"
    r"|\bis responsible for\b"
    r"|\brepresents (?:and warrants )?that\b"
    r"|\bwarrants? that\b"
    r"|\bcertifies? that\b"
    r"|\backnowledges? (?:and agrees )?that\b",
    re.IGNORECASE,
)

RIGHT_OF_UN = re.compile(
    r"\b" + AGENCY + r"\s+"
    r"(?:reserves the right|may(?:,[^,]{0,40},)?\s+(?:at its option|require|request|terminate|suspend|disqualify|reject))"
    r"|\breserves the right to\b",
    re.IGNORECASE,
)

PERMISSION = re.compile(
    r"\bmay\b|\bis entitled to\b|\bat its (?:sole )?(?:option|discretion)\b",
    re.IGNORECASE,
)

CONDITION = re.compile(
    r"\bin order to be eligible\b|\bto be eligible\b|\beligibility (?:criteria|requirements?)\b"
    r"|\bprovided that\b|\bshall be deemed\b|\bit is a condition\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Actors
# ---------------------------------------------------------------------------

ACTOR_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("subcontractor", re.compile(
        r"\b(?:sub-?contractors?|agents?|intermediar(?:y|ies)|consortium (?:member|partner)s?"
        r"|joint venture partners?|third part(?:y|ies)|Affiliates?)\b", re.IGNORECASE)),
    ("personnel", re.compile(
        r"\b(?:its |the )?Personnel\b|\bemployees?\b|\bstaff\b", re.IGNORECASE)),
    ("contractor", re.compile(
        r"\b(?:the )?(?:Contractor|Company|Supplier|Bidder|Offeror|Tenderer|Proposer|Vendor"
        r"|Consultant|Service Provider|Contracting Firm|CF|Undersigned|Institution)\b")),
    ("un", re.compile(
        r"\b(?:" + _AG + r"|the UN)\b")),
]

# ---------------------------------------------------------------------------
# Exceptions and carve-outs
# ---------------------------------------------------------------------------

EXCEPTION_MARKERS = re.compile(
    r"\b(?:"
    r"except(?:ing)?(?: (?:as|for|where|that|to the extent|in so far as|insofar as))?"
    r"|unless(?: and until)?"
    r"|save (?:that|for|as|to the extent)"
    r"|other than"
    r"|with the exception of"
    r"|provided(?:,| that|, however,| however that)"
    r"|subject to"
    r"|notwithstanding"
    r"|shall not apply"
    r"|does not apply"
    r"|do not apply"
    r"|nothing in this\b[^,.;]{0,60}\bshall"
    r"|to the extent (?:that|permitted)"
    r"|but for"
    r"|in the absence of"
    r")\b",
    re.IGNORECASE,
)

# An exception that points somewhere else. These are the ones models lose:
# measured degradation to near zero on non-local scope resolution.
NONLOCAL_HINT = re.compile(
    r"\b(?:Article|Clause|Section|paragraph|Annex|Schedule|Appendix)\s+"
    r"([A-Z]?\d{1,2}(?:\.\d{1,3}){0,4}[a-z]?|\([a-z]\)|[A-Z])\b"
    r"|\bas (?:set out|specified|provided|defined) (?:in|under|elsewhere)\b"
    r"|\bin accordance with the\b"
    r"|\belsewhere in (?:this|the) (?:Contract|Agreement)\b",
)

SENTENCE_SPLIT = re.compile(r"(?<=[.;])\s+(?=[A-Z(\"])")


def match_categories(text: str) -> list[tuple[str, list[str]]]:
    """Return [(category_id, [matched surface forms])] for a piece of text."""
    out: list[tuple[str, list[str]]] = []
    for cid, spec in CATEGORIES.items():
        hits: list[str] = []
        for pat in spec["anchors"]:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                hits.append(m.group(0)[:80])
        if hits:
            out.append((cid, hits))
            continue
        ctx = [
            re.search(p, text, re.IGNORECASE).group(0)[:60]
            for p in spec["context"]
            if re.search(p, text, re.IGNORECASE)
        ]
        if len(ctx) >= 2:
            out.append((cid, ctx))
    return out
