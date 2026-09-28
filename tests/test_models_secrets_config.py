import logging

import pytest
from pydantic import ValidationError

from osintizada.config import load_settings
from osintizada.core.enums import EntityType, RelationType, SearchMode
from osintizada.core.models import Entity, Relationship
from osintizada.core.secrets import SecretRedactingFilter, mask_secret, sanitize


def test_entity_id_is_deterministic():
    assert Entity(type=EntityType.EMAIL, value="a@b.com").id == Entity(type=EntityType.EMAIL, value="a@b.com").id
    assert Entity(type=EntityType.EMAIL, value="a@b.com").id != Entity(type=EntityType.USERNAME, value="a@b.com").id


def test_relationship_requires_reason_and_evidence():
    with pytest.raises(ValidationError):
        Relationship(source_id="a", target_id="b", type=RelationType.USES, reason="motivo", evidence_ids=[])
    with pytest.raises(ValidationError):
        Relationship(source_id="a", target_id="b", type=RelationType.USES, reason="", evidence_ids=["e1"])
    rel = Relationship(source_id="a", target_id="b", type=RelationType.USES, reason="email no perfil",
                       evidence_ids=["e1"])
    assert rel.confidence == 0.5


def test_mask_secret():
    assert mask_secret("sk-abcdefghijklmn37ad") == "sk-****37ad"
    assert mask_secret(None) == "NOT CONFIGURED"
    assert mask_secret("short") == "****"


def test_sanitize_removes_secrets():
    text = sanitize("GET https://api.x.com/?api_key=SUPERSECRET123 Authorization: Bearer abcdefghijkl")
    assert "SUPERSECRET123" not in text
    assert "abcdefghijkl" not in text


def test_log_filter_redacts():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token=%s", ("abc123456",), None)
    SecretRedactingFilter().filter(record)
    assert "abc123456" not in record.getMessage()


def test_load_settings_from_yaml(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("search:\n  max_depth: 5\nproviders:\n  foo:\n    enabled: false\nmodes:\n  quick:\n    max_queries: 3\n")
    s = load_settings(cfg)
    assert s.search.max_depth == 5
    assert s.provider("foo").enabled is False
    assert s.provider("unknown").enabled is True
    assert s.mode(SearchMode.QUICK).max_queries == 3
    # demais campos do modo preservados pelo deep merge
    assert s.mode(SearchMode.QUICK).max_queries_per_identifier == 8


def test_default_yaml_is_valid():
    s = load_settings()
    assert s.tor.mode == "TOR_DISABLED"
