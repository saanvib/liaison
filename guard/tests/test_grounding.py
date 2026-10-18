import json

from contracts import Citation, GuardRequest, Source
from guard.grounding import Grounding, content_tokens, split_sentences

SNIPPETS = [
    Citation(doc_id="ifu", doc_title="GX-200 IFU", snippet="Incubate the lysate at 56 C for 10 minutes before loading the spin column."),
    Citation(doc_id="ds", doc_title="GX-200 datasheet", snippet="Validated sample types: plasma and serum. Store kit at -20 C."),
]


def req(draft, citations=SNIPPETS):
    return GuardRequest(request_id="r", source=Source.VENDOR_AGENT, question="q", draft_answer=draft, citations=citations)


class FakeLLM:
    def __init__(self, reply=None, error=None):
        self.reply, self.error = reply, error

    def complete(self, system, prompt, schema=None):
        if self.error:
            raise self.error
        return self.reply


def test_supported_draft_is_grounded():
    r = Grounding().check(req("Incubate the lysate at 56 C for 10 minutes. Plasma and serum are validated sample types."))
    assert r.grounded


def test_unsupported_sentence_is_named():
    r = Grounding().check(req("Incubate the lysate at 56 C for 10 minutes. Saliva works fine and gives identical yields."))
    assert not r.grounded
    assert r.unsupported_sentence.startswith("Saliva works fine")
    assert "Saliva works fine" in r.reason


def test_wrong_number_counts_against_sentence():
    r = Grounding().check(req("Incubate the lysate at 72 C for 45 minutes overnight."))
    assert not r.grounded


def test_wrong_number_in_short_sentence_is_unsupported():
    r = Grounding().check(req("Store kit at 4 C."))
    assert not r.grounded and r.unsupported_sentence == "Store kit at 4 C."


def test_short_sentences_are_skipped():
    assert Grounding().check(req("Yes. See the IFU. Store kit at -20 C.")).grounded


def test_no_citations_means_ungrounded():
    assert not Grounding().check(req("Store the kit at -20 C for best stability.", citations=[])).grounded


def test_long_unsupported_sentence_is_truncated_in_reason():
    long = "Unsupported claim " + "about novel workflows " * 20 + "."
    r = Grounding().check(req(long))
    assert not r.grounded and len(r.reason) < 200


def test_tokenizer_and_splitter():
    assert "the" not in content_tokens("the kit")
    assert "56" in content_tokens("at 56 C")
    assert split_sentences("One. Two!\nThree?") == ["One.", "Two!", "Three?"]


def test_llm_verdict_used_when_valid():
    llm = FakeLLM(json.dumps({"supported": False, "unsupported_sentence": "It works on saliva."}))
    r = Grounding(llm).check(req("Incubate the lysate at 56 C for 10 minutes."))
    assert not r.grounded and r.unsupported_sentence == "It works on saliva."


def test_llm_failure_falls_back_to_overlap_check():
    bad_draft = "Saliva works fine and gives identical yields every time."
    for llm in (FakeLLM(error=TimeoutError()), FakeLLM("not json"), FakeLLM(json.dumps({"supported": "yes"}))):
        assert not Grounding(llm).check(req(bad_draft)).grounded
