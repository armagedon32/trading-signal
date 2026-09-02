"""Server-side SVG line chart.  No JavaScript, loads instantly, indexable by Google."""

from __future__ import annotations

from html import escape


def svg_line_chart(
    points: list[tuple[str, float]],
    *,
    width: int = 720,
    height: int = 220,
    label: str = "",
    highlight_last: bool = True,
) -> str:
    if len(points) < 2:
        return ""
    pad_left, pad_right, pad_top, pad_bottom = 56, 16, 18, 30
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or (abs(hi) * 0.01 or 1.0)
    lo -= span * 0.08
    hi += span * 0.08
    span = hi - lo

    def x_at(i: int) -> float:
        return pad_left + plot_w * i / (len(points) - 1)

    def y_at(v: float) -> float:
        return pad_top + plot_h * (1 - (v - lo) / span)

    coords = [(x_at(i), y_at(v)) for i, (_, v) in enumerate(points)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
    area = f"{path} L{coords[-1][0]:.1f},{pad_top + plot_h:.1f} L{coords[0][0]:.1f},{pad_top + plot_h:.1f} Z"

    grid = []
    for k in range(5):
        v = lo + span * k / 4
        y = y_at(v)
        grid.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" class="grid"/>'
            f'<text x="{pad_left - 6}" y="{y + 4:.1f}" class="tick" text-anchor="end">{v:,.4g}</text>'
        )

    labels = []
    step = max(1, (len(points) - 1) // 5)
    for i in range(0, len(points), step):
        day = points[i][0]
        labels.append(
            f'<text x="{x_at(i):.1f}" y="{height - 8}" class="tick" text-anchor="middle">{escape(day[5:])}</text>'
        )

    last = ""
    if highlight_last:
        lx, ly = coords[-1]
        last = (
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4" class="dot"/>'
            f'<text x="{min(lx, width - pad_right - 40):.1f}" y="{max(ly - 10, 12):.1f}" class="last" text-anchor="middle">'
            f"{values[-1]:,.4g}</text>"
        )

    title = f"<title>{escape(label)}</title>" if label else ""
    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{escape(label)}" '
        f'xmlns="http://www.w3.org/2000/svg">{title}'
        + "".join(grid)
        + f'<path d="{area}" class="area"/>'
        + f'<path d="{path}" class="line"/>'
        + last
        + "".join(labels)
        + "</svg>"
    )
