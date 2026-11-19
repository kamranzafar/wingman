from types import SimpleNamespace

from typer.testing import CliRunner

from wingman import cli


class FakePlatform:
    def __init__(self):
        self.sources = None

    def discover(self, sources, since, limit):
        self.sources = sources
        return []


def _fake_services(monkeypatch, plat, configured):
    svc = SimpleNamespace(
        platform=lambda name: plat,
        cfg=SimpleNamespace(platform_settings=lambda name: {"sources": configured, "max_age_hours": 24}),
        profile=lambda: SimpleNamespace(brief=lambda: "profile"),
        store=SimpleNamespace(known_keys=lambda *a, **k: set()),
    )
    monkeypatch.setattr(cli, "_services", lambda: svc)


def test_scan_topics_replace_configured_sources(monkeypatch):
    plat = FakePlatform()
    _fake_services(monkeypatch, plat, ["#java"])
    args = ["scan", "-p", "mastodon", "-t", "#kafka", "-t", " outbox pattern ", "-t", " "]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert plat.sources == ["#kafka", "outbox pattern"]
    assert "#kafka, outbox pattern" in result.output


def test_scan_without_topics_uses_configured_sources(monkeypatch):
    plat = FakePlatform()
    _fake_services(monkeypatch, plat, ["#java", "#spring"])
    result = CliRunner().invoke(cli.app, ["scan", "-p", "mastodon"])
    assert result.exit_code == 0, result.output
    assert plat.sources == ["#java", "#spring"] and "2 sources" in result.output


def test_scan_with_nothing_to_scan_explains(monkeypatch):
    _fake_services(monkeypatch, FakePlatform(), [])
    result = CliRunner().invoke(cli.app, ["scan", "-p", "mastodon"])
    assert result.exit_code != 0 and "--topic" in str(result.exception)
