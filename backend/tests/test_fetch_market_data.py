"""Unit tests for market data fetch / Parquet cache / split cross-check.

Live Massive/yfinance calls are mocked — no API key required for this suite.
Optional live integration can be enabled later with MASSIVE_API_KEY + marker.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pandas as pd
import pytest

from backend.data.fetch_market_data import (
    BARS_COLUMNS,
    CALENDAR_COLUMNS,
    DEFAULT_SYMBOLS,
    DIVIDENDS_COLUMNS,
    SPLITS_COLUMNS,
    FetchConfig,
    MassiveClient,
    bar_path,
    bars_to_dataframe,
    cached_bars_nonempty,
    calendar_to_dataframe,
    cross_check_splits,
    dividends_to_dataframe,
    ensure_cache_layout,
    fetch_yfinance_splits,
    grouped_bars_to_frames,
    last_weekday_on_or_before,
    load_config,
    merge_bar_frames,
    parse_retry_after,
    read_parquet,
    redact_url_for_log,
    resolve_api_key,
    run_fetch,
    splits_to_dataframe,
    write_cross_check_report,
    write_parquet,
    merge_bar_frames,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _agg_bar(ts_ms: int, close: float = 100.0) -> dict[str, Any]:
    return {
        "o": close - 1,
        "h": close + 1,
        "l": close - 2,
        "c": close,
        "v": 1_000_000,
        "vw": close,
        "n": 5000,
        "t": ts_ms,
    }


class FakeResponse:
    def __init__(
        self,
        payload: Any,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("GET", "https://example.com"),
                response=httpx.Response(self.status_code, headers=self.headers),
            )

    def json(self) -> Any:
        return self._payload


class FakeHttpClient:
    """Routes GETs by path substring to canned JSON payloads.

    A route value may be a payload dict/list, a FakeResponse, or a list of
    FakeResponse objects (consumed in order for retry scenarios).
    """

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, Any] | None, dict[str, str] | None]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FakeResponse:
        self.calls.append((url, params, headers))
        for key, payload in self.routes.items():
            if key in url:
                if (
                    isinstance(payload, list)
                    and payload
                    and isinstance(payload[0], FakeResponse)
                ):
                    return payload.pop(0)
                if isinstance(payload, FakeResponse):
                    return payload
                return FakeResponse(payload)
        raise AssertionError(f"No fake route matched url={url!r}")


@pytest.fixture
def sample_massive_splits() -> pd.DataFrame:
    return splits_to_dataframe(
        [
            {
                "ticker": "AAPL",
                "execution_date": "2020-08-31",
                "split_from": 1,
                "split_to": 4,
                "adjustment_type": "forward_split",
                "historical_adjustment_factor": 0.25,
                "id": "split-aapl-2020",
            },
            {
                "ticker": "TSLA",
                "execution_date": "2022-08-25",
                "split_from": 1,
                "split_to": 3,
                "adjustment_type": "forward_split",
                "historical_adjustment_factor": 0.333,
                "id": "split-tsla-2022",
            },
        ]
    )


@pytest.fixture
def sample_yfinance_splits() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "execution_date": "2020-08-31",
                "split_ratio": 4.0,
                "source": "yfinance",
            },
            # TSLA missing here → missing_in_yfinance
            {
                "ticker": "NVDA",
                "execution_date": "2024-06-10",
                "split_ratio": 10.0,
                "source": "yfinance",
            },
        ]
    )


# ---------------------------------------------------------------------------
# Config / path layout
# ---------------------------------------------------------------------------


def test_resolve_api_key_prefers_massive() -> None:
    assert (
        resolve_api_key({"MASSIVE_API_KEY": "m1", "POLYGON_API_KEY": "p1"}) == "m1"
    )


def test_resolve_api_key_falls_back_to_polygon() -> None:
    assert resolve_api_key({"POLYGON_API_KEY": "p1"}) == "p1"


def test_resolve_api_key_missing() -> None:
    assert resolve_api_key({}) is None


def test_load_config_requires_key() -> None:
    with pytest.raises(ValueError, match="MASSIVE_API_KEY"):
        load_config(env={})


def test_load_config_defaults(tmp_path: Path) -> None:
    cfg = load_config(
        cache_dir=tmp_path,
        env={"MASSIVE_API_KEY": "test-key", "MARKET_DATA_LOOKBACK_DAYS": "365"},
    )
    assert cfg.api_key == "test-key"
    assert cfg.lookback_days == 365
    assert cfg.symbols == DEFAULT_SYMBOLS
    assert len(DEFAULT_SYMBOLS) >= 30
    assert cfg.s3_bucket is None
    assert cfg.s3_prefix == "market-data"
    assert cfg.aws_region == "us-east-1"


def test_load_config_s3_env(tmp_path: Path) -> None:
    cfg = load_config(
        cache_dir=tmp_path,
        env={
            "MASSIVE_API_KEY": "test-key",
            "S3_CACHE_BUCKET": "  trade-recon-cache  ",
            "S3_CACHE_PREFIX": "",
            "AWS_DEFAULT_REGION": "us-east-2",
        },
    )
    assert cfg.s3_bucket == "trade-recon-cache"
    assert cfg.s3_prefix == "market-data"
    assert cfg.aws_region == "us-east-2"


def test_ensure_cache_layout(tmp_path: Path) -> None:
    paths = ensure_cache_layout(tmp_path)
    assert paths["bars"].is_dir()
    assert paths["cross_check_dir"].is_dir()
    assert paths["splits"].name == "splits.parquet"
    assert bar_path(tmp_path, "aapl") == tmp_path / "bars" / "AAPL.parquet"


# ---------------------------------------------------------------------------
# DataFrame normalizers / parquet round-trip
# ---------------------------------------------------------------------------


def test_bars_to_dataframe_shape() -> None:
    # 2024-01-02 00:00:00 UTC
    raw = [_agg_bar(1704153600000, 185.0)]
    df = bars_to_dataframe("AAPL", raw)
    assert list(df.columns) == list(BARS_COLUMNS)
    assert len(df) == 1
    assert df.iloc[0]["ticker"] == "AAPL"
    assert df.iloc[0]["date"] == "2024-01-02"
    assert df.iloc[0]["close"] == 185.0


def test_bars_empty() -> None:
    df = bars_to_dataframe("AAPL", [])
    assert list(df.columns) == list(BARS_COLUMNS)
    assert df.empty


def test_splits_dividends_calendar_columns() -> None:
    splits = splits_to_dataframe(
        [
            {
                "ticker": "AAPL",
                "execution_date": "2020-08-31",
                "split_from": 1,
                "split_to": 4,
                "adjustment_type": "forward_split",
                "historical_adjustment_factor": 0.25,
                "id": "x",
            }
        ]
    )
    assert list(splits.columns) == list(SPLITS_COLUMNS)

    dividends = dividends_to_dataframe(
        [
            {
                "ticker": "AAPL",
                "ex_dividend_date": "2024-05-10",
                "pay_date": "2024-05-16",
                "record_date": "2024-05-13",
                "declaration_date": "2024-05-02",
                "cash_amount": 0.25,
                "split_adjusted_cash_amount": 0.25,
                "frequency": 4,
                "distribution_type": "recurring",
                "historical_adjustment_factor": 1.0,
                "id": "d1",
            }
        ]
    )
    assert list(dividends.columns) == list(DIVIDENDS_COLUMNS)

    calendar = calendar_to_dataframe(
        [
            {
                "date": "2024-11-28",
                "exchange": "NYSE",
                "name": "Thanksgiving",
                "status": "closed",
            }
        ]
    )
    assert list(calendar.columns) == list(CALENDAR_COLUMNS)


def test_parquet_round_trip(tmp_path: Path) -> None:
    df = bars_to_dataframe("MSFT", [_agg_bar(1704153600000, 370.0)])
    path = write_parquet(df, bar_path(tmp_path, "MSFT"))
    assert path.exists()
    loaded = read_parquet(path)
    assert list(loaded.columns) == list(BARS_COLUMNS)
    assert len(loaded) == 1
    assert loaded.iloc[0]["ticker"] == "MSFT"
    assert float(loaded.iloc[0]["close"]) == 370.0


# ---------------------------------------------------------------------------
# Massive client (mocked HTTP)
# ---------------------------------------------------------------------------


def test_massive_client_daily_bars() -> None:
    payload = {
        "results": [_agg_bar(1704153600000, 100.0)],
        "status": "OK",
    }
    http = FakeHttpClient({"/v2/aggs/ticker/AAPL/range/": payload})
    client = MassiveClient("key", client=http)
    bars = client.fetch_daily_bars("AAPL", date(2024, 1, 1), date(2024, 1, 31))
    assert len(bars) == 1
    _url, params, headers = http.calls[0]
    assert params is None or "apiKey" not in params
    assert headers is not None
    assert headers.get("Authorization") == "Bearer key"


def test_massive_client_pagination() -> None:
    page1 = {
        "results": [
            {
                "ticker": "AAPL",
                "execution_date": "2020-08-31",
                "split_from": 1,
                "split_to": 4,
            }
        ],
        "next_url": "https://api.massive.com/stocks/v1/splits?cursor=abc",
        "status": "OK",
    }
    page2 = {
        "results": [
            {
                "ticker": "TSLA",
                "execution_date": "2022-08-25",
                "split_from": 1,
                "split_to": 3,
            }
        ],
        "status": "OK",
    }
    http = FakeHttpClient(
        {
            "/stocks/v1/splits?cursor=abc": page2,
            "/stocks/v1/splits": page1,
        }
    )
    client = MassiveClient("key", client=http)
    rows = client.fetch_splits(("AAPL", "TSLA"), date(2020, 1, 1), date(2024, 1, 1))
    assert len(rows) == 2
    assert rows[0]["ticker"] == "AAPL"
    assert rows[1]["ticker"] == "TSLA"


def test_massive_client_holidays_list_response() -> None:
    holidays = [
        {
            "date": "2024-11-28",
            "exchange": "NYSE",
            "name": "Thanksgiving",
            "status": "closed",
        }
    ]
    http = FakeHttpClient({"/v1/marketstatus/upcoming": holidays})
    client = MassiveClient("key", client=http)
    assert client.fetch_market_holidays() == holidays


def test_redact_url_for_log_strips_query() -> None:
    url = "https://api.massive.com/v2/aggs/ticker/NVDA/range/1/day/a/b?apiKey=SECRET"
    assert redact_url_for_log(url) == (
        "https://api.massive.com/v2/aggs/ticker/NVDA/range/1/day/a/b"
    )
    assert "SECRET" not in redact_url_for_log(url)


def test_parse_retry_after() -> None:
    assert parse_retry_after("2.5", fallback=1.0) == 2.5
    assert parse_retry_after(None, fallback=3.0) == 3.0
    assert parse_retry_after("nope", fallback=4.0) == 4.0


def test_massive_client_retries_429_with_retry_after() -> None:
    payload = {"results": [_agg_bar(1704153600000, 100.0)], "status": "OK"}
    sleeps: list[float] = []
    http = FakeHttpClient(
        {
            "/v2/aggs/ticker/NVDA/range/": [
                FakeResponse({}, status_code=429, headers={"Retry-After": "2"}),
                FakeResponse(payload),
            ]
        }
    )
    client = MassiveClient("secret-key", client=http, sleep=sleeps.append)
    bars = client.fetch_daily_bars("NVDA", date(2024, 1, 1), date(2024, 1, 31))
    assert len(bars) == 1
    assert len(http.calls) == 2
    assert sleeps == [2.0]


def test_massive_client_retries_503_exponential_backoff() -> None:
    payload = {"results": [_agg_bar(1704153600000, 100.0)], "status": "OK"}
    sleeps: list[float] = []
    http = FakeHttpClient(
        {
            "/v2/aggs/ticker/AAPL/range/": [
                FakeResponse({}, status_code=503),
                FakeResponse({}, status_code=503),
                FakeResponse(payload),
            ]
        }
    )
    client = MassiveClient(
        "key",
        client=http,
        base_backoff_seconds=1.0,
        sleep=sleeps.append,
    )
    bars = client.fetch_daily_bars("AAPL", date(2024, 1, 1), date(2024, 1, 31))
    assert len(bars) == 1
    assert sleeps == [1.0, 2.0]


def test_massive_client_exhausts_retries_on_429() -> None:
    http = FakeHttpClient(
        {
            "/v2/aggs/ticker/AAPL/range/": [
                FakeResponse({}, status_code=429),
                FakeResponse({}, status_code=429),
            ]
        }
    )
    client = MassiveClient(
        "key",
        client=http,
        max_retries=1,
        base_backoff_seconds=0.01,
        sleep=lambda _: None,
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_daily_bars("AAPL", date(2024, 1, 1), date(2024, 1, 31))
    assert len(http.calls) == 2


def test_cached_bars_nonempty(tmp_path: Path) -> None:
    assert not cached_bars_nonempty(tmp_path, "AAPL")
    empty = pd.DataFrame(columns=list(BARS_COLUMNS))
    write_parquet(empty, bar_path(tmp_path, "AAPL"))
    assert not cached_bars_nonempty(tmp_path, "AAPL")
    df = bars_to_dataframe("AAPL", [_agg_bar(1704153600000, 185.0)])
    write_parquet(df, bar_path(tmp_path, "AAPL"))
    assert cached_bars_nonempty(tmp_path, "AAPL")


def test_run_fetch_skips_when_cache_covers_end(tmp_path: Path) -> None:
    existing = bars_to_dataframe("AAPL", [_agg_bar(1704153600000, 185.0)])
    write_parquet(existing, bar_path(tmp_path, "AAPL"))

    routes = {
        "/v2/aggs/ticker/AAPL/range/": {
            "results": [_agg_bar(1704153600000, 999.0)],
            "status": "OK",
        },
        "/v2/aggs/ticker/MSFT/range/": {
            "results": [_agg_bar(1704153600000, 370.0)],
            "status": "OK",
        },
        "/stocks/v1/splits": {"results": [], "status": "OK"},
        "/stocks/v1/dividends": {"results": [], "status": "OK"},
        "/v1/marketstatus/upcoming": [],
    }
    http = FakeHttpClient(routes)
    client = MassiveClient("k", client=http)
    config = FetchConfig(
        api_key="k",
        symbols=("AAPL", "MSFT"),
        cache_dir=tmp_path,
        lookback_days=7,
        skip_yfinance=True,
        force=False,
        incremental=True,
        skip_cached=False,
        symbol_delay_seconds=0.0,
    )
    summary = run_fetch(
        config,
        client=client,
        end_date=date(2024, 1, 2),
        sleep=lambda _: None,
    )
    assert "AAPL" in summary["bars_skipped"]
    assert summary["bars_fetched"] == 1
    aapl = read_parquet(tmp_path / "bars" / "AAPL.parquet")
    assert float(aapl.iloc[0]["close"]) == 185.0
    bar_urls = [u for u, _p, _h in http.calls if "/v2/aggs/ticker/" in u]
    assert all("MSFT" in u for u in bar_urls)
    assert not any("AAPL" in u for u in bar_urls)


def test_run_fetch_force_overwrites_existing(tmp_path: Path) -> None:
    existing = bars_to_dataframe("AAPL", [_agg_bar(1704153600000, 185.0)])
    write_parquet(existing, bar_path(tmp_path, "AAPL"))
    routes = {
        "/v2/aggs/ticker/AAPL/range/": {
            "results": [_agg_bar(1704153600000, 999.0)],
            "status": "OK",
        },
        "/stocks/v1/splits": {"results": [], "status": "OK"},
        "/stocks/v1/dividends": {"results": [], "status": "OK"},
        "/v1/marketstatus/upcoming": [],
        "/v2/aggs/grouped/locale/us/market/stocks/": {"results": [], "status": "OK"},
        "/v2/aggs/ticker/AAPL/prev": {"results": [], "status": "OK"},
    }
    http = FakeHttpClient(routes)
    client = MassiveClient("k", client=http)
    config = FetchConfig(
        api_key="k",
        symbols=("AAPL",),
        cache_dir=tmp_path,
        lookback_days=7,
        skip_yfinance=True,
        force=True,
        symbol_delay_seconds=0.0,
    )
    summary = run_fetch(
        config,
        client=client,
        end_date=date(2024, 1, 10),
        sleep=lambda _: None,
    )
    assert summary["bars_skipped"] == []
    assert summary["bars_fetched"] == 1
    aapl = read_parquet(tmp_path / "bars" / "AAPL.parquet")
    assert float(aapl.iloc[0]["close"]) == 999.0


def test_merge_bar_frames_keeps_latest_on_date() -> None:
    older = bars_to_dataframe("AAPL", [_agg_bar(1704153600000, 185.0)])
    newer = bars_to_dataframe("AAPL", [_agg_bar(1704153600000, 190.0)])
    merged = merge_bar_frames(older, newer)
    assert len(merged) == 1
    assert float(merged.iloc[0]["close"]) == 190.0


# ---------------------------------------------------------------------------
# Cross-check logic
# ---------------------------------------------------------------------------


def test_cross_check_splits_match_and_mismatches(
    sample_massive_splits: pd.DataFrame,
    sample_yfinance_splits: pd.DataFrame,
) -> None:
    report = cross_check_splits(sample_massive_splits, sample_yfinance_splits)
    statuses = set(report["status"].tolist())
    assert "match" in statuses
    assert "missing_in_yfinance" in statuses
    assert "missing_in_massive" in statuses

    aapl = report[(report["ticker"] == "AAPL") & (report["status"] == "match")]
    assert len(aapl) == 1
    assert float(aapl.iloc[0]["massive_ratio"]) == 4.0

    tsla = report[report["ticker"] == "TSLA"]
    assert tsla.iloc[0]["status"] == "missing_in_yfinance"

    nvda = report[report["ticker"] == "NVDA"]
    assert nvda.iloc[0]["status"] == "missing_in_massive"


def test_cross_check_ratio_mismatch() -> None:
    massive = splits_to_dataframe(
        [
            {
                "ticker": "XYZ",
                "execution_date": "2023-01-15",
                "split_from": 1,
                "split_to": 2,
                "adjustment_type": "forward_split",
                "historical_adjustment_factor": 0.5,
                "id": "1",
            }
        ]
    )
    yf = pd.DataFrame(
        [
            {
                "ticker": "XYZ",
                "execution_date": "2023-01-15",
                "split_ratio": 3.0,
                "source": "yfinance",
            }
        ]
    )
    report = cross_check_splits(massive, yf)
    assert report.iloc[0]["status"] == "ratio_mismatch"


def test_write_cross_check_report(tmp_path: Path) -> None:
    paths = ensure_cache_layout(tmp_path)
    report = pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "status": "match",
                "massive_date": "2020-08-31",
                "yfinance_date": "2020-08-31",
                "massive_ratio": 4.0,
                "yfinance_ratio": 4.0,
                "detail": "ok",
            }
        ]
    )
    out = write_cross_check_report(report, paths)
    assert out["json"].exists()
    assert out["csv"].exists()
    payload = json.loads(out["json"].read_text(encoding="utf-8"))
    assert payload["source_of_record"] == "massive"
    assert payload["summary"]["matches"] == 1


def test_fetch_yfinance_splits_with_stub_module() -> None:
    class StubSplits(dict):
        pass

    class StubTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol
            if symbol == "AAPL":
                idx = pd.to_datetime(["2020-08-31"])
                self.splits = pd.Series([4.0], index=idx)
            else:
                self.splits = pd.Series(dtype=float)

    class StubYf:
        @staticmethod
        def Ticker(symbol: str) -> StubTicker:
            return StubTicker(symbol)

    df = fetch_yfinance_splits(("AAPL", "MSFT"), yf_module=StubYf)
    assert len(df) == 1
    assert df.iloc[0]["ticker"] == "AAPL"
    assert float(df.iloc[0]["split_ratio"]) == 4.0


# ---------------------------------------------------------------------------
# End-to-end run_fetch with injected clients
# ---------------------------------------------------------------------------


def test_run_fetch_writes_expected_layout(tmp_path: Path) -> None:
    routes = {
        "/v2/aggs/ticker/AAPL/range/": {
            "results": [_agg_bar(1704153600000, 185.0)],
            "status": "OK",
        },
        "/v2/aggs/ticker/MSFT/range/": {
            "results": [_agg_bar(1704153600000, 370.0)],
            "status": "OK",
        },
        "/stocks/v1/splits": {
            "results": [
                {
                    "ticker": "AAPL",
                    "execution_date": "2020-08-31",
                    "split_from": 1,
                    "split_to": 4,
                    "adjustment_type": "forward_split",
                    "historical_adjustment_factor": 0.25,
                    "id": "s1",
                }
            ],
            "status": "OK",
        },
        "/stocks/v1/dividends": {
            "results": [
                {
                    "ticker": "AAPL",
                    "ex_dividend_date": "2024-05-10",
                    "cash_amount": 0.25,
                    "id": "d1",
                }
            ],
            "status": "OK",
        },
        "/v1/marketstatus/upcoming": [
            {
                "date": "2024-11-28",
                "exchange": "NYSE",
                "name": "Thanksgiving",
                "status": "closed",
            }
        ],
        "/v2/aggs/grouped/locale/us/market/stocks/": {"results": [], "status": "OK"},
        "/v2/aggs/ticker/AAPL/prev": {"results": [], "status": "OK"},
        "/v2/aggs/ticker/MSFT/prev": {"results": [], "status": "OK"},
    }
    http = FakeHttpClient(routes)
    client = MassiveClient("test-key", client=http)
    config = FetchConfig(
        api_key="test-key",
        symbols=("AAPL", "MSFT"),
        cache_dir=tmp_path,
        lookback_days=30,
        skip_yfinance=False,
        symbol_delay_seconds=0.0,
    )

    def fake_yf(symbols: tuple[str, ...]) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "ticker": "AAPL",
                    "execution_date": "2020-08-31",
                    "split_ratio": 4.0,
                    "source": "yfinance",
                }
            ]
        )

    summary = run_fetch(
        config,
        client=client,
        yfinance_fetcher=fake_yf,
        end_date=date(2024, 1, 15),
    )

    assert (tmp_path / "bars" / "AAPL.parquet").exists()
    assert (tmp_path / "bars" / "MSFT.parquet").exists()
    assert (tmp_path / "splits.parquet").exists()
    assert (tmp_path / "dividends.parquet").exists()
    assert (tmp_path / "calendar.parquet").exists()
    assert (tmp_path / "cross_check" / "splits_report.json").exists()
    assert (tmp_path / "cross_check" / "splits_report.csv").exists()

    aapl = read_parquet(tmp_path / "bars" / "AAPL.parquet")
    assert list(aapl.columns) == list(BARS_COLUMNS)
    assert len(aapl) == 1

    splits = read_parquet(tmp_path / "splits.parquet")
    assert list(splits.columns) == list(SPLITS_COLUMNS)
    assert len(splits) == 1

    assert summary["splits_rows"] == 1
    assert summary["dividends_rows"] == 1
    assert summary["calendar_rows"] == 1
    assert summary["cross_check_mismatches"] == 0


def test_run_fetch_optional_s3_upload(tmp_path: Path) -> None:
    routes = {
        "/v2/aggs/ticker/AAPL/range/": {"results": [], "status": "OK"},
        "/stocks/v1/splits": {"results": [], "status": "OK"},
        "/stocks/v1/dividends": {"results": [], "status": "OK"},
        "/v1/marketstatus/upcoming": [],
        "/v2/aggs/grouped/locale/us/market/stocks/": {"results": [], "status": "OK"},
        "/v2/aggs/ticker/AAPL/prev": {"results": [], "status": "OK"},
    }
    client = MassiveClient("k", client=FakeHttpClient(routes))
    config = FetchConfig(
        api_key="k",
        symbols=("AAPL",),
        cache_dir=tmp_path,
        lookback_days=7,
        skip_yfinance=True,
        s3_bucket="my-bucket",
        s3_prefix="market-data",
        symbol_delay_seconds=0.0,
    )
    s3 = MagicMock()
    summary = run_fetch(config, client=client, s3_client=s3, end_date=date(2024, 1, 10))
    assert s3.upload_file.called
    assert any(u.startswith("s3://my-bucket/market-data/") for u in summary["s3_uploaded"])


def test_last_weekday_skips_weekend() -> None:
    assert last_weekday_on_or_before(date(2026, 8, 15)) == date(2026, 8, 14)
    assert last_weekday_on_or_before(date(2026, 8, 14)) == date(2026, 8, 14)


def test_run_fetch_grouped_daily_backfills_lagging_range(tmp_path: Path) -> None:
    """Per-ticker range stops at 13 Aug; grouped daily supplies the 14 Aug session."""
    ts_13 = int(datetime(2026, 8, 13, tzinfo=timezone.utc).timestamp() * 1000)
    ts_14 = int(datetime(2026, 8, 14, tzinfo=timezone.utc).timestamp() * 1000)
    routes = {
        "/v2/aggs/ticker/AAPL/range/": {
            "results": [_agg_bar(ts_13, 220.0)],
            "status": "OK",
        },
        "/v2/aggs/ticker/AAPL/prev": {"results": [], "status": "OK"},
        "/v2/aggs/grouped/locale/us/market/stocks/": {
            "results": [
                {
                    "T": "AAPL",
                    "o": 221.0,
                    "h": 222.0,
                    "l": 220.0,
                    "c": 221.5,
                    "v": 1_000_000,
                    "vw": 221.2,
                    "n": 4000,
                    "t": ts_14,
                }
            ],
            "status": "OK",
        },
        "/stocks/v1/splits": {"results": [], "status": "OK"},
        "/stocks/v1/dividends": {"results": [], "status": "OK"},
        "/v1/marketstatus/upcoming": [],
    }
    http = FakeHttpClient(routes)
    client = MassiveClient("k", client=http)
    config = FetchConfig(
        api_key="k",
        symbols=("AAPL",),
        cache_dir=tmp_path,
        lookback_days=5,
        skip_yfinance=True,
        incremental=True,
        symbol_delay_seconds=0.0,
    )
    summary = run_fetch(
        config,
        client=client,
        end_date=date(2026, 8, 15),
        sleep=lambda _: None,
    )
    assert "AAPL" in summary["bars_grouped_backfill"]
    aapl = read_parquet(tmp_path / "bars" / "AAPL.parquet")
    aapl["date"] = pd.to_datetime(aapl["date"]).dt.date
    dates = set(aapl["date"].tolist())
    assert date(2026, 8, 13) in dates
    assert date(2026, 8, 14) in dates
    grouped_urls = [u for u, _p, _h in http.calls if "grouped" in u]
    assert grouped_urls
    assert "2026-08-14" in grouped_urls[0]


def test_grouped_bars_to_frames_uses_session_date_not_timestamp() -> None:
    ts_wrong = int(datetime(2026, 8, 13, 20, 0, tzinfo=timezone.utc).timestamp() * 1000)
    frames = grouped_bars_to_frames(
        [
            {
                "T": "AAPL",
                "o": 1,
                "h": 2,
                "l": 1,
                "c": 1.5,
                "v": 10,
                "vw": 1.4,
                "n": 3,
                "t": ts_wrong,
            }
        ],
        date(2026, 8, 14),
        ("AAPL",),
    )
    df = frames["AAPL"]
    assert df.iloc[0]["date"] == "2026-08-14"
