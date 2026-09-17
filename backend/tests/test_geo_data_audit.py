"""A3: the routing data files — where they live, how they are replaced, and what a rule that
reads the RU dataset renders as.

The point of the milestone is that the lists stop being frozen at image build time, so the tests
that matter are the ones about a replacement going wrong: a corrupted download, a dataset that
no longer carries a category some rule names, and an xray that does not come back.
"""
import os
from hashlib import sha256

import pytest

from pi_gw_panel import geo_data
from pi_gw_panel.models import RoutingRule
from pi_gw_panel.xray_config.routing import (PRESETS, preset_rules, rules_to_xray, validate_rule)
from pi_gw_panel.xray_config.validate import explain_xray_error

GEOIP_RU = b"\x00" * 4096
GEOSITE_RU = b"\x01" * 8192


class _Settings:
    def __init__(self, tmp_path):
        self.data_dir = str(tmp_path)
        self.config_path = str(tmp_path / "xray.json")
        self.xray_bin = "xray"


class _Supervisor:
    def __init__(self, answer=True):
        self.answer = answer
        self.reloads = 0

    def reload_if_running(self):
        self.reloads += 1
        return self.answer

    def status(self):                      # read by tunnel_proxy: no tunnel in these tests
        return {"running": False}


class _State:
    def __init__(self, tmp_path, *, reload_answer=True):
        self.settings = _Settings(tmp_path)
        self.supervisor = _Supervisor(reload_answer)
        self.store = type("S", (), {"get_setting": lambda self, key: None})()
        self.xray_bin = "xray"


def _serve(monkeypatch, files: dict[str, bytes], *, checksums=True):
    """Stand in for the release downloads: name → bytes, with matching .sha256sum files."""
    def fake(url, *, headers=None, proxy=None, timeout=None, max_bytes=None, raw_body=False):
        asset = url.rsplit("/", 1)[-1]
        if asset.endswith(".sha256sum"):
            if not checksums:
                raise OSError("404")
            body = files[asset[: -len(".sha256sum")]]
            return f"{sha256(body).hexdigest()}  {asset[:-10]}\n", []
        return files[asset], []
    monkeypatch.setattr(geo_data, "fetch_url", fake)


def _no_validation(monkeypatch, ok=True, detail=""):
    monkeypatch.setattr(geo_data, "_validate_against", lambda cfg, binary, asset_dir: (ok, detail))


# --- where the files live ---

def test_the_asset_dir_is_seeded_from_the_image_and_pointed_at(tmp_path, monkeypatch):
    """A gateway with no internet must route exactly as before: the image's files are copied in,
    and every xray this process spawns is told where to look."""
    image = tmp_path / "image"
    image.mkdir()
    (image / "geoip.dat").write_bytes(b"stock-ip")
    (image / "geosite.dat").write_bytes(b"stock-site")
    settings = _Settings(tmp_path / "data")

    target = geo_data.ensure_seeded(settings, image_dir=str(image))
    assert (tmp_path / "data" / "geo" / "geoip.dat").read_bytes() == b"stock-ip"

    monkeypatch.delenv(geo_data.ASSET_ENV, raising=False)
    monkeypatch.setattr(geo_data, "IMAGE_ASSET_DIR", str(image))
    assert geo_data.install_env(settings) == target
    assert os.environ[geo_data.ASSET_ENV] == target

    # Seeding never overwrites what is already installed — an update would be undone on restart.
    (tmp_path / "data" / "geo" / "geoip.dat").write_bytes(b"updated")
    geo_data.ensure_seeded(settings, image_dir=str(image))
    assert (tmp_path / "data" / "geo" / "geoip.dat").read_bytes() == b"updated"


# --- replacing a dataset ---

def test_an_update_swaps_both_files_keeps_the_old_ones_and_reloads(tmp_path, monkeypatch):
    state = _State(tmp_path)
    geo = geo_data.ensure_seeded(state.settings, image_dir="/nonexistent")
    for name in ("geoip_ru.dat", "geosite_ru.dat"):
        with open(os.path.join(geo, name), "wb") as stream:
            stream.write(b"old" * 400)
    _serve(monkeypatch, {"geoip.dat": GEOIP_RU, "geosite.dat": GEOSITE_RU})
    _no_validation(monkeypatch)

    result = geo_data.update(state, "ru")

    assert result["ok"] is True and sorted(result["files"]) == ["geoip_ru.dat", "geosite_ru.dat"]
    assert open(os.path.join(geo, "geosite_ru.dat"), "rb").read() == GEOSITE_RU
    assert open(os.path.join(geo, "geosite_ru.dat.prev"), "rb").read() == b"old" * 400
    assert state.supervisor.reloads == 1
    installed = {row["file"]: row for row in geo_data.status(state.settings)}
    assert installed["geosite_ru.dat"]["bytes"] == len(GEOSITE_RU)
    assert installed["geosite_ru.dat"]["has_previous"] is True


def test_a_checksum_mismatch_replaces_nothing(tmp_path, monkeypatch):
    """The download is the one place a hostile or broken mirror reaches the routing table."""
    state = _State(tmp_path)
    geo = geo_data.ensure_seeded(state.settings, image_dir="/nonexistent")
    with open(os.path.join(geo, "geoip_ru.dat"), "wb") as stream:
        stream.write(b"keep me" * 200)

    def fake(url, *, headers=None, proxy=None, timeout=None, max_bytes=None, raw_body=False):
        if url.endswith(".sha256sum"):
            return f"{'0' * 64}  x\n", []
        return GEOIP_RU, []
    monkeypatch.setattr(geo_data, "fetch_url", fake)
    _no_validation(monkeypatch)

    result = geo_data.update(state, "ru")

    assert result["ok"] is False and "checksum mismatch" in result["error"]
    assert open(os.path.join(geo, "geoip_ru.dat"), "rb").read() == b"keep me" * 200
    assert state.supervisor.reloads == 0


def test_an_upstream_without_checksums_still_installs(tmp_path, monkeypatch):
    state = _State(tmp_path)
    geo_data.ensure_seeded(state.settings, image_dir="/nonexistent")
    _serve(monkeypatch, {"geoip.dat": GEOIP_RU, "geosite.dat": GEOSITE_RU}, checksums=False)
    _no_validation(monkeypatch)
    assert geo_data.update(state, "ru")["ok"] is True


def test_data_the_running_config_cannot_load_is_refused_before_the_swap(tmp_path, monkeypatch):
    """The whole reason the update validates first: a dataset that dropped a category some rule
    names would take the tunnel down on the next restart, hours later, blaming nothing."""
    state = _State(tmp_path)
    geo = geo_data.ensure_seeded(state.settings, image_dir="/nonexistent")
    with open(os.path.join(geo, "geosite_ru.dat"), "wb") as stream:
        stream.write(b"old" * 400)
    open(state.settings.config_path, "w").write('{"outbounds": []}')
    _serve(monkeypatch, {"geoip.dat": GEOIP_RU, "geosite.dat": GEOSITE_RU})
    _no_validation(monkeypatch, ok=False,
                   detail="... > code not found in geosite_ru.dat: RU-BLOCKED")

    result = geo_data.update(state, "ru")

    assert result["ok"] is False
    assert "the category 'ru-blocked' is not in geosite_ru.dat" in result["error"]
    assert open(os.path.join(geo, "geosite_ru.dat"), "rb").read() == b"old" * 400
    assert state.supervisor.reloads == 0


def test_an_xray_that_does_not_come_back_gets_its_old_data_returned(tmp_path, monkeypatch):
    state = _State(tmp_path, reload_answer=False)
    geo = geo_data.ensure_seeded(state.settings, image_dir="/nonexistent")
    for name in ("geoip_ru.dat", "geosite_ru.dat"):
        with open(os.path.join(geo, name), "wb") as stream:
            stream.write(b"old" * 400)
    _serve(monkeypatch, {"geoip.dat": GEOIP_RU, "geosite.dat": GEOSITE_RU})
    _no_validation(monkeypatch)

    result = geo_data.update(state, "ru")

    assert result["ok"] is False and "previous files were restored" in result["error"]
    assert open(os.path.join(geo, "geosite_ru.dat"), "rb").read() == b"old" * 400
    assert state.supervisor.reloads == 2        # the failed one, then the one after the restore


def test_revert_needs_a_previous_copy(tmp_path, monkeypatch):
    state = _State(tmp_path)
    geo = geo_data.ensure_seeded(state.settings, image_dir="/nonexistent")
    assert geo_data.revert(state, "ru")["ok"] is False

    _serve(monkeypatch, {"geoip.dat": GEOIP_RU, "geosite.dat": GEOSITE_RU})
    _no_validation(monkeypatch)
    with open(os.path.join(geo, "geoip_ru.dat"), "wb") as stream:
        stream.write(b"first" * 300)
    geo_data.update(state, "ru")

    assert geo_data.revert(state, "ru")["ok"] is True
    assert open(os.path.join(geo, "geoip_ru.dat"), "rb").read() == b"first" * 300


def test_an_unknown_dataset_is_refused(tmp_path):
    with pytest.raises(ValueError):
        geo_data.update(_State(tmp_path), "elbonia")


# --- what a RU rule renders as (verified against the pinned xray in the milestone gates) ---

def test_a_ru_rule_renders_as_an_ext_reference_and_a_stock_one_does_not():
    rules = [
        RoutingRule(id=None, position=0, type="geosite", value="ru-blocked", action="proxy",
                    dataset="ru"),
        RoutingRule(id=None, position=1, type="geoip", value="ru-blocked", action="proxy",
                    dataset="ru"),
        RoutingRule(id=None, position=2, type="geosite", value="category-ads-all", action="block"),
    ]
    fields = rules_to_xray(rules, "direct")
    assert fields[1]["domain"] == ["ext:geosite_ru.dat:ru-blocked"]
    assert fields[2]["ip"] == ["ext:geoip_ru.dat:ru-blocked"]
    assert fields[3]["domain"] == ["geosite:category-ads-all"]     # stock keeps its short form
    assert fields[0]["ip"] == ["geoip:private"] and fields[-1]["outboundTag"] == "direct"


def test_a_dataset_is_only_meaningful_on_a_geo_rule():
    literal = RoutingRule(id=None, position=0, type="domain", value="x.example", action="proxy",
                          dataset="ru")
    assert "no geo dataset" in validate_rule(literal)
    assert "unknown geo dataset" in validate_rule(
        RoutingRule(id=None, position=0, type="geosite", value="v", action="proxy", dataset="nope"))
    assert validate_rule(
        RoutingRule(id=None, position=0, type="geosite", value="v", action="proxy",
                    dataset="ru")) is None


def test_the_ru_preset_inverts_the_policy_and_carries_its_dataset():
    spec = PRESETS["ru-blocked-only"]
    assert spec["default_action"] == "direct"          # everything direct …
    rules = preset_rules("ru-blocked-only")
    assert [r.action for r in rules] == ["proxy", "proxy"]   # … except what is blocked in RU
    assert {r.dataset for r in rules} == {"ru"}
    # The older presets stay on the stock data, so nothing about them changes.
    assert {r.dataset for r in preset_rules("ru-direct")} == {""}


def test_the_missing_category_error_says_what_to_do():
    text = ("Failed to start: main: ... > failed to load external sites: ru-blocked from "
            "geosite_ru.dat > code not found in geosite_ru.dat: RU-BLOCKED")
    message = explain_xray_error(text)
    assert "'ru-blocked'" in message and "geosite_ru.dat" in message
    assert "Update that geo data" in message
    assert explain_xray_error("") == ""
