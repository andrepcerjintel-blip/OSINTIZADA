"""Identidade RINO: nome, logo oficial e compatibilidade com o nome legado OSINTIZADA."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from osintizada import __version__
from osintizada.api import create_app
from osintizada.branding import LOGO_HEADER, LOGO_ICON, PRODUCT_NAME, TAGLINE, env, user_agent
from osintizada.cli import build_parser
from osintizada.config import Settings, load_settings
from tests.fixtures.jobs_env import JobsEnv

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client(tmp_path, monkeypatch):
    for var in ("RINO_API_TOKEN", "OSINTIZADA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    e = JobsEnv(tmp_path)
    return TestClient(create_app(e.service, e.jobs, e.redis, reconcile_on_startup=False))


def test_official_logo_asset_and_derived_sizes():
    official = Image.open(ROOT / "assets" / "logo-rino.png")
    assert official.format == "PNG" and official.size == (1254, 1254)
    assert Image.open(LOGO_HEADER).size == (256, 256) and Image.open(LOGO_ICON).size == (64, 64)


def test_env_prefers_rino_and_accepts_legacy(monkeypatch):
    monkeypatch.delenv("RINO_DNS_SERVERS", raising=False)
    monkeypatch.setenv("OSINTIZADA_DNS_SERVERS", "9.9.9.9")
    assert env("DNS_SERVERS") == "9.9.9.9"  # legado continua funcionando
    monkeypatch.setenv("RINO_DNS_SERVERS", "1.1.1.1")
    assert env("DNS_SERVERS") == "1.1.1.1"  # prefixo oficial tem precedência
    assert env("NAO_EXISTE", "padrao") == "padrao"


def test_config_file_official_and_legacy(tmp_path, monkeypatch):
    monkeypatch.delenv("RINO_CONFIG", raising=False)
    monkeypatch.delenv("OSINTIZADA_CONFIG", raising=False)
    assert (ROOT / "config" / "rino.yaml").is_file()
    legacy = tmp_path / "legado.yaml"
    legacy.write_text("search:\n  max_depth: 4\n")
    monkeypatch.setenv("OSINTIZADA_CONFIG", str(legacy))
    assert load_settings().search.max_depth == 4


def test_user_agent_and_settings_default():
    assert user_agent("0.5.0") == "RINO/0.5 (+investigation research tool)"
    assert Settings().network.user_agent.startswith("RINO/")


def test_cli_identity(capsys):
    parser = build_parser()
    assert parser.prog == "rino" and TAGLINE in parser.description
    with pytest.raises(SystemExit):
        parser.parse_args(["--version"])
    assert capsys.readouterr().out.strip() == f"RINO {__version__}"


def test_openapi_title_description_and_logo(client):
    spec = client.get("/openapi.json").json()
    assert spec["info"]["title"] == "RINO API"
    assert "RINO" in spec["info"]["description"] and spec["info"]["x-logo"]["url"] == "/branding/logo.png"
    docs = client.get("/docs").text
    assert "RINO API" in docs and "/branding/icon.png" in docs
    assert "RINO API" in client.get("/redoc").text


def test_routes_contracts_preserved(client):
    paths = set(client.get("/openapi.json").json()["paths"])
    for p in ("/health", "/metrics", "/cases", "/cases/{case_id}/investigate", "/jobs/{job_id}",
              "/jobs/{job_id}/events", "/jobs/{job_id}/cancel", "/jobs/{job_id}/retry",
              "/cases/{case_id}/exports", "/cases/{case_id}/timeline", "/providers", "/providers/health"):
        assert p in paths, p


def test_home_page_and_logo_routes_without_token(client, monkeypatch):
    monkeypatch.setenv("RINO_API_TOKEN", "segredo-123")
    home = client.get("/")
    assert home.status_code == 200 and "RINO" in home.text and TAGLINE in home.text
    assert 'src="/branding/logo.png"' in home.text
    logo = client.get("/branding/logo.png")
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/png"
    assert logo.content == LOGO_HEADER.read_bytes()
    assert client.get("/branding/icon.png").content == LOGO_ICON.read_bytes()
    assert client.get("/cases").status_code == 401  # dados continuam protegidos


def test_legacy_api_token_variable_still_protects(client, monkeypatch):
    monkeypatch.setenv("OSINTIZADA_API_TOKEN", "legado-456")
    assert client.get("/cases").status_code == 401
    assert client.get("/cases", headers={"Authorization": "Bearer legado-456"}).status_code == 200


def test_health_identifies_product_without_changing_semantics(client):
    body = client.get("/health").json()
    assert body["product"] == PRODUCT_NAME and body["api"]["name"] == "RINO API"
    assert body["api"]["status"] == "ok" and {"database", "redis", "worker", "queue", "jobs"} <= set(body)


def test_exports_carry_rino_identity(tmp_path, monkeypatch):
    from osintizada.exports.service import ExportService

    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    e = JobsEnv(tmp_path)
    e.settings.jobs.export_dir = str(tmp_path / "exports")
    case_id = e.service.create_case("marca", None)
    svc = ExportService(e.db, e.settings)
    bundle = json.loads(ExportService.to_json(svc.bundle(case_id)))
    assert bundle["product"] == "RINO" and bundle["generator"] == f"RINO {__version__}"
    assert bundle["format"] == "rino.case" and "osintizada.case" in bundle["format_aliases"]
    page = ExportService.to_html(svc.bundle(case_id))
    assert "<title>RINO — Relatório" in page and 'class="brand"' in page
    assert 'src="data:image/png;base64,' in page  # logo embutida (relatório autocontido)
    assert "OSINTIZADA" not in page
    info = svc.export_to_file(case_id, "html", "job-1")
    assert info["filename"].startswith("rino-")
