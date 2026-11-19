# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

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
        cfg=SimpleNamespace(platform_settings=lambda name: {"sources": configured, "max_age_hours": 24},
                            platform_name=lambda name: name or "mastodon"),
        profile=lambda: SimpleNamespace(brief=lambda: "profile"),
        store=SimpleNamespace(known_keys=lambda *a, **k: set()),
        settings=SimpleNamespace(profile=SimpleNamespace(private_terms=[])),
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


def test_scan_keeps_private_terms_out_of_the_ranking_prompt(monkeypatch):
    from datetime import timedelta

    from wingman.core import ranker
    from wingman.core.models import Thread, now_utc

    thread = Thread(platform="mastodon", id="1", source="#x", title="t", body="b", url="https://x/1", author="a",
                    score=0, num_comments=0, created_at=now_utc() - timedelta(hours=1))

    class OnePost(FakePlatform):
        def discover(self, sources, since, limit):
            return [thread]

    seen = {}
    monkeypatch.setattr(ranker, "rank", lambda llm, brief, *a, **k: seen.setdefault("brief", brief) and [])
    svc_profile = SimpleNamespace(brief=lambda: "Works at AcmeCorp on payments; AcmeCorp alumni.")
    _fake_services(monkeypatch, OnePost(), ["#x"])
    svc = cli._services()
    svc.profile, svc.llm = (lambda: svc_profile), None
    svc.settings.profile.private_terms = ["AcmeCorp"]
    svc.store.save_ranked = lambda r: None
    result = CliRunner().invoke(cli.app, ["scan", "-p", "mastodon"])
    assert result.exit_code == 0, result.output
    assert "AcmeCorp" not in seen["brief"] and "payments" in seen["brief"]
