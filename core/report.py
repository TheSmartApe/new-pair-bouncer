"""Turns a paper-trading run into a shareable report: equity chart, report.html, and a report-card PNG."""
import base64
import io
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

TEMPLATE_DIR = Path(__file__).parent / "templates"
BRAND_DIR = Path(__file__).parent / "brand"

# Chart colors per theme: strategy line, benchmark line, gridlines, muted text.
_THEME_COLORS = {
    "light": {"strategy": "#3f9b1a", "benchmark": "#9aa79e", "grid": "#dfe6da", "muted": "#6b7a70"},
    "dark": {"strategy": "#8bc53f", "benchmark": "#5f6b64", "grid": "#223028", "muted": "#b7c2ba"},
}


def logo_data_uri(variant: str = "on-light") -> str:
    """Base64 data: URI for a CoinGecko API lockup (on-light | on-dark | mono), so templates need no relative paths."""
    svg = (BRAND_DIR / f"coingecko-api-{variant}.svg").read_text()
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def format_usd(value: float | None, *, signed: bool = False) -> str:
    """123.4 -> '$123.40'; -50 -> '-$50.00'; signed=True adds a '+' for positive values."""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ("+" if signed else "")
    return f"{sign}${abs(value):,.2f}"


def format_pct(value: float | None, *, decimals: int = 1, signed: bool = False) -> str:
    """Formats a number that's already in percent units: 18.75 -> '18.75%' (or '+18.75%' if signed and positive)."""
    if value is None:
        return "—"
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def format_ratio_as_pct(ratio: float | None, *, decimals: int = 0) -> str:
    """Formats a 0-1 ratio as a percent: 0.64 -> '64%'."""
    if ratio is None:
        return "—"
    return f"{ratio * 100:.{decimals}f}%"


def prettify_run_name(run_name: str) -> str:
    """'smart-money-radar-demo' -> 'Smart Money Radar Demo', used when no human title is supplied."""
    return run_name.replace("-", " ").replace("_", " ").strip().title()


def _pnl_class(pnl_usd: float | None, pnl_pct: float | None) -> str:
    """'positive' | 'negative' | '' for coloring the P&L stat, preferring the $ figure."""
    value = pnl_usd if pnl_usd is not None else pnl_pct
    if not value:
        return ""
    return "positive" if value > 0 else "negative"


def equity_chart_png(equity_curve: list[tuple[float, float]], benchmark: list[tuple[float, float]] | None = None) -> bytes:
    """Renders the equity curve, plus an optional benchmark line, to an in-memory PNG."""
    fig, ax = plt.subplots(figsize=(8, 4))
    if equity_curve:
        xs = [datetime.fromtimestamp(ts, tz=timezone.utc) for ts, _ in equity_curve]
        ax.plot(xs, [e for _, e in equity_curve], label="strategy", color="#8bc53f", linewidth=2)
    if benchmark:
        xs = [datetime.fromtimestamp(ts, tz=timezone.utc) for ts, _ in benchmark]
        ax.plot(xs, [e for _, e in benchmark], label="benchmark", color="#999999", linestyle="--")
    ax.set_ylabel("Equity (USD)")
    if equity_curve or benchmark:
        ax.legend()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=144)
    plt.close(fig)
    return buf.getvalue()


def _scale_points(series, x_min, x_max, y_min, y_max, width, height, pad=6, top_pad=6):
    """Maps (ts, value) points into SVG pixel coordinates within width x height."""
    x_span = (x_max - x_min) or 1
    y_span = (y_max - y_min) or 1
    pts = []
    for t, v in series:
        x = pad + (t - x_min) / x_span * (width - 2 * pad)
        y = height - pad - (v - y_min) / y_span * (height - pad - top_pad)
        pts.append((x, y))
    return pts


def _smooth_path(points: list[tuple[float, float]]) -> str:
    """SVG path 'd' string that smooths a polyline using quadratic-bezier midpoints."""
    if len(points) < 3:
        return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in points)
    d = [f"M{points[0][0]:.1f},{points[0][1]:.1f}"]
    for i in range(1, len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        d.append(f"Q{x0:.1f},{y0:.1f} {mx:.1f},{my:.1f}")
    d.append(f"L{points[-1][0]:.1f},{points[-1][1]:.1f}")
    return " ".join(d)


def equity_svg(
    equity_curve: list[tuple[float, float]],
    benchmark: list[tuple[float, float]] | None = None,
    *,
    width: int = 1000,
    height: int = 200,
    theme: str = "light",
) -> str:
    """Inline SVG hero chart for the report card: a filled strategy line, plus an optional lighter dashed benchmark line."""
    colors = _THEME_COLORS.get(theme, _THEME_COLORS["light"])
    if not equity_curve:
        return (
            f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
            f'<text x="{width / 2}" y="{height / 2}" fill="{colors["muted"]}" font-size="15" '
            f'text-anchor="middle" dominant-baseline="middle">No equity data yet</text></svg>'
        )

    combined = equity_curve + (benchmark or [])
    xs = [t for t, _ in combined]
    ys = [v for _, v in combined]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if y_min == y_max:
        y_min, y_max = y_min - 1, y_max + 1

    parts = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
        f'<defs><linearGradient id="eqFill" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{colors["strategy"]}" stop-opacity="0.28"/>'
        f'<stop offset="100%" stop-color="{colors["strategy"]}" stop-opacity="0"/></linearGradient></defs>'
    ]
    parts.append(
        "".join(
            f'<line x1="0" y1="{height * f:.1f}" x2="{width}" y2="{height * f:.1f}" '
            f'stroke="{colors["grid"]}" stroke-width="1" stroke-dasharray="4 4"/>'
            for f in (0.25, 0.5, 0.75)
        )
    )

    if benchmark:
        b_pts = _scale_points(benchmark, x_min, x_max, y_min, y_max, width, height)
        parts.append(
            f'<path d="{_smooth_path(b_pts)}" fill="none" stroke="{colors["benchmark"]}" '
            f'stroke-width="2" stroke-dasharray="7 5" stroke-linecap="round"/>'
        )

    s_pts = _scale_points(equity_curve, x_min, x_max, y_min, y_max, width, height)
    line_d = _smooth_path(s_pts)
    area_d = f"{line_d} L{s_pts[-1][0]:.1f},{height} L{s_pts[0][0]:.1f},{height} Z"
    parts.append(f'<path d="{area_d}" fill="url(#eqFill)" stroke="none"/>')
    parts.append(
        f'<path d="{line_d}" fill="none" stroke="{colors["strategy"]}" '
        f'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
    )
    lx, ly = s_pts[-1]
    parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4.5" fill="{colors["strategy"]}"/>')
    parts.append("</svg>")
    return "".join(parts)


def equity_sparkline_svg(equity_curve: list[tuple[float, float]] | None, *, width: int = 380, height: int = 340, theme: str = "dark") -> str:
    """Stylized, decorative equity sparkline for the cover's right-side visual. Falls back to an abstract motif with no data."""
    if not equity_curve or len(equity_curve) < 2:
        return abstract_chart_motif_svg(width=width, height=height, theme=theme)

    colors = _THEME_COLORS.get(theme, _THEME_COLORS["dark"])
    xs = [t for t, _ in equity_curve]
    ys = [v for _, v in equity_curve]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if y_min == y_max:
        y_min, y_max = y_min - 1, y_max + 1

    pts = _scale_points(equity_curve, x_min, x_max, y_min, y_max, width, height, pad=10, top_pad=30)
    line_d = _smooth_path(pts)
    area_d = f"{line_d} L{pts[-1][0]:.1f},{height} L{pts[0][0]:.1f},{height} Z"
    accent = colors["strategy"]
    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
        f'<defs><linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{accent}" stop-opacity="0.45"/>'
        f'<stop offset="100%" stop-color="{accent}" stop-opacity="0"/></linearGradient></defs>'
        f'<path d="{area_d}" fill="url(#sparkFill)" stroke="none"/>'
        f'<path d="{line_d}" fill="none" stroke="{accent}" stroke-width="4" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        f'<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="6" fill="{accent}"/>'
        "</svg>"
    )


def abstract_chart_motif_svg(*, width: int = 380, height: int = 340, theme: str = "dark") -> str:
    """A tasteful decorative ascending-bars motif for a cover with no run data to plot."""
    colors = _THEME_COLORS.get(theme, _THEME_COLORS["dark"])
    accent = colors["strategy"]
    bars = [0.32, 0.5, 0.42, 0.66, 0.58, 0.8, 1.0]
    n = len(bars)
    gap = 16
    bar_w = (width - gap * (n - 1)) / n
    rects = []
    for i, h_frac in enumerate(bars):
        bar_h = height * 0.68 * h_frac
        x = i * (bar_w + gap)
        y = height - bar_h
        opacity = 0.22 + 0.68 * (i / (n - 1))
        rects.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
            f'rx="{bar_w / 2:.1f}" fill="{accent}" opacity="{opacity:.2f}"/>'
        )
    return f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">' + "".join(rects) + "</svg>"


def _fill(html: str, context: dict) -> str:
    """Replaces every {{key}} in `html` with str(context[key])."""
    for k, v in context.items():
        html = html.replace("{{" + k + "}}", str(v))
    return html


def _card_context(
    display_title: str,
    subtitle: str | None,
    metrics: dict,
    credits_used: int,
    equity_curve: list | None,
    benchmark: list | None,
    theme: str,
) -> dict:
    pnl_usd = metrics.get("pnl_usd")
    pnl_pct = metrics.get("pnl_pct")
    return {
        "theme": theme,
        "title": display_title,
        "subtitle_html": f'<div class="subtitle">{subtitle}</div>' if subtitle else "",
        "pnl_usd_display": format_usd(pnl_usd, signed=True),
        "pnl_pct_display": format_pct(pnl_pct, signed=True),
        "pnl_class": _pnl_class(pnl_usd, pnl_pct),
        "win_rate_display": format_ratio_as_pct(metrics.get("win_rate")),
        "trades": metrics.get("trades", "—"),
        "max_drawdown_display": format_pct(metrics.get("max_drawdown_pct")),
        "credits_used": credits_used,
        "logo": logo_data_uri("on-dark" if theme == "dark" else "on-light"),
        "equity_svg": equity_svg(equity_curve or [], benchmark, theme=theme),
        "benchmark_legend_html": (
            '<span><span class="swatch benchmark"></span><span class="text">Benchmark</span></span>' if benchmark else ""
        ),
    }


def _matplotlib_card(
    display_title: str,
    subtitle: str | None,
    metrics: dict,
    credits_used: int,
    out_path: Path,
    equity_curve: list | None = None,
    benchmark: list | None = None,
    theme: str = "light",
):
    """Fallback report card when Playwright isn't installed: a stat block plus a plain equity plot, drawn with matplotlib."""
    is_dark = theme == "dark"
    bg = "#0b0f0d" if is_dark else "#f7f9f6"
    fg = "#f4f7f4" if is_dark else "#12271a"
    muted = "#b7c2ba" if is_dark else "#6b7a70"
    accent = "#8bc53f" if is_dark else "#3f9b1a"

    fig = plt.figure(figsize=(12, 6.3), facecolor=bg)

    ax_text = fig.add_axes((0.06, 0.42, 0.88, 0.52))
    ax_text.axis("off")
    ax_text.text(0, 0.92, display_title, fontsize=24, weight="bold", color=fg)
    if subtitle:
        ax_text.text(0, 0.74, subtitle, fontsize=14, color=muted)
    lines = [
        f"P&L: {format_usd(metrics.get('pnl_usd'), signed=True)} ({format_pct(metrics.get('pnl_pct'), signed=True)})",
        f"Win rate: {format_ratio_as_pct(metrics.get('win_rate'))}",
        f"Trades: {metrics.get('trades', '—')}",
        f"Max drawdown: {format_pct(metrics.get('max_drawdown_pct'))}",
        f"Credits used: {credits_used}",
    ]
    for i, line in enumerate(lines):
        ax_text.text(0, 0.52 - i * 0.13, line, fontsize=14, color=fg)

    ax_chart = fig.add_axes((0.06, 0.06, 0.88, 0.30))
    ax_chart.set_facecolor(bg)
    if equity_curve:
        xs = [t for t, _ in equity_curve]
        ax_chart.plot(xs, [v for _, v in equity_curve], color=accent, linewidth=2)
    if benchmark:
        xs = [t for t, _ in benchmark]
        ax_chart.plot(xs, [v for _, v in benchmark], color=muted, linewidth=1.5, linestyle="--")
    ax_chart.set_xticks([])
    for spine in ax_chart.spines.values():
        spine.set_color(muted)
    ax_chart.tick_params(colors=muted)

    fig.savefig(out_path, dpi=100, facecolor=bg)
    plt.close(fig)


def render_report_card(
    run_name: str,
    metrics: dict,
    credits_used: int,
    out_path: str | Path,
    *,
    title: str | None = None,
    subtitle: str | None = None,
    equity_curve: list | None = None,
    benchmark: list | None = None,
    theme: str = "light",
):
    """Renders templates/report-card.html with Playwright if it's installed, else falls back to matplotlib."""
    out_path = Path(out_path)
    display_title = title or prettify_run_name(run_name)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _matplotlib_card(display_title, subtitle, metrics, credits_used, out_path, equity_curve, benchmark, theme)
        return
    html = _fill(
        (TEMPLATE_DIR / "report-card.html").read_text(),
        _card_context(display_title, subtitle, metrics, credits_used, equity_curve, benchmark, theme),
    )
    tmp = out_path.with_suffix(".tmp.html")
    tmp.write_text(html)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1200, "height": 630})
            page.goto(tmp.resolve().as_uri())
            page.screenshot(path=str(out_path))
            browser.close()
    except Exception:
        _matplotlib_card(display_title, subtitle, metrics, credits_used, out_path, equity_curve, benchmark, theme)
    finally:
        tmp.unlink(missing_ok=True)


def _html(run_name: str, metrics: dict, credits_used: int, chart_bytes: bytes) -> str:
    chart_b64 = base64.b64encode(chart_bytes).decode()
    rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in metrics.items())
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{run_name} report</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 720px; margin: 40px auto; color: #1a1a1a; }}
table {{ border-collapse: collapse; width: 100%; }}
td {{ padding: 6px 10px; border-bottom: 1px solid #eee; }}
img {{ width: 100%; border-radius: 8px; }}
</style></head>
<body>
<h1>{run_name}</h1>
<p>Credits used: {credits_used}</p>
<img src="data:image/png;base64,{chart_b64}" alt="equity curve">
<table>{rows}</table>
</body></html>"""


def build(
    run_name: str,
    metrics: dict,
    equity_curve: list,
    benchmark: list | None = None,
    credits_used: int = 0,
    out_dir: str | Path = "reports",
    *,
    title: str | None = None,
    subtitle: str | None = None,
    theme: str = "light",
) -> dict:
    """Writes {out_dir}/{run_name}/report.html, equity.png, and report-card.png. Returns their paths."""
    out = Path(out_dir) / run_name
    out.mkdir(parents=True, exist_ok=True)
    chart_bytes = equity_chart_png(equity_curve, benchmark)
    (out / "equity.png").write_bytes(chart_bytes)
    card_path = out / "report-card.png"
    render_report_card(
        run_name,
        metrics,
        credits_used,
        card_path,
        title=title,
        subtitle=subtitle,
        equity_curve=equity_curve,
        benchmark=benchmark,
        theme=theme,
    )
    html_path = out / "report.html"
    html_path.write_text(_html(run_name, metrics, credits_used, chart_bytes))
    return {"html": str(html_path), "equity_png": str(out / "equity.png"), "card_png": str(card_path)}
