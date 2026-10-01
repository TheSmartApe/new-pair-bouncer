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

fig, ax = plt.subplots(figsize=(17.0, 9.6), dpi=170)
fig.patch.set_facecolor("#ffffff")
ax.set_facecolor("#ffffff")
ax.set_xlim(-6, 190)
ax.set_ylim(10, 98)
ax.set_aspect("equal")
ax.axis("off")


def box(x0, y0, x1, y1, edge, fill, lw=2.6):
    ax.add_patch(FancyBboxPatch((x0, y0), x1 - x0, y1 - y0, boxstyle="round,pad=1.0,rounding_size=2.6", linewidth=lw, edgecolor=edge, facecolor=fill, zorder=3))


def arrow(p0, p1, color=INK, lw=2.8, scale=24):
    ax.add_patch(FancyArrowPatch(p0, p1, arrowstyle="-|>", mutation_scale=scale, linewidth=lw, color=color, shrinkA=0, shrinkB=0, zorder=4))


def node(x0, y0, x1, y1, edge, fill, title, sub, mono=None, title_size=15):
    box(x0, y0, x1, y1, edge, fill)
    cx, top = (x0 + x1) / 2, y1
    ax.text(cx, top - 4.2, title, ha="center", va="center", fontsize=title_size, fontweight="bold", color=edge, zorder=5)
    ax.text(cx, top - 10.6, sub, ha="center", va="center", fontsize=10.6, color=GREY, linespacing=1.45, zorder=5)
    if mono:
        ax.text(cx, y0 + 1.6, mono, ha="center", va="center", fontsize=9.3, color=GREY, family="monospace", zorder=5)


# data in
node(0, 52, 36, 74, INK, GREY_F, "new pair", "every new pool on\nRobinhood Chain", "new_pools")
node(48, 52, 86, 74, INK, GREY_F, "launch tape", "every trade of the first\n2 minutes, with wallets", "trades/range · tokens/multi")
arrow((37.5, 63), (46.5, 63))

# memory
node(48, 18, 86, 38, INK, "#ffffff", "memory", "known bots, wallet clusters,\ndeployers who pulled liquidity\nbefore, learned from past launches")
arrow((67, 50.5), (67, 40), color=GREY)
ax.text(70, 45, "every launch\nteaches it", ha="left", va="center", fontsize=10, color=GREY, linespacing=1.4, zorder=5)

# the checks
node(100, 40, 140, 86, RED, RED_F, "the bouncer", "", title_size=16)
checks = [
    "dev already sold?",
    "early buyers hold 25%+",
    "of the supply?",
    "deployer rugged before?",
    "known bots / one cluster",
    "doing the buying?",
    "a real crowd of buyers?",
]
for i, line in enumerate(checks):
    ax.text(120, 77 - i * 4.6, line, ha="center", va="center", fontsize=10.4, color=INK, zorder=5)
ax.text(120, 42.5, "token info · wallet PnL", ha="center", va="center", fontsize=9.3, color=GREY, family="monospace", zorder=5)
arrow((87.5, 63), (98.5, 63), color=RED)
arrow((87.5, 28), (101, 38.5), color=GREY)

# outputs
node(152, 70, 186, 90, GREEN, GREEN_F, "ENTER", "passed every check:\nthe paper bot buys")
node(152, 44, 186, 64, RED, RED_F, "AVOID", "with the reason,\non the dashboard")
node(152, 18, 186, 38, GREEN, "#ffffff", "3 paper books", "bouncer vs crowd only\nvs buy everything,\nsame exits")
arrow((141.5, 72), (150.5, 80), color=GREEN)
arrow((141.5, 58), (150.5, 54), color=RED)
arrow((141.5, 46), (150.5, 32), color=GREY)


out = Path(__file__).resolve().parent / "architecture.png"
fig.savefig(out, facecolor="#ffffff", bbox_inches="tight", pad_inches=0.25)
print(out)
