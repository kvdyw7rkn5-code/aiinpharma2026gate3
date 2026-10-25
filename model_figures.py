"""Presentation figures from models_report.json.   python3 model_figures.py  ->  figures/*.png"""
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e0"
S1, S2 = "#2a78d6", "#eb6834"        # categorical slots 1-2 of the reference palette
XMAX = 0.42

plt.rcParams.update({"font.family": "sans-serif", "font.size": 10, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
                     "xtick.color": INK2, "ytick.color": INK, "figure.facecolor": SURFACE, "axes.facecolor": SURFACE})


def accuracy(rep, path):
    ms = sorted(rep["models"], key=lambda m: min(m["tests"]["unseen"]["rmse"], 99), reverse=True)
    fig, ax = plt.subplots(figsize=(10, 5.8))
    for i, m in enumerate(ms):
        a, b = m["tests"]["new_lots"]["rmse"], m["tests"]["unseen"]["rmse"]
        bb = min(b, XMAX)
        ax.plot([a, bb], [i, i], color=GRID, lw=2, zorder=1)
        ax.scatter(a, i, s=70, color=S1, edgecolor=SURFACE, linewidth=2, zorder=3)
        ax.scatter(bb, i, s=70, color=S2, edgecolor=SURFACE, linewidth=2, zorder=3,
                   marker=">" if b > XMAX else "o")
        if b > XMAX:
            ax.text(XMAX - 0.006, i + 0.32, f"{b:.1f} mm off scale", ha="right", va="center", color=INK2, fontsize=8.5)
    ax.set_yticks(range(len(ms)))
    ax.set_yticklabels([f"{m['key']}  {m['name']}" for m in ms])
    for t, m in zip(ax.get_yticklabels(), ms):
        if m["key"] == "M03":
            t.set_fontweight("bold")
    ax.set_xlim(0, XMAX)
    ax.set_xlabel("prediction error, RMSE (mm)   ·   lower is better")
    ax.grid(axis="x", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(length=0)
    ax.scatter([], [], s=70, color=S1, label="new production lots (known settings)")
    ax.scatter([], [], s=70, color=S2, label="a setting never seen in training")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, frameon=False, fontsize=9.5, labelcolor=INK)
    fig.suptitle("All models are about equally accurate on new lots. Only the linear model and the\n"
                 "Gaussian process stay accurate on a pressure or material level they have not seen.",
                 x=0.01, ha="left", fontsize=13, color=INK, y=0.995)
    fig.text(0.01, 0.005, f"{rep['n_units']} ASM-01 units, inputs known before assembly (bag port ID, tube OD, pressure). "
             "Bold = production model (model.json).", color=INK2, fontsize=8.5)
    fig.tight_layout(rect=(0, 0.02, 1, 0.955))
    fig.savefig(path, dpi=200)
    plt.close(fig)


def twin(rep, path):
    names = {m["key"]: m["name"] for m in rep["models"]}
    rows = sorted(rep["twin"].items(), key=lambda kv: kv[1]["caught_in_advance_%"])
    fig, ax = plt.subplots(figsize=(10, 4.2))
    for i, (k, v) in enumerate(rows):
        ax.barh(i, v["caught_in_advance_%"], height=0.6, color=S1 if k == "M03" else "#9ec5f4")
        fa = "  ·  FALSE ALARM on stable line" if v["false_prediction_on_stable_line"] else ""
        ax.text(v["caught_in_advance_%"] + 1, i, f"{v['caught_in_advance_%']:.0f} %  ·  median {v['median_units_early']:.0f} units early{fa}",
                va="center", color=INK, fontsize=9)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([f"{k}  {names[k]}" for k, _ in rows])
    for t, (k, _) in zip(ax.get_yticklabels(), rows):
        if k == "M03":
            t.set_fontweight("bold")
    ax.set_xlim(0, 135)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xlabel("failures predicted before they happened (%)")
    ax.grid(axis="x", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(length=0)
    n = next(iter(rep["twin"].values()))["scenarios"]
    fig.suptitle("Inside the digital twin, the choice of model barely changes the outcome",
                 x=0.01, ha="left", fontsize=13, color=INK)
    fig.text(0.01, 0.01, f"Each model used as the twin's baseline on {n} synthetic de-calibration scenarios "
             "+ the stable 1,800-unit line.", color=INK2, fontsize=8.5)
    fig.tight_layout(rect=(0, 0.03, 1, 0.95))
    fig.savefig(path, dpi=200)
    plt.close(fig)


if __name__ == "__main__":
    rep = json.load(open("models_report.json"))
    os.makedirs("figures", exist_ok=True)
    accuracy(rep, "figures/model_accuracy.png")
    twin(rep, "figures/model_twin.png")
    print("figures/model_accuracy.png, figures/model_twin.png")
