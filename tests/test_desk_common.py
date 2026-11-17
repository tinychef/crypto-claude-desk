"""Tests for the shared desk_common package (V2 infrastructure)."""
import json
import os
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import requests

import desk_common
from desk_common import exchanges as ex_mod
from desk_common import http as http_mod


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("DESK_HTTP_CACHE", "1")
    monkeypatch.setattr(http_mod.time, "sleep", lambda *_: None)
    yield


def _resp(status=200, text="{}", headers=None):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.headers = headers or {}
    return r


# ---------------------------------------------------------------- serialize
class TestSerialize:
    def test_numpy_types(self):
        out = json.loads(desk_common.tool_serializer({"b": np.bool_(True), "i": np.int64(3),
                                                      "f": np.float64(1.5), "a": np.array([1, 2])}))
        assert out == {"b": True, "i": 3, "f": 1.5, "a": [1, 2]}

    def test_nan_and_inf_become_null(self):
        out = json.loads(desk_common.tool_serializer({"x": float("nan"), "y": [float("inf"), 1.0]}))
        assert out == {"x": None, "y": [None, 1.0]}

    def test_unknown_object_stringified(self):
        class Foo:
            def __str__(self):
                return "foo"
        assert json.loads(desk_common.tool_serializer({"o": Foo()})) == {"o": "foo"}


# ---------------------------------------------------------------- symbols
class TestSymbols:
    @pytest.mark.parametrize("raw", ["btc", "BTC/USDT", "BTC-USDT", "BTC/USDT:USDT", "BTCUSDT", " btc "])
    def test_base_symbol(self, raw):
        assert desk_common.base_symbol(raw) == "BTC"

    def test_pairs(self):
        assert desk_common.spot_pair("eth") == "ETH/USDT"
        assert desk_common.perp_pair("SOL/USDT") == "SOL/USDT:USDT"

    @pytest.mark.parametrize("bad", ["", None, "BTC$", "../etc"])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            desk_common.base_symbol(bad)


# ---------------------------------------------------------------- keys
class TestKeys:
    def test_get_key_prefers_desk_prefix(self, monkeypatch):
        monkeypatch.setenv("DESK_COINALYZE_KEY", "abc")
        monkeypatch.setenv("COINALYZE_API_KEY", "zzz")
        assert desk_common.get_key("coinalyze") == "abc"

    def test_get_key_missing(self, monkeypatch):
        monkeypatch.delenv("DESK_NOPE_KEY", raising=False)
        assert desk_common.get_key("nope") is None

    def test_unavailable_shape(self):
        u = desk_common.unavailable("glassnode", reason="missing API key", cost="~$800/mo")
        assert u["status"] == "unavailable" and u["provider"] == "glassnode"
        assert "DESK_GLASSNODE_KEY" in u["hint"] and u["cost"] == "~$800/mo"


# ---------------------------------------------------------------- http
class TestHttp:
    def test_success_and_cache(self):
        session = MagicMock()
        session.get.return_value = _resp(200, '{"a": 1}')
        with patch.object(http_mod, "_get_session", return_value=session):
            assert desk_common.http_get_json("https://x.test/a", ttl=60) == {"a": 1}
            assert desk_common.http_get_json("https://x.test/a", ttl=60) == {"a": 1}
        assert session.get.call_count == 1  # second call served from cache

    def test_ttl_zero_bypasses_cache(self):
        session = MagicMock()
        session.get.return_value = _resp(200, '{"a": 1}')
        with patch.object(http_mod, "_get_session", return_value=session):
            desk_common.http_get_json("https://x.test/b", ttl=0)
            desk_common.http_get_json("https://x.test/b", ttl=0)
        assert session.get.call_count == 2

    def test_retries_on_429_then_succeeds(self):
        session = MagicMock()
        session.get.side_effect = [_resp(429, "", {"Retry-After": "1"}), _resp(200, '{"ok": true}')]
        with patch.object(http_mod, "_get_session", return_value=session):
            assert desk_common.http_get_json("https://x.test/c", ttl=0, retries=2) == {"ok": True}

    def test_no_retry_on_451(self):
        session = MagicMock()
        session.get.return_value = _resp(451, "blocked")
        with patch.object(http_mod, "_get_session", return_value=session):
            with pytest.raises(desk_common.HttpError) as ei:
                desk_common.http_get("https://x.test/d", ttl=0, retries=3)
        assert ei.value.status == 451 and session.get.call_count == 1

    def test_network_error_raises_after_retries(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("boom")
        with patch.object(http_mod, "_get_session", return_value=session):
            with pytest.raises(desk_common.HttpError):
                desk_common.http_get("https://x.test/e", ttl=0, retries=2)
        assert session.get.call_count == 3

    def test_invalid_json(self):
        session = MagicMock()
        session.get.return_value = _resp(200, "<html>")
        with patch.object(http_mod, "_get_session", return_value=session):
            with pytest.raises(desk_common.HttpError):
                desk_common.http_get_json("https://x.test/f", ttl=0)

    def test_cache_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("DESK_HTTP_CACHE", "0")
        session = MagicMock()
        session.get.return_value = _resp(200, '{"a": 1}')
        with patch.object(http_mod, "_get_session", return_value=session):
            desk_common.http_get("https://x.test/g", ttl=60)
            desk_common.http_get("https://x.test/g", ttl=60)
        assert session.get.call_count == 2


# ---------------------------------------------------------------- exchanges
class TestExchanges:
    def setup_method(self):
        ex_mod._instances.clear()
        ex_mod._blocked.clear()

    def test_default_priority_okx_first(self, monkeypatch):
        monkeypatch.delenv("DESK_EXCHANGES", raising=False)
        assert ex_mod.exchange_priority()[0] == "okx"

    def test_env_priority(self, monkeypatch):
        monkeypatch.setenv("DESK_EXCHANGES", "kraken, okx ,notanexchange")
        assert ex_mod.exchange_priority() == ["kraken", "okx"]

    def test_get_exchange_sets_proxy_and_caches(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
        a = ex_mod.get_exchange("okx")
        b = ex_mod.get_exchange("okx")
        assert a is b
        assert a.httpsProxy == "http://127.0.0.1:9999"

    def test_get_exchange_unknown(self):
        with pytest.raises(ValueError):
            ex_mod.get_exchange("not_a_real_exchange")

    def test_fallback_skips_geo_blocked(self):
        calls = []

        def fake_get(name, market="spot"):
            m = MagicMock()
            m.id = name
            return m

        def fn(ex):
            calls.append(ex.id)
            if ex.id == "binance":
                raise Exception("binance GET https://api.binance.com 451 restricted location")
            return 42

        with patch.object(ex_mod, "get_exchange", side_effect=fake_get):
            result, used, errors = ex_mod.fetch_with_fallback(fn, exchanges=["binance", "okx"])
        assert result == 42 and used == "okx"
        assert "binance" in ex_mod._blocked and len(errors) == 1
        # blocked venue is demoted to the end of the priority list
        assert ex_mod.exchange_priority()[-1] in ("binance",) or "binance" not in ex_mod.exchange_priority()[:3]

    def test_fallback_all_fail(self):
        with patch.object(ex_mod, "get_exchange", return_value=MagicMock()):
            with pytest.raises(RuntimeError):
                ex_mod.fetch_with_fallback(lambda ex: (_ for _ in ()).throw(Exception("down")),
                                           exchanges=["okx", "kraken"])

    def test_fallback_empty_result_tries_next(self):
        results = iter([[], [1, 2]])
        with patch.object(ex_mod, "get_exchange", return_value=MagicMock()):
            res, used, errors = ex_mod.fetch_with_fallback(lambda ex: next(results), exchanges=["okx", "kraken"])
        assert res == [1, 2] and used == "kraken" and "empty" in errors[0]


# ---------------------------------------------------------------- blocked hosts
class TestBlockedHosts:
    def test_proxy_error_marks_blocked_without_retries(self):
        session = MagicMock()
        session.get.side_effect = requests.exceptions.ProxyError("Tunnel connection failed: 403 Forbidden")
        with patch.object(http_mod, "_get_session", return_value=session):
            with pytest.raises(desk_common.HttpError) as ei:
                desk_common.http_get("https://blocked.test/x", ttl=0, retries=3)
        assert ei.value.blocked is True
        assert session.get.call_count == 1

    def test_451_marks_blocked(self):
        session = MagicMock()
        session.get.return_value = _resp(451, "")
        with patch.object(http_mod, "_get_session", return_value=session):
            with pytest.raises(desk_common.HttpError) as ei:
                desk_common.http_get("https://geo.test/x", ttl=0)
        assert ei.value.blocked is True

    def test_http_failure_blocked_is_unavailable(self):
        err = desk_common.HttpError("network error", None, "https://www.deribit.com/api/v2/x", blocked=True)
        out = desk_common.http_failure(err, "deribit")
        assert out["status"] == "unavailable" and "www.deribit.com" in out["reason"]

    def test_http_failure_other_is_error(self):
        err = desk_common.HttpError("HTTP 500", 500, "https://x.test")
        assert desk_common.http_failure(err, "x")["status"] == "error"
