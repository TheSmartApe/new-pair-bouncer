"""Terminal output: the colored feed renders every verdict kind, the plain log stays plain, and two
collectors can't share a database."""
import io

import pytest
from rich.console import Console

from sniper import collect, ui
from sniper.checks import BouncerConfig
from sniper.config import SniperConfig


@pytest.fixture
def screen(monkeypatch):
    con = Console(file=io.StringIO(), force_terminal=True, legacy_windows=False, width=160, color_system=None)  # text only, no wrapping
    monkeypatch.setattr(ui, "console", con)
    monkeypatch.setattr(ui, "PRETTY", True)
    return lambda: con.file.getvalue()


def _v(verdict, checks, **features):
    return {"verdict": verdict, "stage": 0, "features": features,
            "checks": [{"key": k, "group": "g", "status": s, "reason": r, "value": None} for k, s, r in checks]}


def test_avoid_leads_with_the_rug_ring(screen):
    v = _v("AVOID", [("buyers", "fail", "4 unique buyers"),
                     ("rug_ring", "fail", "3 of the first-30s buyers were early in launches that died: one of them in 9 of 12")],
           buyers=4, volume_usd=1234.5, reserve_usd=5000)
    ui.verdict("OFY / WETH", v, ts=0, snipers=2)
    out = screen()
    assert "AVOID" in out and "OFY / WETH" in out
    assert out.index("rug ring") < out.index("thin crowd")  # the rug ring reason first, the rest as "also failed"
    assert "one of them in 9 of 12" in out and "$1.2k volume" in out and "2 known serial snipers" in out


def test_watch_and_enter_render(screen):
    ui.verdict("A / WETH", _v("WATCH", [("top10_holders", "warn", "top 10 hold 80%"), ("known_bots", "warn", "3 of 24 buyers are bots")]))
    ui.verdict("B / WETH", _v("ENTER", [("buyers", "pass", "40 unique buyers")], price_usd=8.4265277e-05, fillable=True), position_usd=100)
    out = screen()
    assert "2 warnings" in out and "also: known bots" in out
    assert "passed 1 checks" in out and "$0.00008427" in out


def test_old_rows_without_features_still_render(screen):
    ui.verdict("C / WETH", {"verdict": "AVOID", "stage": 0, "checks": [{"key": "liquidity", "status": "fail", "reason": "$0 in the pool"}]})
    assert "liquidity" in screen()


def test_paper_exit_and_sweep(screen):
    ui.paper_exit("OFY / WETH", {"pnl": -12.5, "usd": 100, "reason": "stop_loss", "opened_ts": 0}, ts=600)
    ui.sweep("robinhood", 3, {"new": 5}, {"ENTER": 1, "WATCH": 0, "AVOID": 4}, 1234, 9)
    out = screen()
    assert "−12.5%" in out and "stop loss" in out and "held 10 min" in out
    assert "+5 new pairs" in out and "4 avoid" in out and "1 enter" in out


def test_plain_mode_prints_no_verdict_blocks(monkeypatch, capsys):
    monkeypatch.setattr(ui, "PRETTY", False)
    ui.verdict("A / WETH", _v("AVOID", [("buyers", "fail", "1 unique buyer")]))
    ui.sweep("robinhood", 1, {"new": 1}, {"ENTER": 0, "WATCH": 0, "AVOID": 1}, 1, 1)
    ui.log("robinhood: +1 new")
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and out.startswith("[") and out.endswith("] robinhood: +1 new\n")


def test_price_format():
    assert ui._price(8.4265277e-05) == "$0.00008427"
    assert ui._price(0) == "$0"
    assert ui._price(12345.6) == "$12,346"


def test_banner_renders(screen):
    cfg = SniperConfig()
    ui.banner(cfg, {"wallets": 10, "trades": 20, "launches": 3, "snipers": 1, "bots": 2, "ring": 4})
    out = screen()
    assert "NEW PAIR BOUNCER" in out and "CoinGecko API" in out and "rug ring wallets" in out
    assert f"${BouncerConfig().position_usd:,.0f} a pair" in out


def test_second_collector_is_refused(tmp_path):
    first = collect.acquire_lock(tmp_path / "db.collect.lock")
    assert first is not None
    assert collect.acquire_lock(tmp_path / "db.collect.lock") is None
    first.close()  # the OS releases it with the process, or here explicitly
    again = collect.acquire_lock(tmp_path / "db.collect.lock")
    assert again is not None
    again.close()
