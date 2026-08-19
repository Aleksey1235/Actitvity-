from __future__ import annotations

from functools import wraps
from io import BytesIO
from threading import RLock
from math import isnan
from typing import Mapping, Sequence


WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
SEGMENT_RU = ["Утро", "День", "Вечер", "Ночь"]
_RENDER_LOCK = RLock()


def _serialized(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        # matplotlib pyplot has global state. Serializing render calls keeps
        # concurrent Discord requests from corrupting each other.
        with _RENDER_LOCK:
            return func(*args, **kwargs)
    return wrapper


def _plt():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # DejaVu Sans ships with matplotlib and reliably renders Cyrillic on Windows/Linux.
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.titlesize": 16,
            "axes.labelsize": 10,
            "legend.fontsize": 9,
            "figure.dpi": 120,
            "savefig.dpi": 170,
        }
    )
    return plt


def _finish(fig) -> BytesIO:
    output = BytesIO()
    fig.savefig(output, format="png", bbox_inches="tight", facecolor="white")
    output.seek(0)
    return output


def _clean_axes(ax, *, grid_axis: str = "y") -> None:
    ax.grid(True, axis=grid_axis, alpha=0.16, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_alpha(0.25)
    ax.spines["bottom"].set_alpha(0.25)


def _empty_chart(title: str, message: str, *, figsize=(9.2, 4.8)) -> BytesIO:
    plt = _plt()
    fig, ax = plt.subplots(figsize=figsize)
    ax.axis("off")
    ax.text(0.5, 0.63, title, ha="center", va="center", fontsize=17, fontweight="bold")
    ax.text(0.5, 0.43, message, ha="center", va="center", fontsize=11, wrap=True)
    fig.tight_layout()
    output = _finish(fig)
    plt.close(fig)
    return output


@_serialized
def weekly_comparison_png(
    labels: Sequence[str],
    current: Sequence[int],
    previous: Sequence[int],
    *,
    subtitle: str | None = None,
) -> BytesIO:
    """Polished daily unique-attendance comparison for Discord weekly reports."""
    if not labels:
        return _empty_chart("Активность по дням", "За выбранный период пока нет дней для отображения.")

    plt = _plt()
    fig, ax = plt.subplots(figsize=(9.6, 5.0))
    x = list(range(len(labels)))
    current_values = [int(v) for v in current]
    previous_values = [int(v) for v in previous]

    ax.plot(x, current_values, marker="o", linewidth=2.5, markersize=6.5, label="Эта неделя")
    ax.plot(x, previous_values, marker="o", linewidth=2.0, markersize=5.5, linestyle="--", label="Прошлая неделя")

    top = max([0, *current_values, *previous_values])
    label_pad = max(0.7, top * 0.035)
    for idx, value in enumerate(current_values):
        ax.text(idx, value + label_pad, str(value), ha="center", va="bottom", fontsize=9, fontweight="bold")
    for idx, value in enumerate(previous_values):
        # Avoid text collisions when the points are equal.
        if idx < len(current_values) and current_values[idx] == value:
            ax.text(idx, value - label_pad, str(value), ha="center", va="top", fontsize=8.5, alpha=0.72)
        else:
            ax.text(idx, value + label_pad, str(value), ha="center", va="bottom", fontsize=8.5, alpha=0.72)

    ax.set_title("Уникальные участники по дням", loc="left", fontweight="bold", pad=14)
    if subtitle:
        ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9.5, alpha=0.72, va="bottom")
    ax.set_ylabel("Уникальных участников")
    ax.set_xticks(x, labels)
    ax.set_ylim(bottom=0, top=max(2, top * 1.22 + 1))
    ax.legend(frameon=False, loc="upper left", ncol=2)
    _clean_axes(ax)
    fig.tight_layout()
    output = _finish(fig)
    plt.close(fig)
    return output


@_serialized
def pulse_history_png(
    labels: Sequence[str],
    scores: Sequence[float | None],
    confidences: Sequence[float | None] | None = None,
) -> BytesIO:
    """Family Pulse trend. None scores are rendered as gaps, never as fake zeroes."""
    if len(labels) < 2 or sum(v is not None for v in scores) < 2:
        return _empty_chart(
            "Family Pulse по неделям",
            "Нужно минимум две недели с опубликованной оценкой Family Pulse.\nНедостоверные периоды не подставляются как 0.",
        )

    plt = _plt()
    fig, ax = plt.subplots(figsize=(10.2, 5.0))
    x = list(range(len(labels)))
    y = [float("nan") if v is None else float(v) for v in scores]
    ax.plot(x, y, marker="o", linewidth=2.5, markersize=6.5, label="Family Pulse")

    for idx, value in enumerate(scores):
        if value is None:
            continue
        confidence_text = ""
        if confidences and idx < len(confidences) and confidences[idx] is not None:
            confidence_text = f"\n{float(confidences[idx]):.0%} дост."
        ax.annotate(
            f"{int(round(value))}{confidence_text}",
            (idx, float(value)),
            textcoords="offset points",
            xytext=(0, 9),
            ha="center",
            fontsize=8.5,
        )

    ax.set_title("Family Pulse по неделям", loc="left", fontweight="bold", pad=14)
    ax.text(
        0,
        1.02,
        "Исторические снимки недель. Периоды без достаточной достоверности показаны разрывом.",
        transform=ax.transAxes,
        fontsize=9.3,
        alpha=0.72,
        va="bottom",
    )
    ax.set_ylabel("Family Pulse")
    ax.set_xticks(x, labels, rotation=25, ha="right")
    ax.set_ylim(0, 105)
    ax.set_yticks([0, 20, 40, 60, 80, 100])
    _clean_axes(ax)
    fig.tight_layout()
    output = _finish(fig)
    plt.close(fig)
    return output


@_serialized
def category_distribution_png(
    counts: Mapping[str, int],
    *,
    period_label: str,
) -> BytesIO:
    labels = ["Тренировки", "Семейный контент", "Фракционный контент"]
    keys = ["training", "family", "faction"]
    values = [int(counts.get(k, 0)) for k in keys]

    plt = _plt()
    fig, ax = plt.subplots(figsize=(9.4, 5.0))
    bars = ax.bar(labels, values, width=0.62)
    top = max([0, *values])
    for bar, value in zip(bars, values, strict=True):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            value + max(0.25, top * 0.025),
            str(value),
            ha="center",
            va="bottom",
            fontsize=10,
            fontweight="bold",
        )
    ax.set_title("Какие активности проводит семья", loc="left", fontweight="bold", pad=14)
    ax.text(0, 1.02, period_label, transform=ax.transAxes, fontsize=9.4, alpha=0.72, va="bottom")
    ax.set_ylabel("Аналитических активностей")
    ax.set_ylim(bottom=0, top=max(2, top * 1.2 + 1))
    ax.tick_params(axis="x", labelrotation=0)
    _clean_axes(ax)
    fig.tight_layout()
    output = _finish(fig)
    plt.close(fig)
    return output


@_serialized
def group_coverage_png(
    *,
    main_members: int,
    main_unique: int,
    academy_members: int,
    academy_unique: int,
    period_label: str,
) -> BytesIO:
    if main_members <= 0 and academy_members <= 0:
        return _empty_chart(
            "Охват Main и Academy",
            "В выбранном периоде нет участников Main/Academy для сравнения.",
        )

    plt = _plt()
    fig, ax = plt.subplots(figsize=(8.8, 5.0))
    names = ["Основной состав", "Academy"]
    percentages = [
        (main_unique / main_members * 100) if main_members else 0,
        (academy_unique / academy_members * 100) if academy_members else 0,
    ]
    totals = [(main_unique, main_members), (academy_unique, academy_members)]
    bars = ax.bar(names, percentages, width=0.55)
    for bar, percentage, (unique, total) in zip(bars, percentages, totals, strict=True):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            percentage + 2.2,
            f"{percentage:.0f}%\n{unique}/{total}",
            ha="center",
            va="bottom",
            fontsize=10,
            fontweight="bold",
        )
    ax.set_title("Охват: основной состав vs Academy", loc="left", fontweight="bold", pad=14)
    ax.text(
        0,
        1.02,
        f"{period_label}. Охват = доля участников группы, посетивших хотя бы одну активность.",
        transform=ax.transAxes,
        fontsize=9.2,
        alpha=0.72,
        va="bottom",
    )
    ax.set_ylabel("Охват, %")
    ax.set_ylim(0, 112)
    ax.set_yticks([0, 20, 40, 60, 80, 100])
    _clean_axes(ax)
    fig.tight_layout()
    output = _finish(fig)
    plt.close(fig)
    return output


@_serialized
def activity_heatmap_png(
    matrix: Sequence[Sequence[int]],
    *,
    period_label: str,
    weekday_labels: Sequence[str] = WEEKDAY_RU,
    segment_labels: Sequence[str] = SEGMENT_RU,
) -> BytesIO:
    """7x4 heat map using planned activity time, aligned with availability segments."""
    import numpy as np

    if len(matrix) != len(weekday_labels) or any(len(row) != len(segment_labels) for row in matrix):
        raise ValueError("Heatmap matrix shape does not match labels")

    data = np.asarray(matrix, dtype=int)
    plt = _plt()
    fig, ax = plt.subplots(figsize=(8.8, 6.0))
    image = ax.imshow(data, aspect="auto", interpolation="nearest", cmap="Blues")

    ax.set_title("Когда семья проводит активности", loc="left", fontweight="bold", pad=14)
    ax.text(
        0,
        1.02,
        f"{period_label}. Используется плановое время аналитических активностей.",
        transform=ax.transAxes,
        fontsize=9.2,
        alpha=0.72,
        va="bottom",
    )
    ax.set_xticks(range(len(segment_labels)), segment_labels)
    ax.set_yticks(range(len(weekday_labels)), weekday_labels)
    ax.set_xlabel("Время суток")
    ax.set_ylabel("День недели")

    max_value = int(data.max()) if data.size else 0
    threshold = max_value / 2 if max_value else 0
    for y in range(data.shape[0]):
        for x in range(data.shape[1]):
            value = int(data[y, x])
            text_color = "white" if max_value and value > threshold else "black"
            ax.text(x, y, str(value), ha="center", va="center", fontsize=10, color=text_color, fontweight="bold")

    cbar = fig.colorbar(image, ax=ax, fraction=0.035, pad=0.03)
    cbar.set_label("Количество активностей")
    fig.tight_layout()
    output = _finish(fig)
    plt.close(fig)
    return output
