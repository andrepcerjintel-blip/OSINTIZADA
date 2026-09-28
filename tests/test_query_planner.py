import pytest

from osintizada.config import Settings
from osintizada.core.enums import IdentifierType as T
from osintizada.core.enums import QueryCategory, SearchMode
from osintizada.core.normalization import normalize
from osintizada.core.query_planner import QueryPlanner


@pytest.fixture
def planner(settings: Settings) -> QueryPlanner:
    return QueryPlanner(settings)


def queries(planner, raw, type_, mode=SearchMode.DEEP_SWEEP, **kw):
    return [q.query for q in planner.plan(normalize(raw, type_), mode=mode, **kw)]


def test_username_core_queries(planner):
    qs = queries(planner, "fearless1999", T.USERNAME)
    for expected in [
        '"fearless1999"',
        '"@fearless1999"',
        '"fearless1999" email',
        '"fearless1999" telegram',
        '"fearless1999" discord',
        'site:t.me "fearless1999"',
        'site:github.com "fearless1999"',
        'site:reddit.com "fearless1999"',
        'site:x.com "fearless1999"',
    ]:
        assert expected in qs


def test_every_query_has_reason_priority_and_operators(planner):
    for q in planner.plan(normalize("fearless1999", T.USERNAME), mode=SearchMode.DEEP_SWEEP):
        assert q.reason
        assert 0 <= q.priority <= 100
        if q.query.startswith("site:"):
            assert "site" in q.operators


def test_sorted_by_priority_and_deduplicated(planner):
    planned = planner.plan(normalize("fearless1999", T.USERNAME), mode=SearchMode.DEEP_SWEEP)
    prios = [q.priority for q in planned]
    assert prios == sorted(prios, reverse=True)
    keys = [q.dedup_key for q in planned]
    assert len(keys) == len(set(keys))


def test_limit_respected(planner):
    assert len(planner.plan(normalize("fearless1999", T.USERNAME), max_queries=5)) == 5


def test_quick_mode_restricts_categories(planner):
    planned = planner.plan(normalize("fearless1999", T.USERNAME), mode=SearchMode.QUICK)
    allowed = {QueryCategory.EXACT, QueryCategory.SOCIAL, QueryCategory.TELEGRAM, QueryCategory.CODE}
    assert planned and all(q.category in allowed for q in planned)


def test_cpf_generates_document_queries_with_formatted_value(planner):
    qs = queries(planner, "52998224725", T.CPF)
    assert '"529.982.247-25" filetype:pdf' in qs
    assert '"529.982.247-25" "diário oficial"' in qs
    assert '"52998224725"' in qs  # variante só-dígitos


def test_phone_variants_become_exact_queries(planner):
    qs = queries(planner, "(61) 99999-9999", T.PHONE)
    assert '"+5561999999999"' in qs
    assert '"61 99999-9999"' in qs


def test_domain_queries(planner):
    qs = queries(planner, "example.com", T.DOMAIN)
    assert "site:example.com" in qs
    assert '"example.com" -site:example.com' in qs


def test_depth_reduces_priority(planner):
    ident = normalize("fearless1999", T.USERNAME)
    p0 = planner.plan(ident, depth=0)[0].priority
    p2 = planner.plan(ident, depth=2)[0].priority
    assert p2 < p0


def test_excluded_domains(planner):
    qs = queries(planner, "fearless1999", T.USERNAME, excluded_domains=["spam.com"])
    assert all(q.endswith("-site:spam.com") for q in qs)


def test_plan_many_global_limit(settings):
    settings.modes[SearchMode.QUICK].max_queries = 4
    planner = QueryPlanner(settings)
    ids = [normalize("fearless1999", T.USERNAME), normalize("a@b.com", T.EMAIL)]
    assert len(planner.plan_many(ids, mode=SearchMode.QUICK)) == 4


def test_raw_search_is_logged_as_raw():
    q = QueryPlanner.raw('"usuario123" "proton.me"')
    assert q.category == QueryCategory.RAW
    assert "quote" in q.operators
    with pytest.raises(ValueError):
        QueryPlanner.raw("   ")
