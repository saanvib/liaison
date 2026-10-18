import io
import json
from collections import Counter

import pytest

from experts import ingest_github as ig
from experts import matcher as matcher_mod
from experts.embed import BagOfWordsEmbedder
from experts.matcher import Matcher, load_directory, load_evidence
from contracts import Topic
from experts.tests.test_matcher import NAMED, ROUTED

DIRECTORY = load_directory()
EXPERT_IDS = {e.id for e in DIRECTORY}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    for var in ("EMBEDDINGS_URL", "EMBEDDINGS_API_KEY", "EMBEDDINGS_MODEL", "EXPERT_MATCH_THRESHOLD", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(matcher_mod, "_default", None)
    monkeypatch.setattr(matcher_mod, "EVIDENCE_PATH", tmp_path / "evidence.json")


@pytest.fixture
def sources():
    return ig.load_sources(expert_ids=EXPERT_IDS)


@pytest.fixture
def evidence(sources):
    return ig.build_evidence(ig.FixtureGitHub(), sources)


def texts(evidence, expert_id):
    return [e.text for e in evidence if e.expert_id == expert_id]


# --- sources ------------------------------------------------------------------

def test_sources_map_to_directory(sources):
    assert sources.repos and set(sources.authors.values()) <= EXPERT_IDS


def test_unknown_expert_id_is_rejected(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text("github:\n  repos: [a/b]\n  authors: {someone: no-such-expert}\n")
    with pytest.raises(ValueError, match="no-such-expert"):
        ig.load_sources(path, EXPERT_IDS)


# --- building evidence ----------------------------------------------------------

def test_attributes_commits_and_prs(evidence):
    assert "Retry LIMS upload when the hospital network drops the connection" in texts(evidence, "eng-chen")
    assert "Add door interlock check before the centrifuge spins up" in texts(evidence, "eng-alvarez")
    pr = next(e for e in evidence if e.source == "github_pr")
    assert pr.url.startswith("https://github.com/") and pr.date


def test_pr_text_is_title_plus_first_paragraph(evidence):
    audit = next(t for t in texts(evidence, "eng-chen") if t.startswith("Keep audit trail"))
    assert "21 CFR Part 11" in audit and "migration" not in audit


def test_commit_email_maps_when_no_linked_account(evidence):
    # The heater commit has author=null and a differently cased email.
    assert any(t.startswith("Reduce heater block overshoot") for t in texts(evidence, "eng-alvarez"))


@pytest.mark.parametrize("dropped", ["fix typo", "Merge pull request", "Bump urllib3", "dark mode"])
def test_noise_is_dropped(evidence, dropped):
    # Too short, merge commit, bot, unmapped author.
    assert not any(dropped.lower() in e.text.lower() for e in evidence)


def test_labels_refs_and_product_names_are_stripped(evidence):
    assert "Add template for supplier qualification audits" in texts(evidence, "qara-hoffmann")
    hl7 = [t for t in texts(evidence, "eng-chen") if "HL7" in t]
    assert hl7 == ["Add HL7 result messages for the LIMS bridge"]  # squash duplicate kept once, no GX-200 or #57


@pytest.mark.parametrize(
    "body, para",
    [
        ("## Summary\nTwo **follow-ups** from `run_eval`.\n\n- details", "Two follow-ups from run_eval."),
        ("Plain first line.\n\nSecond.", "Plain first line."),
        ("Set _asyncio_mode_ to auto.", "Set asyncio_mode to auto."),
        (None, ""),
    ],
)
def test_first_paragraph_strips_markdown(body, para):
    assert ig.first_paragraph(body) == para


@pytest.mark.parametrize(
    "raw, cleaned",
    [
        ("fix(export): keep sample IDs (#42)", "keep sample IDs"),
        ("[P2] Add flat build", "Add flat build"),
        ("Update GX analyzer docs", "Update analyzer docs"),
        ("Note the GX-2000 spec", "Note the GX-2000 spec"),  # whole terms only
    ],
)
def test_clean(raw, cleaned):
    assert ig.clean(raw, ["GX-200", "GX"]) == cleaned


def test_newest_first_and_capped(sources):
    sources.max_per_expert = 1
    ev = ig.build_evidence(ig.FixtureGitHub(), sources)
    assert texts(ev, "eng-chen") == [
        "Keep audit trail entries when a user account is deactivated. Deactivating an operator deleted "
        "their audit trail rows, which breaks 21 CFR Part 11 review"
    ]
    assert all(n == 1 for n in Counter(e.expert_id for e in ev).values())


def test_since_filters_old_activity(sources):
    ev = ig.build_evidence(ig.FixtureGitHub(), sources, since="2026-06-01")
    assert not any("saliva" in e.text for e in ev)
    assert any("late Ct values" in e.text for e in ev)


# --- GitHub client ----------------------------------------------------------------

def test_client_request_shape_and_pagination(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout):
        seen.append((req.full_url, req.get_header("Authorization"), req.get_header("Accept")))
        n = 100 if len(seen) == 1 else 3
        return io.BytesIO(json.dumps([{"i": i} for i in range(n)]).encode())

    monkeypatch.setattr("experts.ingest_github.urllib.request.urlopen", fake_urlopen)
    items = ig.GitHubClient(token="t").commits("org/repo", since="2026-01-01")
    assert len(items) == 103
    assert seen[0] == ("https://api.github.com/repos/org/repo/commits?since=2026-01-01&per_page=100&page=1",
                       "Bearer t", "application/vnd.github+json")
    assert seen[1][0].endswith("page=2") and len(seen) == 2


def test_client_stops_at_max_pages(monkeypatch):
    calls = []
    monkeypatch.setattr("experts.ingest_github.urllib.request.urlopen",
                        lambda req, timeout: calls.append(1) or io.BytesIO(json.dumps([{}] * 100).encode()))
    assert len(ig.GitHubClient(max_pages=2).pulls("org/repo")) == 200 and len(calls) == 2


# --- matcher with evidence ----------------------------------------------------------

@pytest.fixture
def with_evidence(evidence, tmp_path):
    path = tmp_path / "ev.json"
    ig.write_evidence(evidence, path)
    return Matcher(DIRECTORY, BagOfWordsEmbedder(), evidence=load_evidence(path))


@pytest.mark.parametrize(
    "question, expert_id",
    [
        ("Our LIMS upload fails whenever the network connection drops", "eng-chen"),
        ("Does the centrifuge have a door interlock?", "eng-alvarez"),
        ("Do you have a template for supplier qualification audits?", "qara-hoffmann"),
    ],
)
def test_evidence_names_experts_profiles_miss(with_evidence, question, expert_id):
    without = Matcher(DIRECTORY, BagOfWordsEmbedder()).match(question)
    assert without.fallback or without.expert.id != expert_id
    r = with_evidence.match(question)
    assert not r.fallback and r.expert.id == expert_id


@pytest.mark.parametrize("question, expert_id", NAMED)
def test_evidence_keeps_profile_matches(with_evidence, question, expert_id):
    assert with_evidence.match(question).expert.id == expert_id


@pytest.mark.parametrize("question, topic", ROUTED)
def test_evidence_keeps_topic_routing(with_evidence, question, topic):
    r = with_evidence.match(question)
    assert r.topic == topic and (r.fallback or topic in r.expert.topics)


@pytest.mark.parametrize(
    "question",
    ["What's the weather like in Paris?", "Tell me a joke", "Who won the football game last night?",
     "Can you add a dark mode to the website?"],  # last one: only an unmapped author's commit mentions it
)
def test_evidence_does_not_create_false_matches(with_evidence, question):
    assert with_evidence.match(question).fallback


def test_write_evidence_leaves_no_temp_file(evidence, tmp_path):
    ig.write_evidence(evidence, tmp_path / "ev.json")
    assert [p.name for p in tmp_path.iterdir()] == ["ev.json"]


def test_evidence_weight_scales_score():
    rivera = next(e for e in DIRECTORY if e.id == "rnd-rivera")
    q = "zebrafish embryo imaging pipeline"
    m = Matcher([rivera], BagOfWordsEmbedder(), threshold=0.0, evidence={"rnd-rivera": [q]}, evidence_weight=0.5)
    assert m.match(q).score == pytest.approx(0.5)


def test_load_evidence_missing_file(tmp_path):
    assert load_evidence(tmp_path / "absent.json") == {}


def test_main_writes_evidence_and_default_matcher_uses_it(capsys):
    assert ig.main(["--fixture", "--out", str(matcher_mod.EVIDENCE_PATH)]) == 0
    assert "wrote 8 chunks" in capsys.readouterr().err
    r = matcher_mod.default_matcher().match("Does the centrifuge have a door interlock?")
    assert r.expert.id == "eng-alvarez"
