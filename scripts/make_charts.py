#!/usr/bin/env python3
"""Draw the result charts as plain SVG.

No plotting library is required: the SVG is generated directly, so the project
keeps its "standard library only" property.  Open the .svg files in any browser.
"""

from __future__ import annotations

import csv
import os
import statistics
import sys
from collections import defaultdict
from html import escape

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import config  # noqa: E402

PHASES = ["baseline", "load", "throughput", "failover", "recovery"]
COLOURS = ["#2b6cb0", "#2f855a", "#b7791f", "#c53030", "#553c9a"]

WIDTH = 760
HEIGHT = 420
MARGIN_LEFT = 80
MARGIN_RIGHT = 30
MARGIN_TOP = 60
MARGIN_BOTTOM = 90


def load_rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def frame(title: str, subtitle: str, body: str) -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">
  <rect width="100%%" height="100%%" fill="#ffffff"/>
  <text x="%d" y="28" font-family="sans-serif" font-size="18" fill="#1a202c">%s</text>
  <text x="%d" y="48" font-family="sans-serif" font-size="12" fill="#4a5568">%s</text>
%s
</svg>
""" % (
        WIDTH,
        HEIGHT,
        WIDTH,
        HEIGHT,
        MARGIN_LEFT,
        escape(title),
        MARGIN_LEFT,
        escape(subtitle),
        body,
    )


def bar_chart(title: str, subtitle: str, labels, values, unit: str, colour: str = "#2b6cb0") -> str:
    plot_w = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    plot_h = HEIGHT - MARGIN_TOP - MARGIN_BOTTOM
    peak = max(values) if values and max(values) > 0 else 1.0
    slot = plot_w / max(len(labels), 1)
    bar_w = min(70.0, slot * 0.55)
    parts = []
    # axis
    parts.append(
        '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#cbd5e0"/>'
        % (MARGIN_LEFT, MARGIN_TOP, MARGIN_LEFT, MARGIN_TOP + plot_h)
    )
    parts.append(
        '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#cbd5e0"/>'
        % (MARGIN_LEFT, MARGIN_TOP + plot_h, WIDTH - MARGIN_RIGHT, MARGIN_TOP + plot_h)
    )
    for index, (label, value) in enumerate(zip(labels, values)):
        height = (value / peak) * (plot_h - 20)
        x = MARGIN_LEFT + slot * index + (slot - bar_w) / 2
        y = MARGIN_TOP + plot_h - height
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>' % (x, y, bar_w, height, colour))
        parts.append(
            '<text x="%.1f" y="%.1f" font-family="sans-serif" font-size="12" fill="#1a202c" text-anchor="middle">%.2f</text>'
            % (x + bar_w / 2, y - 6, value)
        )
        parts.append(
            '<text x="%.1f" y="%d" font-family="sans-serif" font-size="12" fill="#4a5568" text-anchor="middle">%s</text>'
            % (x + bar_w / 2, MARGIN_TOP + plot_h + 20, escape(label))
        )
    parts.append(
        '<text x="%d" y="%d" font-family="sans-serif" font-size="12" fill="#4a5568">%s</text>'
        % (MARGIN_LEFT, MARGIN_TOP - 12, escape(unit))
    )
    return frame(title, subtitle, "\n".join(parts))


def grouped_chart(title: str, subtitle: str, categories, series: dict, unit: str) -> str:
    plot_w = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    plot_h = HEIGHT - MARGIN_TOP - MARGIN_BOTTOM
    peak = max([value for values in series.values() for value in values] + [1])
    slots = len(categories)
    names = list(series.keys())
    slot = plot_w / max(slots, 1)
    bar_w = min(30.0, slot / (len(names) + 1))
    parts = [
        '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#cbd5e0"/>'
        % (MARGIN_LEFT, MARGIN_TOP, MARGIN_LEFT, MARGIN_TOP + plot_h),
        '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#cbd5e0"/>'
        % (MARGIN_LEFT, MARGIN_TOP + plot_h, WIDTH - MARGIN_RIGHT, MARGIN_TOP + plot_h),
        '<text x="%d" y="%d" font-family="sans-serif" font-size="12" fill="#4a5568">%s</text>'
        % (MARGIN_LEFT, MARGIN_TOP - 12, escape(unit)),
    ]
    for cat_index, category in enumerate(categories):
        start = MARGIN_LEFT + slot * cat_index + (slot - bar_w * len(names)) / 2
        for series_index, name in enumerate(names):
            value = series[name][cat_index]
            height = (value / peak) * (plot_h - 20)
            x = start + series_index * bar_w
            y = MARGIN_TOP + plot_h - height
            parts.append(
                '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>'
                % (x, y, bar_w - 2, height, COLOURS[series_index % len(COLOURS)])
            )
            if value:
                parts.append(
                    '<text x="%.1f" y="%.1f" font-family="sans-serif" font-size="10" fill="#1a202c" text-anchor="middle">%d</text>'
                    % (x + (bar_w - 2) / 2, y - 4, value)
                )
        parts.append(
            '<text x="%.1f" y="%d" font-family="sans-serif" font-size="12" fill="#4a5568" text-anchor="middle">%s</text>'
            % (MARGIN_LEFT + slot * cat_index + slot / 2, MARGIN_TOP + plot_h + 20, escape(category))
        )
    for series_index, name in enumerate(names):
        legend_x = MARGIN_LEFT + series_index * 110
        legend_y = HEIGHT - 40
        parts.append(
            '<rect x="%d" y="%d" width="12" height="12" fill="%s"/>'
            % (legend_x, legend_y, COLOURS[series_index % len(COLOURS)])
        )
        parts.append(
            '<text x="%d" y="%d" font-family="sans-serif" font-size="12" fill="#4a5568">%s</text>'
            % (legend_x + 18, legend_y + 11, escape(name))
        )
    return frame(title, subtitle, "\n".join(parts))


def main() -> int:
    rows = load_rows(config.RESULTS_CSV)
    if not rows:
        print("no results to chart")
        return 1

    ok_rows = [r for r in rows if r.get("ok") == "True"]
    if not ok_rows:
        print("no successful requests to chart")
        return 1

    os.makedirs(config.CHARTS_DIR, exist_ok=True)

    # 1. mean latency per phase
    labels, values = [], []
    for phase in PHASES:
        group = [float(r["latency_ms"]) for r in ok_rows if r.get("phase") == phase]
        if group:
            labels.append(phase)
            values.append(statistics.fmean(group))
    path = os.path.join(config.CHARTS_DIR, "latency_by_phase.svg")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            bar_chart(
                "Mean response time per phase",
                "Measured at the virtual IP through the OpenFlow switch (lower is better)",
                labels,
                values,
                "milliseconds",
            )
        )
    print("wrote %s" % path)

    # 2. throughput per server
    by_server: dict[str, list[float]] = defaultdict(list)
    for row in ok_rows:
        if row.get("phase") == "throughput":
            by_server[row.get("server_host") or "?"].append(float(row["throughput_mbps"]))
    if by_server:
        servers = sorted(by_server)
        path = os.path.join(config.CHARTS_DIR, "throughput_by_server.svg")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                bar_chart(
                    "Mean throughput per server",
                    "4 MB transfers; the figures are virtual-Ethernet limited, compare them relatively",
                    servers,
                    [statistics.fmean(by_server[s]) for s in servers],
                    "megabits per second",
                    colour="#2f855a",
                )
            )
        print("wrote %s" % path)

    # 3. requests served per server per phase
    servers = sorted({row.get("server_host") for row in ok_rows if row.get("server_host")})
    series = {}
    for server in servers:
        counts = []
        for phase in PHASES:
            counts.append(
                sum(
                    1
                    for row in ok_rows
                    if row.get("phase") == phase and row.get("server_host") == server
                )
            )
        series[server] = counts
    if series:
        path = os.path.join(config.CHARTS_DIR, "requests_by_server.svg")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                grouped_chart(
                    "Successful requests per server",
                    "How the controller spread connections across the pool in each phase",
                    PHASES,
                    series,
                    "requests",
                )
            )
        print("wrote %s" % path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
