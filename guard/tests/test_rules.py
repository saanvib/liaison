import pytest

from contracts import GuardCategory, GuardRequest, Source, Topic, Verdict
from guard.rules import load_policy, load_rules, screen_categories

RULES = {rule.category: rule for rule in load_rules()}


def req(question="", draft=""):
    return GuardRequest(request_id="r1", source=Source.VENDOR_AGENT, question=question, draft_answer=draft)


# (category, question, draft): each must be flagged by that category's rule.
POSITIVES = [
    (GuardCategory.PATIENT_DATA, "Patient DOB 03/14/1962, can you rerun the assay?", ""),
    (GuardCategory.PATIENT_DATA, "The sample is from patient John Smith, is the result valid?", ""),
    (GuardCategory.PATIENT_DATA, "MRN 00482913 had a failed run, why?", ""),
    (GuardCategory.PATIENT_DATA, "Her SSN is 123-45-6789, please look up the order.", ""),
    (GuardCategory.PATIENT_DATA, "Call the clinic about the patient at (310) 555-0142.", ""),
    (GuardCategory.PATIENT_DATA, "Email jane.doe@mail.com about her hospital sample results.", ""),
    (GuardCategory.PATIENT_DATA, "How do I read this plot?", "The result for Patient: Maria Lopez shows a high Ct."),
    (GuardCategory.PATIENT_DATA, "Sample was from someone born on 4/2/1980, is that relevant?", ""),
    (GuardCategory.RUO_CLINICAL_USE, "Can I use the GX-200 kit for clinical diagnosis?", ""),
    (GuardCategory.RUO_CLINICAL_USE, "Is it okay to rely on this assay to diagnose sepsis?", ""),
    (GuardCategory.RUO_CLINICAL_USE, "Can we report results back to the patients?", ""),
    (GuardCategory.RUO_CLINICAL_USE, "Our CLIA lab wants to run this as an LDT.", ""),
    (GuardCategory.RUO_CLINICAL_USE, "What does the result mean?", "Yes, you can use these results to guide treatment decisions."),
    (GuardCategory.RUO_CLINICAL_USE, "Is it validated?", "The kit is suitable for diagnostic testing in your lab."),
    (GuardCategory.RUO_CLINICAL_USE, "Is it RUO?", "Not for diagnostic procedures, but you can use it for diagnosis of patients anyway."),
    (GuardCategory.OFF_LABEL, "Can I use it off-label for pediatric samples?", ""),
    (GuardCategory.OFF_LABEL, "Can I run saliva instead of plasma?", ""),
    (GuardCategory.OFF_LABEL, "Is there a workaround to use it outside the IFU?", ""),
    (GuardCategory.OFF_LABEL, "Which unvalidated sample types have customers tried?", ""),
    (GuardCategory.OFF_LABEL, "Can I use it on urine?", "Even though it's not validated for urine, many labs use it that way."),
    (GuardCategory.COMPARATIVE_CLAIM, "Is your kit better than the Qiagen one?", ""),
    (GuardCategory.COMPARATIVE_CLAIM, "Does GX-200 outperform other assays?", ""),
    (GuardCategory.COMPARATIVE_CLAIM, "How does it stack up against competitors?", ""),
    (GuardCategory.COMPARATIVE_CLAIM, "Is it sensitive?", "Our assay is more sensitive than any alternative."),
    (GuardCategory.COMPARATIVE_CLAIM, "Why choose you?", "It is the most accurate kit on the market."),
    (GuardCategory.COMPARATIVE_CLAIM, "How does GX-200 do versus Thermo kits?", ""),
    (GuardCategory.PRICING_CONTRACT, "What's the price for 500 kits?", ""),
    (GuardCategory.PRICING_CONTRACT, "Can we get a discount on a bulk order?", ""),
    (GuardCategory.PRICING_CONTRACT, "Please send me a quote.", ""),
    (GuardCategory.PRICING_CONTRACT, "Order status?", "Each kit is $1,200 with net 30 terms."),
    (GuardCategory.PRICING_CONTRACT, "Can you guarantee delivery by Friday?", ""),
    (GuardCategory.PRICING_CONTRACT, "What are the contract terms for a 2-year supply?", ""),
    (GuardCategory.ROADMAP, "What's on your product roadmap?", ""),
    (GuardCategory.ROADMAP, "When will the 384-well version be available?", ""),
    (GuardCategory.ROADMAP, "Any upcoming features for the analyzer software?", ""),
    (GuardCategory.ROADMAP, "Does it support RNA?", "We plan to launch RNA support in Q3."),
    (GuardCategory.ROADMAP, "Is multiplexing possible?", "Multiplexing is in development and coming soon."),
]

# (category, question, draft): that category's rule must NOT flag these near-misses.
NEGATIVES = [
    (GuardCategory.PATIENT_DATA, "How should patient samples be stored before extraction?", ""),
    (GuardCategory.PATIENT_DATA, "What is the lot number format, e.g. LOT 2024-118?", ""),
    (GuardCategory.PATIENT_DATA, "Email support@vendor.com for a replacement manual.", ""),
    (GuardCategory.PATIENT_DATA, "Run 96 samples in 3 batches of 32 each.", ""),
    (GuardCategory.RUO_CLINICAL_USE, "Is the kit RUO?", "GX-200 is for Research Use Only. Not for use in diagnostic procedures."),
    (GuardCategory.RUO_CLINICAL_USE, "What is the extraction protocol?", "Incubate at 56 C for 10 minutes."),
    (GuardCategory.RUO_CLINICAL_USE, "Can I use it in a research setting for mouse tissue?", ""),
    (GuardCategory.RUO_CLINICAL_USE, "Is it a clinical kit?", "It is not intended for clinical use."),
    (GuardCategory.OFF_LABEL, "Which sample types are validated?", "Plasma and serum are validated per the IFU."),
    (GuardCategory.OFF_LABEL, "Where is the label on the box?", ""),
    (GuardCategory.OFF_LABEL, "What does the IFU say about storage?", ""),
    (GuardCategory.COMPARATIVE_CLAIM, "What is the limit of detection?", "The LoD is 50 copies/mL."),
    (GuardCategory.COMPARATIVE_CLAIM, "Is the new protocol faster than the old one we used?", ""),
    (GuardCategory.COMPARATIVE_CLAIM, "Compare results between plate 1 and plate 2.", ""),
    (GuardCategory.PRICING_CONTRACT, "How much sample volume do I need?", ""),
    (GuardCategory.PRICING_CONTRACT, "What is the shelf life of the reagents?", ""),
    (GuardCategory.PRICING_CONTRACT, "How do I contact technical support?", ""),
    (GuardCategory.ROADMAP, "Which software version am I running?", ""),
    (GuardCategory.ROADMAP, "Will the kit work at 4 C?", "Yes, the kit is stable at 4 C for 12 months."),
    (GuardCategory.ROADMAP, "How do I map the wells to sample IDs?", ""),
]

CLEAN = [
    "What is the recommended storage temperature for the GX-200 kit?",
    "How many reactions are in one kit?",
    "What does error code E42 on the analyzer mean?",
    "Which centrifuge speed should I use for the spin column step?",
    "Can I freeze-thaw the controls more than once?",
    "Where can I download the safety data sheet?",
]


def _id(case):
    return f"{case[0].value}:{(case[1] or case[2])[:40]}"


@pytest.mark.parametrize("case", POSITIVES, ids=_id)
def test_positive_hits_its_category(case):
    category, question, draft = case
    hit = RULES[category].check(req(question, draft))
    assert hit is not None, f"{category.value} rule missed: {question!r} / {draft!r}"
    assert hit.category == category
    assert hit.verdict == Verdict.BLOCKED
    assert hit.reason.strip()


@pytest.mark.parametrize("case", NEGATIVES, ids=_id)
def test_negative_does_not_hit_its_category(case):
    category, question, draft = case
    assert RULES[category].check(req(question, draft)) is None


@pytest.mark.parametrize("question", CLEAN)
def test_clean_question_hits_no_rule(question):
    hits = [r.category.value for r in RULES.values() if r.check(req(question, ""))]
    assert hits == []


def test_every_category_has_enough_cases():
    for category in RULES:
        assert sum(c[0] == category for c in POSITIVES) >= 5, category
        assert sum(c[0] == category for c in NEGATIVES) >= 3, category


def test_pricing_hit_reroutes_to_sales_ops():
    hit = RULES[GuardCategory.PRICING_CONTRACT].check(req("What's the price per kit?"))
    assert hit.reroute_topic == Topic.SALES_OPS
    for category, rule in RULES.items():
        if category != GuardCategory.PRICING_CONTRACT:
            assert rule.reroute_topic is None


@pytest.mark.parametrize(
    "question, secret",
    [
        ("Her SSN is 123-45-6789.", "123-45-6789"),
        ("Sample from patient John Smith.", "John"),
        ("Reach the patient at (310) 555-0142.", "555-0142"),
        ("Email jane.doe@mail.com about her clinical sample.", "jane.doe"),
    ],
)
def test_patient_data_reason_does_not_echo_sensitive_text(question, secret):
    hit = RULES[GuardCategory.PATIENT_DATA].check(req(question))
    assert hit is not None
    assert secret not in hit.reason
    assert "question" in hit.reason


def test_patient_data_scans_draft_and_names_the_field():
    hit = RULES[GuardCategory.PATIENT_DATA].check(req("Is this ok?", "MRN: 99812 is fine."))
    assert hit is not None and "draft" in hit.reason


def test_screen_rules_default_to_patient_data_only():
    assert screen_categories() == {GuardCategory.PATIENT_DATA}
    assert screen_categories({"screen_rules": ["patient_data", "roadmap"]}) == {
        GuardCategory.PATIENT_DATA,
        GuardCategory.ROADMAP,
    }


def test_every_policy_category_has_a_rule():
    assert set(load_policy()["categories"]) == {c.value for c in RULES}
