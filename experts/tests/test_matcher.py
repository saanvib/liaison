import io
import json

import pytest

import experts
from contracts import Topic
from experts import matcher as matcher_mod
from experts.embed import BagOfWordsEmbedder, HttpEmbedder, cosine, default_embedder
from experts.matcher import Matcher, chunks, classify_topic, load_directory
from experts.print_allowlist import allowlist_line

DIRECTORY = load_directory()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Every test runs with the offline embedder and a fresh default matcher."""
    for var in ("EMBEDDINGS_URL", "EMBEDDINGS_API_KEY", "EMBEDDINGS_MODEL", "EXPERT_MATCH_THRESHOLD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(matcher_mod, "_default", None)


@pytest.fixture
def m():
    return Matcher(DIRECTORY, BagOfWordsEmbedder())


# Held-out questions (not the ones used while writing directory.yaml).
NAMED = [
    ("How can I reduce inhibitor carryover in my eluate?", "rnd-rivera"),
    ("What's the recommended proteinase K incubation temperature?", "rnd-rivera"),
    ("Is it ok to freeze-thaw my plasma samples before extraction?", "rnd-natarajan"),
    ("Where do I find the expiry date and shelf life of the reagents?", "qara-hoffmann"),
    ("We need the CE marking and IVDR status for our tender.", "qara-bell"),
    ("How often does the analyzer need calibration and preventive maintenance?", "eng-alvarez"),
    ("The software update removed my user accounts", "eng-chen"),
    ("Can we negotiate payment terms on our purchase order?", "sales-lee"),
]

# Held-out questions that must reach the right topic, by a named expert or the topic channel.
# The dry-ice one falls back offline (no single summary line shares enough words); real embeddings
# should name Aisha Rahman.
ROUTED = [
    ("Do you ship with dry ice to Canada and handle customs?", Topic.SALES_OPS),
    ("Our auditor wants your ISO 13485 certificate", Topic.QA_RA),
    ("The centrifuge module makes a grinding noise", Topic.ENGINEERING),
    ("Which sample types give the best RNA purity?", Topic.RND),
]


@pytest.mark.parametrize("question, topic", ROUTED)
def test_routes_to_right_topic(m, question, topic):
    r = m.match(question)
    assert r.topic == topic
    assert r.fallback or topic in r.expert.topics


@pytest.mark.parametrize("question, expert_id", NAMED)
def test_known_good_matches(m, question, expert_id):
    r = m.match(question)
    assert not r.fallback, f"fell back (score {r.score:.2f})"
    assert r.expert.id == expert_id
    assert r.score >= m.threshold
    assert r.topic in r.expert.topics


@pytest.mark.parametrize(
    "question",
    ["What's the weather like in Paris?", "Tell me a joke", "Who won the football game last night?"],
)
def test_no_match_gives_fallback(m, question):
    r = m.match(question)
    assert r.fallback and r.expert is None
    assert isinstance(r.topic, Topic)


def test_fallback_keeps_given_topic(m):
    r = m.match("Tell me a joke", topic=Topic.QA_RA)
    assert r.fallback and r.topic == Topic.QA_RA


def test_fallback_infers_topic_from_keywords(m):
    # Shares no line with any expert, but the keyword classifier still finds the right channel.
    r = m.match("Is your invoice address changing?")
    assert r.fallback and r.topic == Topic.SALES_OPS


def test_topic_filter_restricts_candidates(m):
    q = "Analyzer error code E42 keeps coming back"
    assert m.match(q).expert.id == "eng-alvarez"
    r = m.match(q, topic=Topic.SALES_OPS)
    assert r.expert is None or Topic.SALES_OPS in r.expert.topics
    assert r.topic == Topic.SALES_OPS


def test_topic_filter_picks_best_within_topic(m):
    r = m.match("How do I export run results to CSV?", topic=Topic.ENGINEERING)
    assert r.expert.id == "eng-chen" and r.topic == Topic.ENGINEERING


def test_threshold_is_respected():
    strict = Matcher(DIRECTORY, BagOfWordsEmbedder(), threshold=0.99)
    r = strict.match("How do I export run results to our LIMS?")
    assert r.fallback and r.topic == Topic.ENGINEERING  # inferred from keywords


def test_threshold_env_var(monkeypatch):
    monkeypatch.setenv("EXPERT_MATCH_THRESHOLD", "0.99")
    assert experts.match("How do I export run results to our LIMS?").fallback


@pytest.mark.parametrize(
    "question, topic",
    [
        ("analyzer firmware crash", Topic.ENGINEERING),
        ("price and discount for an order", Topic.SALES_OPS),
        ("ISO audit and certificate", Topic.QA_RA),
        ("low DNA yield after elution", Topic.RND),
        ("hello there", None),
    ],
)
def test_classify_topic(question, topic):
    assert classify_topic(question) == topic


def test_chunks_drop_labels():
    rivera = next(e for e in DIRECTORY if e.id == "rnd-rivera")
    cs = chunks(rivera)
    assert cs[0] == rivera.role
    assert not any(c.lower().startswith(("project:", "meeting notes:")) for c in cs)


# --- directory and allowlist ------------------------------------------------

def test_directory_shape():
    assert len(DIRECTORY) == 8
    for topic in Topic:
        assert sum(topic in e.topics for e in DIRECTORY) == 2, topic
    for field in ("id", "slack_user_id"):
        values = [getattr(e, field) for e in DIRECTORY]
        assert len(set(values)) == len(values), field
    for e in DIRECTORY:
        assert e.slack_user_id.startswith("U_") and e.display_name and e.role and e.summary


def test_duplicate_ids_are_rejected(tmp_path):
    path = tmp_path / "dir.yaml"
    path.write_text(
        "experts:\n"
        "  - {id: a, slack_user_id: U_1, display_name: A, role: R, topics: [rd], summary: s}\n"
        "  - {id: b, slack_user_id: U_1, display_name: B, role: R, topics: [rd], summary: s}\n"
    )
    with pytest.raises(ValueError, match="slack_user_id"):
        load_directory(path)


@pytest.mark.parametrize(
    "topic, ids",
    [
        (Topic.RND, {"U_RND_1", "U_RND_2"}),
        (Topic.QA_RA, {"U_QARA_1", "U_QARA_2"}),
        (Topic.ENGINEERING, {"U_ENG_1", "U_ENG_2"}),
        (Topic.SALES_OPS, {"U_SALES_1", "U_SALES_2"}),
    ],
)
def test_allowlist(topic, ids):
    assert experts.allowlist(topic) == ids


def test_print_allowlist_matches_directory():
    line = allowlist_line()
    assert line.startswith("APPROVER_ALLOWLIST=")
    assert line.split("=", 1)[1].split(",") == [e.slack_user_id for e in DIRECTORY]


def test_public_api():
    assert set(experts.__all__) == {"Expert", "MatchResult", "match", "load_directory", "allowlist"}
    r = experts.match("How do I export run results to our LIMS?")
    assert isinstance(r, experts.MatchResult) and r.expert.id == "eng-chen"
    assert all(isinstance(e, experts.Expert) for e in experts.load_directory())


# --- embedders ----------------------------------------------------------------

def test_default_embedder_is_offline_without_url():
    assert isinstance(default_embedder(), BagOfWordsEmbedder)


def test_bag_of_words_cosine():
    a, b, c = BagOfWordsEmbedder().embed(["analyzer error codes", "error code on the analyzer", "price quote"])
    assert cosine(a, b) == pytest.approx(1.0)  # same stemmed words
    assert cosine(a, c) == 0.0


def test_http_embedder_request_shape(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen.update(url=req.full_url, body=json.loads(req.data), auth=req.get_header("Authorization"))
        return io.BytesIO(json.dumps({"data": [{"embedding": [1.0, 0.0]}, {"embedding": [0.0, 1.0]}]}).encode())

    monkeypatch.setattr("experts.embed.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("EMBEDDINGS_URL", "https://embeddings.example/v1/embeddings")
    monkeypatch.setenv("EMBEDDINGS_API_KEY", "k")
    monkeypatch.setenv("EMBEDDINGS_MODEL", "some-model")
    emb = default_embedder()
    assert isinstance(emb, HttpEmbedder)
    assert emb.embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert seen == {"url": "https://embeddings.example/v1/embeddings",
                    "body": {"input": ["a", "b"], "model": "some-model"}, "auth": "Bearer k"}
