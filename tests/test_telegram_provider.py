from osintizada.core.enums import EntityType, IdentifierType, ProviderStatus, RelationType
from osintizada.core.normalization import normalize
from osintizada.providers.telegram.telethon_provider import TelegramProvider
from osintizada.resilience import ProviderRuntime


class User:  # nome da classe igual ao tipo do Telethon
    def __init__(self, id, username=None, first_name=None, last_name=None, bot=False):
        self.id, self.username, self.first_name, self.last_name, self.bot = id, username, first_name, last_name, bot


class Channel:
    def __init__(self, id, username, title, megagroup=False):
        self.id, self.username, self.title, self.megagroup = id, username, title, megagroup


class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__("flood")
        self.seconds = seconds


class FakeClient:
    def __init__(self, entities, error=None):
        self.entities, self.error, self.disconnected = entities, error, False

    async def get_entity(self, key):
        if self.error:
            raise self.error
        if key in self.entities:
            return self.entities[key]
        raise ValueError(f'No user has "{key}" as username')

    async def disconnect(self):
        self.disconnected = True


def provider(settings, client):
    rt = ProviderRuntime()
    rt.telegram_client_factory = lambda: client
    return TelegramProvider(settings, rt)


async def test_not_configured_without_credentials(settings, monkeypatch):
    for var in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION"):
        monkeypatch.delenv(var, raising=False)
    p = TelegramProvider(settings, ProviderRuntime())
    resp = await p.search(normalize("@durov", IdentifierType.TELEGRAM_USERNAME))
    assert resp.status == ProviderStatus.NOT_CONFIGURED and resp.results == []
    assert "TELEGRAM_API_ID" in resp.errors[0]


async def test_resolve_username_prioritizes_telegram_id(settings):
    client = FakeClient({"durov": User(1006503122, "durov", "Pavel", "Durov")})
    resp = await provider(settings, client).search(normalize("@Durov", IdentifierType.TELEGRAM_USERNAME))
    assert resp.status == ProviderStatus.SUCCESS and client.disconnected
    [main] = resp.results
    assert main.type == EntityType.TELEGRAM_USER and main.value == "1006503122"
    assert main.relation_to_query == RelationType.USES_USERNAME and main.relation_direction == "reverse"
    assert "mutáveis" in main.relation_reason
    assert main.attributes["telegram_id"] == 1006503122 and main.attributes["display_name"] == "Pavel Durov"
    assert main.observed_at is not None


async def test_unknown_username_is_no_results(settings):
    resp = await provider(settings, FakeClient({})).search(normalize("ninguem_aqui", IdentifierType.TELEGRAM_USERNAME))
    assert resp.status == ProviderStatus.NO_RESULTS


async def test_lookup_by_id_links_current_username(settings):
    client = FakeClient({777: Channel(777, "canal_x", "Canal X")})
    resp = await provider(settings, client).search(normalize("777", IdentifierType.TELEGRAM_ID))
    main = next(r for r in resp.results if r.type == EntityType.TELEGRAM_CHANNEL)
    assert main.value == "777" and main.attributes["display_name"] == "Canal X"
    handle = next(r for r in resp.results if r.type == EntityType.TELEGRAM_USER)
    assert handle.value == "canal_x" and handle.relation_to_query == RelationType.USES_USERNAME


async def test_flood_wait_is_rate_limited(settings):
    settings.resilience.rate_limit_retries = 0
    resp = await provider(settings, FakeClient({}, FloodWaitError(120))).search(
        normalize("durov", IdentifierType.TELEGRAM_USERNAME))
    assert resp.status == ProviderStatus.RATE_LIMITED and resp.metadata["retry_after"] == 120
