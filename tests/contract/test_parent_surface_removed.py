from typing import Any

import pytest
from pydantic import ValidationError

from nalar.bootstrap.app import create_app
from nalar.bootstrap.settings import Settings
from nalar.presentation.api.schemas.administration import PersonCreateIn
from nalar.presentation.worker.consumers.default import default_handlers
from nalar.presentation.worker.tick import cron_handlers


def test_parent_paths_fields_and_jobs_are_absent() -> None:
    app = create_app(Settings(_env_file=None))
    schema = app.openapi()
    assert not any(
        "/parent/" in path
        or "/children/" in path
        or path.endswith(("/release", "/release-preview"))
        for path in schema["paths"]
    )
    properties = schema["components"]["schemas"]
    for name in ("MeOut", "PublicationOut", "PublicationDetailOut", "PersonOut", "PublicConfigOut"):
        if name in properties:
            assert not any(
                "parent" in field or "digest" in field or field == "linked_children"
                for field in properties[name]["properties"]
            )
    container: Any = None
    assert "digest_delivery" not in default_handlers(container)
    assert not {"weekly_digest", "release_reminders"} & cron_handlers(container).keys()


def test_parent_account_creation_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PersonCreateIn(full_name="Former parent", role="parent", email="parent@example.test")
