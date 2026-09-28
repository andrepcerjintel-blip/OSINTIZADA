import json

from osintizada.cli import main


def test_detect_json(capsys):
    assert main(["detect", "darkwolf88", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["detection"]["candidates"][0]["type"] == "username"


def test_plan_json(capsys):
    assert main(["plan", "fearless1999", "--mode", "quick", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data and all("reason" in q for q in data)


def test_plan_forced_type(capsys):
    assert main(["plan", "52998224725", "--type", "cpf", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert all(q["identifier_type"] == "cpf" for q in data)


def test_run_and_providers(capsys, monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    assert main(["run", "a@example.org", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    by = {e["provider"]: e["status"] for e in data["search_log"]}
    assert by["local.identifier_analysis"] == "SUCCESS"
    assert by["search.brave"] == "NOT_CONFIGURED"  # sem chave: nunca simula resultado
    assert main(["providers", "--json"]) == 0
    assert "local.identifier_analysis" in capsys.readouterr().out


def test_invalid_forced_type_value_returns_error(capsys):
    assert main(["plan", "not-an-ip", "--type", "ipv4"]) == 2
