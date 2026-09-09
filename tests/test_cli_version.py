"""CLI version reporting must follow the checked-out package source."""

from mcp_huddle import __version__
from mcp_huddle.__main__ import _version


def test_cli_version_matches_source_package() -> None:
    assert _version() == __version__
