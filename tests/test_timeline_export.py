import csv
import io
import json
import zipfile
from datetime import datetime

import pytest

from osintizada.config import Settings
from osintizada.exports.service import ExportService
from osintizada.investigation.service import InvestigationRequest
from osintizada.timeline.service import TimelineService, register_event_rule
from tests.fixtures.environment import build_service


@pytest.fixture
async def case(monkeypatch, tmp_path):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    settings = Settings()
    settings.jobs.export_dir = str(tmp_path / "exports")
    service, db, _ = build_service(settings)
    case_id = service.create_case("timeline", "caso de teste")
    await service.investigate(case_id, InvestigationRequest(inputs=["example.com"], mode="deep", max_depth=1))
    return db, settings, case_id


async def test_timeline_uses_fact_time_not_collection_time(case):
    db, _, case_id = case
    events = TimelineService(db).build(case_id)
    assert events, "timeline vazia"
    times = [e.event_time for e in events]
    assert times == sorted(times)  # ordem cronológica
    assert all(e.time_basis in ("observed_at", "published_at") for e in events)
    archived = [e for e in events if e.event_type == "PAGE_ARCHIVED"]
    assert archived and archived[0].event_time.startswith("2002") and archived[0].temporality == "HISTORICAL_DATA"
    certs = [e for e in events if e.event_type == "CERTIFICATE_ISSUED"]
    assert certs and all(e.provider == "infra.crtsh" for e in certs)
    for e in events:  # data do fato ≠ data de coleta (o arquivo é de 2002; a coleta é agora)
        assert e.collected_at and e.collected_at[:4] >= "2026"
    # Evidências sem data do fato (ex.: DNS) não entram como fato…
    assert not any(e.provider == "infra.dns" for e in events)
    # …só aparecem explicitamente como coleta.
    with_collection = TimelineService(db).build(case_id, include_collection_only=True)
    collected = [e for e in with_collection if e.event_type == "COLLECTED"]
    assert collected and all(e.time_basis == "collected_at" for e in collected)


async def test_timeline_filters(case):
    db, _, case_id = case
    svc = TimelineService(db)
    only_urls = svc.build(case_id, entity_type="URL")
    assert only_urls and all(e.entity_type == "URL" for e in only_urls)
    assert all(e.provider == "infra.crtsh" for e in svc.build(case_id, provider="infra.crtsh"))
    old = svc.build(case_id, date_to="2010-01-01")
    assert old and all(e.event_time < "2010" for e in old)
    assert all(e.event_time >= "2014" for e in svc.build(case_id, date_from=datetime(2014, 1, 1)))


def test_event_rules_are_extensible():
    @register_event_rule
    def _never(ev, ent):
        return None

    assert _never({}, {}) is None


async def test_json_export_is_reconstructible(case):
    db, settings, case_id = case
    bundle = json.loads(ExportService(db, settings).to_json(ExportService(db, settings).bundle(case_id)))
    assert bundle["case"]["id"] == case_id and bundle["seeds"][0]["source_type"] == "USER_INPUT"
    entity_ids = {e["id"] for e in bundle["entities"]}
    evidence_ids = {e["id"] for e in bundle["evidence"]}
    for rel in bundle["relationships"]:  # IDs preservados: grafo reconstruível
        assert rel["source_entity_id"] in entity_ids and rel["target_entity_id"] in entity_ids
        assert rel["evidence_ids"] and set(rel["evidence_ids"]) <= evidence_ids
    assert all(ev["entity_id"] in entity_ids for ev in bundle["evidence"])
    assert bundle["timeline"] and bundle["searches"] and bundle["audit_summary"]["total_events"] > 0
    assert "infra.dns" in bundle["provider_status"] and bundle["limitations"]


async def test_csv_export_is_split_by_type(case):
    db, settings, case_id = case
    data = ExportService(db, settings).to_csv_zip(ExportService(db, settings).bundle(case_id))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert set(zf.namelist()) == {"entities.csv", "evidence.csv", "relationships.csv", "timeline.csv",
                                      "searches.csv", "seeds.csv"}
        rows = list(csv.DictReader(io.StringIO(zf.read("relationships.csv").decode())))
    assert rows and json.loads(rows[0]["evidence_ids"])


async def test_html_report_links_conclusions_to_evidence(case):
    db, settings, case_id = case
    report = ExportService(db, settings).to_html(ExportService(db, settings).bundle(case_id))
    for section in ("Case Summary", "Seeds", "Entities", "Relationships", "Timeline", "Evidence", "Provider Status",
                    "Search History", "Limitations"):
        assert f"<h2>{section}</h2>" in report
    assert 'href="#ev-' in report and 'id="ev-' in report
    assert "<script" not in report.lower()


def test_csv_formula_injection_is_neutralized():
    from osintizada.exports.service import _csv_safe

    assert _csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)" and _csv_safe("normal") == "normal"


async def test_export_to_file(case):
    db, settings, case_id = case
    info = ExportService(db, settings).export_to_file(case_id, "html", "job123")
    assert info["size_bytes"] > 0 and info["path"].endswith("job123.html") and len(info["sha256"]) == 64
