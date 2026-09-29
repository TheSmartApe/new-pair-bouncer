"""Renders docs/architecture.png, the hand-drawn pipeline diagram used in the README.

    .venv/Scripts/python.exe docs/make_architecture.py
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

plt.rcParams["path.sketch"] = (1.8, 120, 24)
plt.rcParams["font.family"] = ["Comic Sans MS", "Segoe Print", "sans-serif"]

INK = "#1e1e1e"
RED, RED_F = "#e03131", "#ffe3e3"
GREEN, GREEN_F = "#2f9e44", "#ebfbee"
GREY, GREY_F = "#495057", "#f1f3f5"

fig, ax = plt.subplots(figsize=(17.0, 8.4), dpi=170)
fig.patch.set_facecolor("#ffffff")
ax.set_facecolor("#ffffff")
ax.set_xlim(-6, 188)
ax.set_ylim(-9, 87)
ax.set_aspect("equal")
ax.axis("off")


def box(x0, y0, x1, y1, edge, fill, lw=2.6):
    ax.add_patch(FancyBboxPatch((x0, y0), x1 - x0, y1 - y0, boxstyle="round,pad=1.0,rounding_size=2.6", linewidth=lw, edgecolor=edge, facecolor=fill, zorder=3))


def arrow(p0, p1, color=INK, lw=2.8, scale=24):
    ax.add_patch(FancyArrowPatch(p0, p1, arrowstyle="-|>", mutation_scale=scale, linewidth=lw, color=color, shrinkA=0, shrinkB=0, zorder=4))


def node(x0, y0, x1, y1, edge, fill, title, sub, mono=None):
    box(x0, y0, x1, y1, edge, fill)
    cx, top = (x0 + x1) / 2, y1
    ax.text(cx, top - 4.2, title, ha="center", va="center", fontsize=15, fontweight="bold", color=edge, zorder=5)
    ax.text(cx, top - 10.2, sub, ha="center", va="center", fontsize=10.8, color=GREY, linespacing=1.45, zorder=5)
    if mono:
        ax.text(cx, y0 + 1.6, mono, ha="center", va="center", fontsize=9.5, color=GREY, family="monospace", zorder=5)


# data in
node(0, 34, 38, 54, INK, GREY_F, "new pools", "every launch on\nRobinhood Chain, every 2 min", "new_pools")
node(50, 34, 90, 54, INK, GREY_F, "launch tape", "every trade in the first 120s,\nblock + second of each buy", "pools/{pool}/trades/range")
arrow((39.5, 44), (48.5, 44))

# classes (code-derived)
node(104, 60, 142, 78, INK, GREY_F, "serial sniper", "buys in the first 10s of\n3+ launches, holds")
node(104, 34, 142, 52, RED, RED_F, "round-tripper", "buys in 10s, dumps within 30s\nat a loss, launch after launch")
node(104, 8, 142, 26, INK, "#ffffff", "serial launcher", "buys in the launch block\nitself (usually the dev)")
arrow((91.5, 47), (102.5, 67))
arrow((91.5, 44), (102.5, 43), color=RED)
arrow((91.5, 41), (102.5, 19))
ax.text(123, 0.5, "packs = serial wallets that hit the same launches in the same block", ha="center", va="center", fontsize=10.8, color=RED, zorder=5)

# outputs
node(154, 60, 184, 78, GREEN, GREEN_F, "live alert", "a known sniper just\nentered a new launch")
node(154, 34, 184, 52, GREEN, GREEN_F, "outcomes", "+1h · +6h · +24h:\nalive? price vs snipers?", "pools/multi")
node(154, 8, 184, 26, GREEN, GREEN_F, "hourly report", "leaderboards, packs,\nthe devs they serve", "tokens/{t}/info")
arrow((143.5, 69), (152.5, 69), color=GREEN)
arrow((143.5, 43), (152.5, 43), color=GREEN)
arrow((143.5, 17), (152.5, 17), color=GREEN)

out = Path(__file__).resolve().parent / "architecture.png"
fig.savefig(out, facecolor="#ffffff", bbox_inches="tight", pad_inches=0.25)
print(out)
