import pytest

from osintizada.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Settings padrão, isolados de qualquer YAML local."""
    return Settings()
