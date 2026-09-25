"""Builds article-kit/: cover, report card, equity chart, architecture diagram, screenshots, article-draft.md."""
from pathlib import Path

from . import report as report_mod

TEMPLATE_DIR = Path(__file__).parent / "templates"


def _fill(html: str, context: dict) -> str:
    """Replaces every {{key}} in `html` with str(context[key])."""
    for k, v in context.items():
        html = html.replace("{{" + k + "}}", str(v))
    return html


def _shoot(target: str, out_path: Path, width: int, height: int):
    """Screenshots a URL or local file (as a file:// URI) to a PNG at an exact pixel size. Needs Playwright."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError("Playwright isn't installed. Run: uv pip install playwright && playwright install chromium") from e

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(target)
        page.screenshot(path=str(out_path))
        browser.close()


def _cover_context(title: str, handle: str, subtitle: str | None, equity_curve: list | None, theme: str) -> dict:
    return {
        "title": title,
        "subtitle_html": f'<div class="subtitle">{subtitle}</div>' if subtitle else "",
        "handle": handle,
        "logo": report_mod.logo_data_uri("on-dark" if theme == "dark" else "on-light"),
        "visual_svg": report_mod.equity_sparkline_svg(equity_curve, theme=theme),
        "theme": theme,
    }


def build(
    title: str,
    handle: str,
    metrics: dict,
    equity_curve: list,
    credits_used: int,
    out_dir: str | Path = "article-kit",
    screenshot_urls: list[str] | None = None,
    *,
    subtitle: str | None = None,
    benchmark: list | None = None,
    theme: str = "dark",
) -> dict:
    """Renders every article-kit asset into `out_dir` and returns their paths. Needs Playwright installed."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    cover_html = _fill((TEMPLATE_DIR / "cover.html").read_text(), _cover_context(title, handle, subtitle, equity_curve, theme))
    tmp_cover = out / "_cover.html"
    tmp_cover.write_text(cover_html)
    _shoot(tmp_cover.resolve().as_uri(), out / "cover.png", 1500, 600)
    tmp_cover.unlink(missing_ok=True)
    paths["cover"] = str(out / "cover.png")

    (out / "equity.png").write_bytes(report_mod.equity_chart_png(equity_curve, benchmark))
    paths["equity"] = str(out / "equity.png")

    report_mod.render_report_card(
        title,
        metrics,
        credits_used,
        out / "report-card.png",
        title=title,
        subtitle=subtitle,
        equity_curve=equity_curve,
        benchmark=benchmark,
        theme=theme,
    )
    paths["report_card"] = str(out / "report-card.png")

    _shoot((TEMPLATE_DIR / "architecture.svg").resolve().as_uri(), out / "architecture.png", 1200, 700)
    paths["architecture"] = str(out / "architecture.png")

    for i, url in enumerate(screenshot_urls or []):
        shots_dir = out / "screenshots"
        shots_dir.mkdir(exist_ok=True)
        shot = shots_dir / f"screen-{i + 1}.png"
        _shoot(url, shot, 1280, 800)
        paths[f"screenshot_{i + 1}"] = str(shot)

    draft = _fill(
        (TEMPLATE_DIR / "article-draft.md").read_text(),
        {"title": title, "handle": handle, "credits_used": credits_used, **{f"metric_{k}": v for k, v in metrics.items()}},
    )
    (out / "article-draft.md").write_text(draft)
    paths["article_draft"] = str(out / "article-draft.md")
    return paths
