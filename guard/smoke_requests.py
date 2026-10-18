"""One GuardRequest per mode, shared by the local smoke run and the hosted smoke client."""
from contracts import Citation, GuardRequest, Source

REQUESTS = [
    GuardRequest(request_id="smoke-screen", source=Source.VENDOR_AGENT,
                 question="Patient DOB 03/14/1962, why did her run fail?", draft_answer=""),
    GuardRequest(request_id="smoke-draft", source=Source.VENDOR_AGENT,
                 question="How should I store the kit?", draft_answer="Store the GX-200 kit at -20 C.",
                 citations=[Citation(doc_id="ds", doc_title="GX-200 datasheet", snippet="Store the GX-200 kit at -20 C.")]),
    GuardRequest(request_id="smoke-egress", source=Source.EXPERT,
                 question="Any deals?", draft_answer="I can give you a 15% discount this quarter."),
]
