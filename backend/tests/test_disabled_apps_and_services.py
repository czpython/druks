from importlib.metadata import EntryPoint, EntryPoints, entry_points

import pytest
from conftest import bind_ambient_session
from druks.apps import loader
from druks.apps.exceptions import AppNotFound
from druks.services import Service
from druks.services.exceptions import ServiceDisabledError
from druks.settings import Settings
from druks.testing import asgi_client, configure_app_for_test
from fastapi import FastAPI
from pydantic import BaseModel


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    path = tmp_path / "druks.toml"
    path.write_text("")
    monkeypatch.setenv("DRUKS_CONFIG", str(path))
    return path


async def test_a_disabled_app_is_not_installed(configuration, druks_db, tmp_path):
    configuration.write_text("[apps.software_factory]\nenabled = false\n")
    api = configure_app_for_test(settings=Settings(data_dir=tmp_path))
    async with asgi_client(api) as client:
        names = [app["name"] for app in (await client.get("/api/apps")).json()]
    assert "core" in names
    assert "software_factory" not in names

    # Routes mount at boot, so a fresh API shows what this configuration serves.
    api = FastAPI()
    loader.load(api)
    async with asgi_client(api) as client:
        assert (await client.get("/api/software_factory/pages")).status_code == 404


def test_a_disabled_app_never_loads(configuration, monkeypatch):
    configuration.write_text("[apps.broken]\nenabled = false\n")
    installed = entry_points(group="druks.apps")
    broken = EntryPoint("broken", "broken_app:App", "druks.apps")
    monkeypatch.setattr(loader, "entry_points", lambda group: EntryPoints([*installed, broken]))

    assert "broken" not in {app.name for app in loader.iter_apps()}


def test_an_app_that_no_package_provides_refuses_to_start(configuration):
    configuration.write_text("[apps.unknown]\nenabled = false\n")

    with pytest.raises(AppNotFound, match="apps.unknown"):
        loader.iter_apps()


async def test_a_disabled_service_is_not_installed(
    declared_services, configuration, druks_db, tmp_path
):
    configuration.write_text("[services.acme]\nenabled = false\n")

    class Acme(Service):
        class Settings(BaseModel):
            url: str

    bind_ambient_session(druks_db)
    await Service.sync_configuration(druks_db)
    api = configure_app_for_test(settings=Settings(data_dir=tmp_path))
    async with asgi_client(api) as client:
        slugs = [card["slug"] for card in (await client.get("/api/services")).json()]
        response = await client.post("/api/services/acme", json={"url": "https://acme.test"})
    assert "linear" in slugs
    assert "acme" not in slugs
    assert response.status_code == 404
    with pytest.raises(ServiceDisabledError, match="services.acme"):
        await Acme.get()


@pytest.mark.parametrize(
    "body, key",
    [
        ('[apps.software_factory]\nenabled = false\nicon = "wrench"', "apps.software_factory"),
        ('[apps.software_factory]\nenabled = "no"', "apps.software_factory.enabled"),
        ("[services.jira]\nenabled = 1", "services.jira.enabled"),
        ('[services.jira]\nenabled = false\nurl = "https://jira.test"', "services.jira"),
    ],
)
def test_a_malformed_table_refuses_to_start(configuration, body, key):
    configuration.write_text(body)

    with pytest.raises(ValueError, match=key):
        Settings()
