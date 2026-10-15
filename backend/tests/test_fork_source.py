"""The deployed fork must point users at its corresponding modified source."""

from app.routers.about import _basis
from app.services import updates


def test_about_links_to_modified_source_without_retargeting_upstream_updates() -> None:
    info = _basis()
    assert info.repo_url == "https://github.com/MStoffberg/nexview"
    assert updates.REPO == "DerKezorm/nexview"
    assert info.license == "AGPL-3.0-or-later"
