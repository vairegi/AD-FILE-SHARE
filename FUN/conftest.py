import pytest
import rich_cards

@pytest.fixture(autouse=True)
def isolated_navigation_state():
    rich_cards._retired.clear()
    yield
    rich_cards._retired.clear()
