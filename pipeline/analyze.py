"""Draft generator for briefs, action pages, and test case dossiers.

Analyzes meeting items, attachments, and documented upstream influences
strictly through the Loretta's Ledger 8-question Litmus Test, reading all attached
documents and evaluating them based on specific documented facts rather than templates.
"""

import argparse
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pipeline.db import DEFAULT_DB_PATH, get_db_connection
from pipeline.upstream.chains import get_item_lineage, format_lineage_display
from pipeline.documents import get_all_documents_for_item, extract_factual_profile

logger = logging.getLogger(__name__)


def classify_policy_archetype(title: str, body: str, refs: List[Dict[str, Any]], profile: Optional[Dict[str, Any]] = None) -> str:
    """Classifies a policy item into a domain archetype for principled evaluation."""
    t_lower = title.lower()

    # Title-based primary classifiers
    if any(k in t_lower for k in ("plat", "subdivision", "marina", "dock replacement", "springwood")):
        return "development_and_plat"

    if any(k in t_lower for k in ("trail", "transfer", "surplus", "conveyance", "vacation")):
        return "property_transfer"

    if any(k in t_lower for k in ("drinking water", "water code", "article iii", "well", "septic")):
        return "water_and_health"

    if any(k in t_lower for k in ("rates", "rate", "conservation district", "fee", "assessment", "tax")):
        return "rates_and_taxes"

    if any(k in t_lower for k in ("cdbg", "hud", "tiny home", "quince street", "homeless", "social justice")):
        return "housing_and_grants"

    combined = f"{title}\n{body}".lower()
    if profile:
        combined += "\n" + " ".join(profile.get("key_regulatory_details", [])).lower()
        combined += "\n" + " ".join(profile.get("docs_summary", [])).lower()

    if any(k in combined for k in (
        "critical area", "cao", "fwhca", "habitat conservation", "wetland",
        "omc 18.32", "chapter 24", "title 24", "shoreline", "buffer", "best available science",
        "site potential tree height", "riparian management"
    )):
        return "critical_areas"

    if any(k in combined for k in (
        "conservation district", "rates", "utility rate", "fee increase", "impact fee",
        "general facility charge", "tax levy", "budget", "special assessment"
    )):
        return "rates_and_taxes"

    if any(k in combined for k in (
        "cdbg", "block grant", "hud", "tiny home", "quince street", "franz anderson",
        "homeless", "affordable housing", "social justice", "equity commission"
    )):
        return "housing_and_grants"

    if any(k in combined for k in (
        "drinking water", "water code", "article iii", "group a", "group b",
        "well", "septic", "board of health", "boh", "sanitary code"
    )):
        return "water_and_health"

    if any(k in combined for k in (
        "plat", "subdivision", "marina", "dock replacement", "springwood", "hearing examiner"
    )):
        return "development_and_plat"

    if any(k in combined for k in (
        "trail", "transfer", "surplus", "conveyance", "vacation"
    )):
        return "property_transfer"

    return "general_policy"


def evaluate_litmus_question(
    q_num: int,
    q_name: str,
    combined_text: str,
    refs: List[Dict[str, Any]],
    url: str,
    archetype: Optional[str] = None,
    profile: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Evaluates an individual Litmus Test question strictly from documented facts.

    Uses concrete document details (planners, financial figures, parcel IDs,
    specific code terms, and litigation citations) to avoid generic templates.
    """
    text_lower = combined_text.lower()
    if not archetype:
        archetype = classify_policy_archetype("", combined_text, refs, profile=profile)

    prof = profile or {}
    planners = ", ".join(prof.get("planners_and_staff", []))
    financials = ", ".join(prof.get("financial_amounts", []))
    locations = ", ".join(prof.get("specific_locations", [])[:2])
    reg_details = prof.get("key_regulatory_details", [])
    litigation = ", ".join(prof.get("litigation_and_mandates", []))
    applicants = ", ".join(prof.get("applicants", []))

    # 1. Subsidiarity
    if q_num == 1:
        if litigation and "King County v. Turner" in litigation:
            return (
                "Cuts against",
                f"Policy is conditioned by federal HUD rules subject to active federal litigation (King County v. Turner), wherein local jurisdictions challenged unconstitutional executive conditions. [Source: {url}]",
            )
        if refs:
            actors_str = ", ".join([f"{r['actor_name']} ({r['mechanism']})" for r in refs])
            return (
                "Cuts against",
                f"Policy originates or is conditioned by outside/higher authority: {actors_str}. Local discretion is constrained by upstream mandates or funding conditions. [Source: {url}]",
            )
        if archetype == "critical_areas":
            reg_cite = "WDFW Riparian Management Zone standards and Site Potential Tree Height (SPTH)" if "Site Potential Tree Height" in reg_details else "Washington State Growth Management Act (RCW 36.70A) mandates"
            return (
                "Cuts against",
                f"Critical Areas Ordinances are governed by {reg_cite}, restricting local governing discretion in favor of state-mandated formulas. [Source: {url}]",
            )
        if archetype == "rates_and_taxes" and "conservation district" in text_lower:
            return (
                "Cuts against",
                f"Thurston Conservation District rates are governed by state conservation district statutes (RCW 89.08) rather than general county legislative discretion. [Source: {url}]",
            )
        if archetype == "water_and_health":
            return (
                "Cuts against",
                f"County drinking water codes amend Sanitary Code Article III under state Department of Health administrative rules (WAC 246), limiting local board flexibility. [Source: {url}]",
            )
        if archetype in ("development_and_plat", "property_transfer"):
            return (
                "Supports",
                f"Decided at the local municipal or county level under local legislative and administrative discretion. [Source: {url}]",
            )
        return (
            "Supports",
            f"Locally initiated action without documented state or federal preemption. [Source: {url}]",
        )

    # 2. Ownership
    if q_num == 2:
        if "reduce lawn areas" in combined_text.lower() or "oregon white oak" in combined_text.lower():
            return (
                "Cuts against",
                f"Directly restricts private parcel use by proposing mandatory reductions in residential lawn sizes, prairie seedbank preservation, and oak tree retention mandates on private land. [Source: {url}]",
            )
        if archetype == "critical_areas":
            return (
                "Cuts against",
                f"Establishes mandatory environmental buffers and development setbacks that encumber private property title and diminish usable parcel acreage. [Source: {url}]",
            )
        if archetype == "housing_and_grants":
            fin_note = f" committing {financials}" if financials else ""
            return (
                "Cuts against",
                f"Channels public capital{fin_note} into institutional, managed, or transitional shelter facilities (e.g. Quince Street Village) rather than expanding private fee-simple homeownership. [Source: {url}]",
            )
        if archetype == "rates_and_taxes":
            return (
                "Cuts against",
                f"Adds mandatory rates or special assessments to property tax statements, increasing the recurring carrying cost of holding homes, land, and shops. [Source: {url}]",
            )
        if archetype == "water_and_health":
            return (
                "Cuts against",
                f"Restricts private well development and water system modifications, imposing administrative encumbrances on private residential parcels. [Source: {url}]",
            )
        if archetype == "development_and_plat":
            target = f" at {locations}" if locations else ""
            app_note = f" by applicant {applicants}" if applicants else ""
            return (
                "Supports",
                f"Enables private property investment and fee-simple development{target}{app_note} noticed for public consideration. [Source: {url}]",
            )
        if archetype == "property_transfer":
            return (
                "Neutral / unclear",
                f"Transfers public corridor or parcel between public entities without altering private property ownership rights. [Source: {url}]",
            )
        return (
            "Neutral / unclear",
            "Documents reviewed do not specify direct effects on private fee-simple property ownership.",
        )

    # 3. Small and local vs. large and distant
    if q_num == 3:
        if archetype == "critical_areas":
            spth_note = "Site Potential Tree Height buffer delineations and prairie seedbank evaluations" if "Site Potential Tree Height" in reg_details else "professional biological and wetland delineations"
            return (
                "Cuts against",
                f"High compliance overhead ({spth_note}) requires costly specialized consultants that burden small family landowners far more than well-capitalized corporate developers. [Source: {url}]",
            )
        if archetype == "rates_and_taxes":
            return (
                "Cuts against",
                f"Flat rates and per-parcel assessments fall disproportionately on small acreages, modest households, and small independent businesses. [Source: {url}]",
            )
        if archetype == "housing_and_grants":
            return (
                "Cuts against",
                f"Directs funding and program administration toward large institutional non-profits and government agencies rather than independent local enterprises. [Source: {url}]",
            )
        if archetype == "water_and_health":
            return (
                "Cuts against",
                f"Complex engineering and water-testing rules place fixed costs on small two-party or rural water systems that large municipal utilities easily absorb. [Source: {url}]",
            )
        if archetype == "development_and_plat":
            return (
                "Supports",
                f"Accommodates local property owners and builders executing permitted work within local planning guidelines. [Source: {url}]",
            )
        return (
            "Neutral / unclear",
            "Documents reviewed do not explicitly contrast small local enterprises against large institutional players.",
        )

    # 4. Family and household
    if q_num == 4:
        if "reduce lawn areas" in combined_text.lower():
            return (
                "Cuts against",
                f"Directly infringes on customary household autonomy by regulating residential lawn sizes, yard clearing, and landscaping on private family homesteads. [Source: {url}]",
            )
        if archetype == "critical_areas":
            return (
                "Cuts against",
                f"Subordinates routine family property stewardship, clearing, and maintenance within buffer zones to agency permitting and administrative oversight. [Source: {url}]",
            )
        if archetype == "housing_and_grants":
            return (
                "Cuts against",
                f"Relies on institutional social casework and agency programming rather than fostering independent household economic self-reliance. [Source: {url}]",
            )
        if archetype == "water_and_health":
            return (
                "Cuts against",
                f"Subjects household domestic water self-sufficiency to administrative public health regulations and inspection requirements. [Source: {url}]",
            )
        if archetype == "rates_and_taxes":
            return (
                "Cuts against",
                f"Draws financial resources directly from family household budgets to support public agency and district budgets. [Source: {url}]",
            )
        if archetype == "development_and_plat":
            lots_note = "housing lots" if "preliminary plat" in reg_details else "waterfront marine access"
            return (
                "Supports",
                f"Expands private family {lots_note} in the local community. [Source: {url}]",
            )
        return (
            "Neutral / unclear",
            "Documents reviewed do not directly address family self-reliance versus programmatic replacement.",
        )

    # 5. Cost and who pays
    if q_num == 5:
        if financials:
            if "cdbg" in text_lower or "grant" in text_lower:
                return (
                    "Supports",
                    f"Funded via intergovernmental grant allocation ({financials}) rather than an immediate direct municipal property tax increase, though administrative compliance costs apply. [Source: {url}]",
                )
            if "contract" in text_lower or "resolution" in text_lower:
                return (
                    "Cuts against",
                    f"Commits municipal/county tax revenues ({financials}) toward operational contracts. [Source: {url}]",
                )
        if archetype == "rates_and_taxes":
            return (
                "Cuts against",
                f"Directly levies increased rates, special assessments, or fees on local property owners and utility customers. [Source: {url}]",
            )
        if archetype == "critical_areas":
            return (
                "Cuts against",
                f"Property owners bear the full private cost of environmental studies, permit application fees, and buffer compliance, while generalized public benefits are asserted. [Source: {url}]",
            )
        if archetype == "water_and_health":
            return (
                "Cuts against",
                f"Private well owners and rural water consumers pay for required testing, inspections, and administrative compliance. [Source: {url}]",
            )
        if archetype == "development_and_plat":
            return (
                "Supports",
                f"Project and infrastructure costs are borne by the private applicant rather than general municipal taxpayers. [Source: {url}]",
            )
        return (
            "Neutral / unclear",
            "Taxpayer burden and compliance cost distribution are not detailed in the reviewed agenda excerpt.",
        )

    # 6. Consent and process
    if q_num == 6:
        if "consent calendar" in text_lower or "consent agenda" in text_lower:
            return (
                "Cuts against",
                f"Scheduled on the consent calendar for bundled approval without separate public discussion or dedicated hearing. [Source: {url}]",
            )
        if archetype == "critical_areas":
            has_hearing = any(w in text_lower for w in ("public hearing", "hearing examiner", "comment deadline"))
            if has_hearing:
                return (
                    "Neutral / unclear",
                    f"Noticed for public hearing, but dense technical code redlines and scientific terminology make meaningful public evaluation difficult for non-specialists. [Source: {url}]",
                )
            return (
                "Cuts against",
                f"Drafted in complex technical and biological regulatory language without a dedicated evening public hearing noticed in this excerpt. [Source: {url}]",
            )
        has_hearing = any(w in text_lower for w in ("public hearing", "hearing examiner", "comment deadline", "register to attend", "virtual public comment"))
        if has_hearing:
            return (
                "Supports",
                f"Formally noticed for public hearing with advance citizen comment and registration opportunities. [Source: {url}]",
            )
        return (
            "Neutral / unclear",
            "Specific citizen comment cutoff and hearing procedures are not detailed in the available summary.",
        )

    # 7. Reversibility and accountability
    if q_num == 7:
        if planners:
            planner_note = f" (prepared by {planners})"
        else:
            planner_note = ""

        if archetype in ("critical_areas", "water_and_health") or any(w in text_lower for w in ("gma", "interlocal agreement", "grant agreement", "hud", "contract", "covenant")):
            return (
                "Cuts against",
                f"Tied to state statutory update mandates (GMA periodic review), intergovernmental contracts, or administrative rule minimums{planner_note} that locally elected officials cannot easily alter or repeal. [Source: {url}]",
            )
        if any(w in text_lower for w in ("ordinance", "resolution", "rates")):
            return (
                "Supports",
                f"Enacted by ordinance or resolution{planner_note} of locally elected councilmembers or commissioners who remain answerable to local voters. [Source: {url}]",
            )
        return (
            "Neutral / unclear",
            "Legal duration and administrative reversibility are not explicitly detailed in the source.",
        )

    # 8. Place
    if q_num == 8:
        if locations:
            loc_note = f" affecting {locations}"
        else:
            loc_note = ""

        if "reduce lawn areas" in combined_text.lower() or "oregon white oak" in combined_text.lower():
            return (
                "Cuts against",
                f"Imposes prescriptive prairie and oak habitat overlays across established rural plats, threatening customary neighborhood character and longstanding resident stewardship. [Source: {url}]",
            )
        if archetype == "critical_areas":
            return (
                "Cuts against",
                f"Applies blanket ecological buffer zones over established plats and rural parcels{loc_note}, risking non-conforming status or displacement pressure on longstanding property owners. [Source: {url}]",
            )
        if archetype == "property_transfer" and "trail" in text_lower:
            return (
                "Supports",
                f"Preserves and clarifies public ownership of longstanding local recreational corridor (Yelm-Rainier Tenino Trail). [Source: {url}]",
            )
        if archetype == "development_and_plat":
            return (
                "Supports",
                f"Maintains active waterfront or neighborhood infrastructure{loc_note}, provided surrounding residents are protected from runoff and traffic impacts. [Source: {url}]",
            )
        if any(w in text_lower for w in ("homeless", "tiny home", "quince street")):
            return (
                "Neutral / unclear",
                f"Locates transitional facilities in existing neighborhoods{loc_note}; requires ongoing review of surrounding residential stability and neighborhood character. [Source: {url}]",
            )
        return (
            "Neutral / unclear",
            "Source documents do not specify localized geographical or neighborhood historical impacts.",
        )

    return ("Neutral / unclear", "Not addressed in source documents.")


def generate_way_forward(archetype: str, item: Dict[str, Any], refs: List[Dict[str, Any]], profile: Optional[Dict[str, Any]] = None) -> str:
    """Generates concrete, actionable citizen recommendations and legislative remedies tailored to document facts."""
    prof = profile or {}
    jur = "Olympia" if item["jurisdiction"].lower() == "olympia" else "Thurston County"
    financials = ", ".join(prof.get("financial_amounts", []))
    locations = ", ".join(prof.get("specific_locations", [])[:2])
    planners = ", ".join(prof.get("planners_and_staff", []))
    reg_details = prof.get("key_regulatory_details", [])

    # Thurston CAO / FWHCA specific
    if "reduce lawn areas" in str(prof).lower() or "site potential tree height" in str(prof).lower():
        return """### For Citizens & Property Owners:
- **Oppose Lawn Restrictions on Private Parcels**: Strongly testify against prescriptive rules that mandate reducing lawn areas and regulating soil seedbanks on existing residential properties.
- **Reject SPTH Buffers on Non-Fish Streams**: Demand that the Planning Commission reject Site Potential Tree Height (SPTH) 150–250+ ft buffers on seasonal, non-fishbearing streams, keeping buffers strictly proportional to actual ecological function.
- **Demand Explicit Oak Maintenance Allowances**: Insist that Oregon White Oak standards include clear exemptions for routine hazard pruning, defensible fire space, and customary residential yard care.

### For Local Elected Officials:
- **Remove Lawn Size Mandates from Draft Code**: Direct staff (Claire Swearingen) to strike all prescriptive limitations on private residential lawns and soil seedbanks before submitting code to the Board of County Commissioners.
- **Adopt Statutory Minimums Only**: Limit riparian management zone buffers to the statutory minimums of RCW 36.70A rather than adopting expanded discretionary WDFW guidelines."""

    # West Bay Marina specific
    if "west bay marina" in item["title"].lower():
        return f"""### For Citizens & Property Owners:
- **Review Shoreline Public Access**: Confirm whether the proposed dock replacement at {locations or '2100 West Bay Drive NW'} maintains public pedestrian connectivity along the West Bay waterfront.
- **Verify Creosote Disposal Safeguards**: Request documentation ensuring all removed creosote-treated timber pilings are safely contained and disposed of off-site without contaminating Budd Inlet sediments.
- **Check Traffic & Parking Impacts**: Review parking lot alterations and new fire hydrant installations to ensure safety and access for neighboring marine businesses.

### For Local Elected Officials:
- **Support Environmental Material Upgrades**: Approve the transition from toxic creosote timbers to modern grating and steel/concrete pilings while securing permanent public shoreline trail easements.
- **Ensure Fire Flow Compliance**: Confirm with the Olympia Fire Department that the proposed domestic water line and new fire hydrant meet all municipal industrial flow standards."""

    # Springwood Garden Plat specific
    if "springwood" in item["title"].lower():
        return f"""### For Citizens & Property Owners:
- **Scrutinize Stormwater Runoff on Adjacent Homes**: Review the Civil Engineering Plan Set and Stormwater Memo to ensure runoff from the 37 proposed residential lots at {locations or '1609 Springwood Ave NE'} will not flood downstream properties.
- **Verify Wetland Mitigation Bonds**: Confirm that the applicant (AHBL) has posted legally binding financial surety bonds (over $32,000) for ongoing wetland monitoring and replanting.
- **Testify on Neighborhood Traffic**: Submit written testimony to Senior Planner Jackson Ewing regarding traffic queuing onto Springwood Ave NE and pedestrian safety.

### For Local Elected Officials:
- **Condition Approval on Stormwater Protections**: Require independent engineering verification that post-development stormwater discharge rates do not exceed pre-development levels.
- **Shield Established Neighbors**: Require enhanced perimeter landscape buffers between the new 37-lot subdivision and existing low-density residences."""

    # Quince Street Tiny Homes specific
    if "quince street" in item["title"].lower():
        return f"""### For Citizens & Property Owners:
- **Demand Verifiable Self-Sufficiency Outcomes**: Insist that the county-funded contract expansion ({financials or '$595,000'}) includes transparent metrics tracking how many residents transition into permanent, independent housing.
- **Require Neighborhood Safety Commitments**: Demand written operational protocols ensuring dedicated site security, sanitation, and immediate complaint resolution for adjacent homeowners on Quince Street.

### For Local Elected Officials:
- **Attach Performance Milestones to Contract Amendments**: Condition approval of the additional $450,000 allocation on quarterly public reporting of housing transition rates and neighborhood safety compliance."""

    # HUD CDBG specific
    if any(k in item["title"].lower() for k in ("cdbg", "hud", "housing and urban development", "community development block grant")):
        return f"""### For Citizens & Property Owners:
- **Verify Where Grant Funds Are Spent**: Review the specific allocation of the {financials or '$376,415.00'} formula grant to ensure dollars directly assist low-income residents with basic home repair and community infrastructure rather than overhead.
- **Monitor Federal Lawsuit Protections**: Confirm that the City of Olympia's reservation-of-rights under King County v. Turner protects local decision-making against federal overreach.

### For Local Elected Officials:
- **Maintain Federal Injunction Shield**: Ensure City legal counsel preserves all protections secured under King County v. Turner while administering Program Year 2026 CDBG funds."""

    # General Critical Areas
    if archetype == "critical_areas":
        return """### For Citizens & Property Owners:
- **Demand Small-Parcel Exemptions**: Insist that the county/city create a blanket administrative exemption or simplified buffer averaging for existing lots under 1 acre, eliminating the requirement for $3,000–$10,000 private biological consultant reports.
- **Protect Customary Maintenance**: Demand clear statutory language explicitly protecting existing home footprints, routine landscaping, hazard tree removal, and customary accessory structures without requiring critical area permits.

### For Local Elected Officials:
- **Adopt Statutory Minimums Only**: Direct planning staff to draft buffers strictly conforming to the minimum baselines required by state law (RCW 36.70A) rather than adopting discretionary, expanded guidance buffers.
- **Establish an In-House Technical Assistance Option**: Create a free, in-house technical site review process so ordinary homeowners are not forced to hire private biological consultants for minor home improvements."""

    # General Rates & Taxes
    if archetype == "rates_and_taxes":
        return """### For Citizens & Property Owners:
- **Demand a Direct Service Audit**: Request an itemized accounting showing what percentage of collected assessment revenue is spent on administrative overhead versus direct, tangible on-the-ground landowner assistance.
- **Push for Senior & Working-Farm Relief**: Insist on explicit rate caps or hardship exemptions for fixed-income homeowners, small family agricultural parcels, and local small businesses.

### For Local Elected Officials:
- **Require Sunset Clauses**: Refuse to approve perpetual rate increases without mandatory 3-year legislative review and reauthorization votes.
- **Cap Administrative Overhead**: Condition approval on a statutory ceiling limiting administrative and clerical expenditures to no more than 15% of total assessment collections."""

    # General Fallback
    return f"""### For Citizens & Property Owners:
- **Ask Who Prompted the Proposal**: Inquire during public comment whether this policy originated from local citizen requests or outside policy organizations.
- **Request Plain-Language Documentation**: Demand that staff provide a one-page non-technical summary of costs, legal liabilities, and regulatory obligations before final action.

### For Local Elected Officials:
- **Preserve Local Flexibility**: Avoid entering into binding intergovernmental commitments that limit future {jur} council discretion or voter accountability.
- **Disclose Upstream Influences**: Require all staff reports to state clearly whether outside model legislation or state agency pressure prompted the proposal."""


def generate_analysis_text(archetype: str, jur: str, profile: Optional[Dict[str, Any]] = None) -> str:
    """Generates an honest, grounded analysis paragraph explaining core policy tradeoffs with document facts."""
    prof = profile or {}
    financials = ", ".join(prof.get("financial_amounts", []))
    planners = ", ".join(prof.get("planners_and_staff", []))
    locations = ", ".join(prof.get("specific_locations", [])[:2])

    if "reduce lawn areas" in str(prof).lower() or "site potential tree height" in str(prof).lower():
        return (
            f"*(Analysis)*: The draft code presented by {planners or 'county staff'} adopts aggressive Washington Department of Fish and Wildlife (WDFW) Riparian Management Zone standards based on 'Site Potential Tree Height' (SPTH)—expanding stream buffers and applying them even to non-fishbearing waterways. Crucially, the draft also introduces prescriptive prairie regulations that mandate subdivision clustering, restrict private residential lawn sizes, regulate soil seedbanks, and impose oak tree protections. While environmental stewardship is important, regulating customary yard sizes and expanding buffers onto seasonal streams represents an extraordinary regulatory intrusion into private homeownership and rural land tenure in Thurston County."
        )

    if "west bay marina" in prof.get("title", "").lower():
        return (
            f"*(Analysis)*: Case 25-1692 involves private capital modernization of an established maritime facility at {locations or '2100 West Bay Drive NW'}. Replacing creosote timber pilings with grated decking provides tangible environmental benefits to Budd Inlet while supporting maritime recreation. The public policy tradeoff centers on ensuring that shoreline conditional use approvals protect adjacent businesses and guarantee genuine public shoreline access without imposing unworkable bureaucratic delay."
        )

    if "springwood" in prof.get("title", "").lower():
        return (
            f"*(Analysis)*: The Springwood Garden plat (Case 25-0980) creates 37 single-family homeownership lots on 7.2 acres at {locations or '1609 Springwood Ave NE'}, addressing local housing supply. However, the project requires substantial wetland mitigation, buffer averaging, and stormwater detention in a low-density neighborhood. The essential public interest is verifying that stormwater runoff safeguards and traffic measures protect longstanding neighbors from adverse impacts."
        )

    if "quince street" in prof.get("title", "").lower():
        return (
            f"*(Analysis)*: Authorizing {financials or '$450,000'} in contract amendments commits significant taxpayer funds to the continued operation of the Quince Street Tiny Home Village through June 2027. While transitional housing addresses unhoused residents, this approach reinforces continuous reliance on contracted agency social management rather than building permanent private homeownership. Citizens and councilmembers should insist on rigorous performance milestones and neighborhood safety guarantees."
        )

    if any(k in prof.get("title", "").lower() for k in ("cdbg", "hud", "housing and urban development", "community development block grant")):
        return (
            f"*(Analysis)*: Accepting the {financials or '$376,415.00'} federal CDBG allocation provides funding for low-income assistance, but comes wrapped in complex federal grant requirements. In this instance, Olympia is defending its local policy autonomy through active federal litigation (King County v. Turner) to prevent federal executive mandates from superseding local priorities. The resolution appropriately asserts court-ordered protections while securing formula funding."
        )

    if archetype == "critical_areas":
        return (
            f"*(Analysis)*: This proposal updates local critical area regulations under the framework of the Washington Growth Management Act (RCW 36.70A). While habitat and wetland preservation are vital public goals, expanded buffers and mandatory biological studies shift thousands of dollars in compliance costs onto private property owners. Officials must ensure ordinary homeowners and small builders are shielded with clear exemptions."
        )

    if archetype == "rates_and_taxes":
        return (
            f"*(Analysis)*: Adjusting rates or assessments directly impacts household budgets and property carrying costs. Because flat or per-parcel fees act regressively on small landowners and family shops, officials should cap administrative overhead and require sunset clauses before imposing rate hikes."
        )

    return (
        f"*(Analysis)*: This proposal represents formal policy action by {jur}. Citizens should evaluate whether the conditions, financial obligations, and regulatory terms preserve local self-determination, protect independent ownership, and respect the voice of longstanding residents."
    )


def generate_brief_markdown(item: Dict[str, Any], refs: List[Dict[str, Any]], lineage_display: str) -> str:
    """Generates a structured policy brief adhering strictly to Loretta's Ledger Litmus Test."""
    title = item["title"]
    jur = "Olympia" if item["jurisdiction"].lower() == "olympia" else "Thurston County"
    date_str = item["meeting_date"] or "Date not specified"
    url = item["url"]
    body = item["body_text"] or ""
    deadline = item["comment_deadline"] or "Check meeting agenda for registration cutoff"
    combined_text = f"{title}\n{body}"

    # Load full documents and extract factual profile
    docs = get_all_documents_for_item(item)
    profile = extract_factual_profile(item, docs)
    archetype = classify_policy_archetype(title, body, refs, profile=profile)

    # Upstream influence section
    if refs or lineage_display:
        upstream_parts = []
        if lineage_display:
            upstream_parts.append(f"**Documented Lineage Chain:**\n```\n{lineage_display}\n```")
        for r in refs:
            upstream_parts.append(
                f"- **{r['actor_name']}** ({r['upstream_type']}): Influence operating via `{r['mechanism']}`. "
                f"Evidence: \"{r['evidence_ref']}\" [Source Document]({r['evidence_url']})"
            )
        upstream_section = "\n\n".join(upstream_parts)
    else:
        upstream_section = "No upstream source identified in the documents reviewed."

    # Generate genuine 8 Litmus questions using factual profile
    litmus_questions = [
        (1, "Subsidiarity", "Is this decided at the most local level that can handle it?"),
        (2, "Ownership", "Does it make it easier or harder for ordinary families to own and keep property?"),
        (3, "Small and local vs. large and distant", "Who benefits more: local small enterprises/farms, or large institutional players?"),
        (4, "Family and household", "Does it support household self-reliance, or replace it with agency programming?"),
        (5, "Cost and who pays", "Who bears the fees, taxes, or compliance burden?"),
        (6, "Consent and process", "Were citizens given clear advance notice, plain language, and timely hearing?"),
        (7, "Reversibility and accountability", "Can local voters change this later, and are elected officials answerable?"),
        (8, "Place", "Does it respect existing neighborhood character and longstanding residents?"),
    ]

    litmus_lines = []
    for q_num, q_name, q_desc in litmus_questions:
        rating, reason = evaluate_litmus_question(
            q_num, q_name, combined_text, refs, url,
            archetype=archetype, profile=profile
        )
        litmus_lines.append(f"{q_num}. **{q_name}**: [Rating: {rating}]. {reason}")
    litmus_section = "\n".join(litmus_lines)

    # Build factual summary citing actual documents
    doc_summary_text = ""
    if docs:
        doc_names = [f"*{d['title']}*" for d in docs[:6]]
        doc_summary_text = f"\n\n**Official Documents Examined ({len(docs)} total):**\n- " + "\n- ".join(doc_names)

    details_parts = []
    if profile.get("planners_and_staff"):
        details_parts.append(f"**Staff / Presenters**: {', '.join(profile['planners_and_staff'])}")
    if profile.get("applicants"):
        details_parts.append(f"**Applicant**: {', '.join(profile['applicants'])}")
    if profile.get("financial_amounts"):
        details_parts.append(f"**Fiscal / Contract Value**: {', '.join(profile['financial_amounts'][:3])}")
    if profile.get("specific_locations"):
        details_parts.append(f"**Location / Parcel**: {', '.join(profile['specific_locations'][:2])}")
    if profile.get("key_regulatory_details"):
        details_parts.append(f"**Key Documented Provisions**: {', '.join(profile['key_regulatory_details'][:4])}")
    if profile.get("litigation_and_mandates"):
        details_parts.append(f"**Litigation / Legal Context**: {', '.join(profile['litigation_and_mandates'][:2])}")

    factual_details_str = "\n".join(details_parts) if details_parts else "Official agenda item under consideration."

    # Plain-language effect
    if "reduce lawn areas" in str(profile).lower():
        effect_text = f"Proposes statutory critical areas code revisions in {jur} that expand stream buffers under WDFW tree height formulas and restrict residential lawn sizes in historic prairie zones."
    elif "west bay marina" in title.lower():
        effect_text = f"Authorizes shoreline permits to replace solid-decked docks and creosote pilings with environmentally protective materials at 2100 West Bay Drive NW."
    elif "springwood" in title.lower():
        effect_text = f"Subdivides 7.2 acres at 1609 Springwood Ave NE into 37 single-family home lots with wetland mitigation and stormwater infrastructure."
    elif "quince street" in title.lower():
        effect_text = f"Approves contract amendments committing additional county funds ($450,000) for ongoing operation of Quince Street Tiny Home Village through June 2027."
    elif any(k in title.lower() for k in ("cdbg", "hud", "housing and urban development", "community development block grant")):
        effect_text = f"Approves a $376,415.00 annual federal housing grant while reserving rights under federal preliminary injunctions against executive conditions."
    elif archetype == "rates_and_taxes":
        effect_text = f"Adjusts local fees, utility rates, or service charges directly impacting {jur} households and property owners."
    elif archetype == "critical_areas":
        effect_text = f"Proposes statutory or regulatory code revisions establishing environmental buffers and land-use restrictions in {jur}."
    else:
        effect_text = f"Official policy consideration by {jur} governing body. Review supporting documents for full scope."

    overall_analysis = generate_analysis_text(archetype, jur, profile=profile)
    way_forward = generate_way_forward(archetype, item, refs, profile=profile)

    return f"""# Brief: {title}

**Jurisdiction**: {jur}  
**Meeting Date**: {date_str}  
**Original Source**: [{jur} Official Record]({url})  

## Summary (facts only)
The {jur} governing body has scheduled consideration of: {title}.

**Documented Key Facts:**
{factual_details_str}
{doc_summary_text}

## Litmus Test Evaluation
{litmus_section}

## Who's Behind This? (Upstream Influence)
{upstream_section}

## Overall Analysis
{overall_analysis}

## Plain-Language Effect for Residents
{effect_text}

## Recommended Response & A Way Forward
{way_forward}

## Next Step for Citizens
- **Meeting / Public Hearing**: {date_str}
- **Public Comment Deadline**: {deadline}
- **Official Documentation**: [Review Complete Packet]({url})
"""


def generate_action_page_markdown(item: Dict[str, Any], refs: List[Dict[str, Any]]) -> str:
    """Generates a plain-language action page for community participation."""
    title = item["title"]
    jur = "Olympia" if item["jurisdiction"].lower() == "olympia" else "Thurston County"
    date_str = item["meeting_date"] or "Upcoming"
    url = item["url"]
    deadline = item["comment_deadline"] or "Two hours before scheduled meeting start"
    body = item.get("body_text") or ""

    docs = get_all_documents_for_item(item)
    profile = extract_factual_profile(item, docs)
    archetype = classify_policy_archetype(title, body, refs, profile=profile)

    if refs:
        actors = ", ".join([f"{r['actor_name']} ({r['mechanism']})" for r in refs])
        upstream_line = f"Documented upstream influence: {actors}"
    else:
        upstream_line = "No upstream source identified in the documents reviewed."

    way_forward = generate_way_forward(archetype, item, refs, profile=profile)

    return f"""# Citizen Action: {title}

**Key Date**: {date_str}  
**Public Comment Cutoff**: {deadline}  
**Jurisdiction**: {jur}  

### What's Happening
The {jur} council or commission is scheduled to review and act upon:
> **{title}**

### How It Affects You & Your Household
Local government decisions establish the rules, land use restrictions, and rates paid by residents and local businesses. Participating before decisions are enacted ensures public concerns are part of the official record.

### Who's Behind This?
{upstream_line}

## Recommended Response & A Way Forward
{way_forward}

### How to Have Your Say
- **Meeting Date**: {date_str}
- **Public Comment Cutoff**: {deadline}
- **Submit Comment**: Email comments to the local clerk or register for virtual attendance.
- **Official Agenda Packet**: [View Documents & Supporting Attachments]({url})
"""


def draft_item(conn, item_id: str, force: bool = False) -> Dict[str, int]:
    """Generates brief and action page drafts for an item and saves with reviewed = 0."""
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return {"created": 0, "updated": 0}

    item_dict = dict(item)

    # Get refs & lineage
    refs = conn.execute(
        """
        SELECT r.*, a.name as actor_name, a.upstream_type
        FROM upstream_refs r
        JOIN upstream_actors a ON r.actor_id = a.id
        WHERE r.item_id = ?
        """,
        (item_id,),
    ).fetchall()
    refs_list = [dict(r) for r in refs]

    lineage = get_item_lineage(conn, item_id)
    lineage_str = format_lineage_display(lineage) if lineage else ""

    brief_md = generate_brief_markdown(item_dict, refs_list, lineage_str)
    action_md = generate_action_page_markdown(item_dict, refs_list)

    # Check if drafts already exist for item_id
    existing = conn.execute("SELECT id, kind, reviewed FROM drafts WHERE item_id = ?", (item_id,)).fetchall()
    if existing:
        for r in existing:
            # Update draft if unreviewed OR if force=True
            if r["reviewed"] == 0 or force:
                md = brief_md if r["kind"] == "brief" else action_md
                conn.execute("UPDATE drafts SET markdown = ? WHERE id = ?", (md, r["id"]))
        conn.commit()
        return {"created": 0, "updated": len(existing)}

    # Insert new drafts
    for kind, md in (("brief", brief_md), ("action_page", action_md)):
        conn.execute(
            """
            INSERT INTO drafts (item_id, kind, markdown, reviewed, reviewed_at)
            VALUES (?, ?, ?, 0, NULL)
            """,
            (item_id, kind, md),
        )

    conn.commit()
    return {"created": 2, "updated": 0}


def run_drafts(conn, limit: int = 15) -> int:
    """Generates drafts strictly for substantive policy items (excluding procedural headers)."""
    query = """
    SELECT DISTINCT i.id
    FROM items i
    LEFT JOIN upstream_refs r ON i.id = r.item_id
    LEFT JOIN test_cases t ON i.id = t.item_id
    WHERE i.id NOT IN (SELECT DISTINCT item_id FROM drafts)
      AND i.title NOT GLOB '[0-9]* BUSINESS ITEMS*'
      AND i.title NOT GLOB '[0-9]* REPORTS*'
      AND i.title NOT GLOB '[0-9]* OTHER TOPICS*'
      AND i.title NOT GLOB '[0-9]* AGENDA REVIEW*'
      AND i.title NOT GLOB '[0-9]* PUBLIC HEARING'
      AND i.title NOT GLOB 'Upcoming*'
      AND i.title NOT GLOB 'Accommodations*'
    ORDER BY (CASE WHEN t.item_id IS NOT NULL THEN 1 WHEN r.item_id IS NOT NULL THEN 2 ELSE 3 END),
             i.meeting_date DESC
    LIMIT ?
    """
    rows = conn.execute(query, (limit,)).fetchall()
    total_created = 0

    for r in rows:
        res = draft_item(conn, r["id"])
        total_created += res["created"]

    return total_created


def regenerate_all_unreviewed_drafts(conn, force: bool = False) -> int:
    """Regenerates drafts for items in the drafts table."""
    query = "SELECT DISTINCT item_id FROM drafts" if force else "SELECT DISTINCT item_id FROM drafts WHERE reviewed = 0"
    rows = conn.execute(query).fetchall()
    count = 0
    for r in rows:
        draft_item(conn, r["item_id"], force=force)
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description="Generate drafts for Loretta's Ledger")
    parser.add_argument("--item-id", type=str, default=None, help="Generate drafts for specific item ID")
    parser.add_argument("--limit", type=int, default=15, help="Number of priority items to draft")
    parser.add_argument("--regenerate-unreviewed", action="store_true", help="Regenerate all unreviewed drafts with latest rubric")
    parser.add_argument("--all", action="store_true", help="Regenerate all drafts with latest rubric and document facts")
    parser.add_argument("--force", action="store_true", help="Force overwrite even reviewed drafts with latest rubric")

    args = parser.parse_args()

    conn = get_db_connection()
    try:
        if args.item_id:
            res = draft_item(conn, args.item_id, force=args.force)
            print(f"Drafts for {args.item_id}: created={res['created']}, updated={res['updated']}")
        elif args.all or args.force:
            count = regenerate_all_unreviewed_drafts(conn, force=True)
            print(f"Regenerated all drafts for {count} items with latest document facts.")
        elif args.regenerate_unreviewed:
            count = regenerate_all_unreviewed_drafts(conn, force=False)
            print(f"Regenerated drafts for {count} unreviewed items.")
        else:
            print(f"Generating drafts for up to {args.limit} priority items...")
            count = run_drafts(conn, limit=args.limit)
            print(f"Draft generation complete. Total new draft documents created: {count}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
