"""My flat insights: monthly payment trend, maintenance paid vs pending, corpus progress — as inline SVG."""
from datetime import date
from html import escape

from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CorpusPayment, LedgerPeriod, MaintenancePayment, Project, ProjectContribution
from app.services import corpus_campaigns as campaigns_svc

COLORS = {"maintenance": "#38bdf8", "corpus": "#a78bfa", "projects": "#fbbf24", "paid": "#4ade80", "pending": "#f87171"}


def _month_keys(db: Session, months: int, today: date) -> list[tuple[int, int]]:
    first = db.scalar(select(LedgerPeriod).order_by(LedgerPeriod.year, LedgerPeriod.month).limit(1))
    start = (first.year, first.month) if first else (today.year, today.month)
    keys, y, m = [], start[0], start[1]
    while (y, m) <= (today.year, today.month):
        keys.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return keys[-months:] or [(today.year, today.month)]


def _label(key: tuple[int, int]) -> str:
    return date(key[0], key[1], 1).strftime("%b %y")


def flat_insights(db: Session, flat_ids: list[int], months: int = 12, today: date | None = None) -> dict:
    today = today or date.today()
    keys = _month_keys(db, months, today)
    index = {k: i for i, k in enumerate(keys)}
    trend = {name: [0] * len(keys) for name in ("maintenance", "corpus", "projects")}

    def add(kind: str, when: date | None, amount: int) -> None:
        if when is not None and (when.year, when.month) in index:
            trend[kind][index[(when.year, when.month)]] += amount

    maint = db.scalars(select(MaintenancePayment).where(MaintenancePayment.flat_id.in_(flat_ids))).all()
    for mp in maint:
        if mp.is_paid:
            add("maintenance", mp.paid_on, mp.amount)
    for cp in db.scalars(select(CorpusPayment).where(CorpusPayment.flat_id.in_(flat_ids), CorpusPayment.is_paid.is_(True))).all():
        add("corpus", cp.paid_on, cp.amount)
    for pc in db.scalars(
        select(ProjectContribution).join(Project, ProjectContribution.project_id == Project.id)
        .where(ProjectContribution.flat_id.in_(flat_ids), Project.deleted_at.is_(None))
    ).all():
        add("projects", pc.paid_on, pc.amount)

    periods = db.scalars(select(LedgerPeriod).order_by(LedgerPeriod.year, LedgerPeriod.month)).all()[-months:]
    by_period = {}
    for mp in maint:
        paid, pending = by_period.get(mp.period_id, (0, 0))
        by_period[mp.period_id] = (paid + mp.amount, pending) if mp.is_paid else (paid, pending + mp.amount)
    status = {
        "labels": [_label((p.year, p.month)) for p in periods],
        "paid": [by_period.get(p.id, (0, 0))[0] for p in periods],
        "pending": [by_period.get(p.id, (0, 0))[1] for p in periods],
    }

    corpus_rows = [
        (f"{r['campaign'].name} · {r['flat'].number}" if len(flat_ids) > 1 else r["campaign"].name, r["paid"], r["share"])
        for r in campaigns_svc.open_campaigns_for_flats(db, flat_ids, today)
        if r["share"]
    ]
    labels = [_label(k) for k in keys]
    return {
        "trend": {"labels": labels, **trend},
        "status": status,
        "corpus": corpus_rows,
        "trend_svg": bar_chart_svg(labels, [("Maintenance", COLORS["maintenance"], trend["maintenance"]),
                                            ("Corpus", COLORS["corpus"], trend["corpus"]),
                                            ("Projects", COLORS["projects"], trend["projects"])],
                                   title="Payments made per month"),
        "status_svg": bar_chart_svg(status["labels"], [("Paid", COLORS["paid"], status["paid"]),
                                                       ("Pending", COLORS["pending"], status["pending"])],
                                    stacked=True, title="Maintenance paid vs pending by month"),
        "corpus_svg": progress_svg(corpus_rows, title="Corpus progress for your share"),
    }


def _money(v: int) -> str:
    return f"₹{v:,}"


def bar_chart_svg(labels: list[str], series: list[tuple[str, str, list[int]]], *, stacked: bool = False,
                  title: str = "", width: int = 640, height: int = 220) -> Markup:
    left, bottom, top = 56, 34, 26
    plot_w, plot_h = width - left - 10, height - bottom - top
    n = max(len(labels), 1)
    if stacked:
        peak = max([sum(s[2][i] for s in series) for i in range(len(labels))] or [0])
    else:
        peak = max([v for s in series for v in s[2]] or [0])
    peak = peak or 1
    slot = plot_w / n
    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}" '
           f'xmlns="http://www.w3.org/2000/svg">', f'<title>{escape(title)}</title>']
    for frac in (0, 0.5, 1):
        y = top + plot_h * (1 - frac)
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - 10}" y2="{y:.1f}" class="chart-grid"/>')
        out.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" class="chart-axis">{_money(int(peak * frac))}</text>')
    for i, label in enumerate(labels):
        x0 = left + slot * i
        if stacked:
            y = top + plot_h
            for name, color, values in series:
                h = plot_h * values[i] / peak
                y -= h
                if values[i]:
                    out.append(f'<rect x="{x0 + slot * 0.18:.1f}" y="{y:.1f}" width="{slot * 0.64:.1f}" height="{h:.1f}" '
                               f'fill="{color}"><title>{escape(label)} · {escape(name)}: {_money(values[i])}</title></rect>')
        else:
            bw = slot * 0.8 / max(len(series), 1)
            for j, (name, color, values) in enumerate(series):
                h = plot_h * values[i] / peak
                if values[i]:
                    out.append(f'<rect x="{x0 + slot * 0.1 + bw * j:.1f}" y="{top + plot_h - h:.1f}" width="{bw:.1f}" '
                               f'height="{h:.1f}" fill="{color}"><title>{escape(label)} · {escape(name)}: '
                               f'{_money(values[i])}</title></rect>')
        out.append(f'<text x="{x0 + slot / 2:.1f}" y="{height - bottom + 16}" text-anchor="middle" class="chart-axis">{escape(label)}</text>')
    lx = left
    for name, color, _ in series:
        out.append(f'<rect x="{lx}" y="6" width="10" height="10" fill="{color}"/>'
                   f'<text x="{lx + 14}" y="15" class="chart-legend">{escape(name)}</text>')
        lx += 24 + 8 * len(name)
    out.append("</svg>")
    return Markup("".join(out))


def progress_svg(rows: list[tuple[str, int, int]], *, title: str = "", width: int = 640) -> Markup:
    if not rows:
        return Markup("")
    row_h, label_w = 30, 220
    height = row_h * len(rows) + 10
    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}" '
           f'xmlns="http://www.w3.org/2000/svg">', f'<title>{escape(title)}</title>']
    bar_w = width - label_w - 130
    for i, (label, paid, share) in enumerate(rows):
        y = 6 + i * row_h
        frac = min(paid / share, 1) if share else 0
        out.append(f'<text x="0" y="{y + 15}" class="chart-legend">{escape(label[:32])}</text>')
        out.append(f'<rect x="{label_w}" y="{y + 4}" width="{bar_w}" height="14" rx="4" class="chart-track"/>')
        out.append(f'<rect x="{label_w}" y="{y + 4}" width="{bar_w * frac:.1f}" height="14" rx="4" fill="{COLORS["paid"]}">'
                   f'<title>{escape(label)}: {_money(paid)} of {_money(share)}</title></rect>')
        out.append(f'<text x="{label_w + bar_w + 8}" y="{y + 15}" class="chart-legend">{_money(paid)} / {_money(share)} '
                   f'({int(frac * 100)}%)</text>')
    out.append("</svg>")
    return Markup("".join(out))
