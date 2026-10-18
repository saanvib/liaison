from contracts import Topic
from vendor_agent import clients, models, texts
from vendor_agent.config import Config


def test_everything_imports():
    assert clients.LLMClient and clients.OfflineEmbedder and models.VendorOutput and texts.RETRY_TEXT


def test_config_from_env_dict_required_only():
    config = Config.from_env(
        {"ROUTER_ADDRESS": "agent1router", "GUARD_ADDRESS": "agent1guard", "INGEST_ADDRESS": "agent1ingest"}
    )
    assert config.router_address == "agent1router"
    assert config.guard_address == "agent1guard"
    assert config.ingest_address == "agent1ingest"
    assert config.approver_allowlist == frozenset()
    assert config.guard_timeout_s == 10.0
    assert config.kb_match_threshold == 0.85
    assert config.low_confidence == 0.5
    assert config.llm_model == "gemini-2.5-flash"
    assert config.team_roles[Topic.RND] == "our R&D team"


def test_config_from_env_dict_overrides():
    config = Config.from_env(
        {
            "ROUTER_ADDRESS": "r",
            "GUARD_ADDRESS": "g",
            "INGEST_ADDRESS": "i",
            "APPROVER_ALLOWLIST": "U1, U2 ,U3",
            "GUARD_TIMEOUT_S": "5",
            "KB_MATCH_THRESHOLD": "0.9",
            "LOW_CONFIDENCE": "0.3",
            "LLM_API_KEY": "sk-test",
            "LLM_MODEL": "gemini-2.5-pro",
            "EMBEDDINGS_URL": "https://example.test/embed",
            "TEAM_ROLES": '{"sales_ops": "the sales team"}',
        }
    )
    assert config.approver_allowlist == frozenset({"U1", "U2", "U3"})
    assert config.guard_timeout_s == 5.0
    assert config.kb_match_threshold == 0.9
    assert config.low_confidence == 0.3
    assert config.llm_api_key == "sk-test"
    assert config.embeddings_url == "https://example.test/embed"
    assert config.team_roles[Topic.SALES_OPS] == "the sales team"
    assert config.team_roles[Topic.RND] == "our R&D team"
