"""PDF summary report (one month or all time) built with reportlab."""
import io
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.dates import format_date_ddmmyyyy as ddmmyyyy, format_ist_stamp
from app.models import (
    CorpusPayment,
    Expense,
    Flat,
    LedgerPeriod,
    MaintenancePayment,
    Project,
    ProjectPayment,
    Settlement,
)
from app.config import PORTAL_NAME
from app.services import corpus_campaigns as campaigns_svc
from app.services import ledger as ledger_svc
from app.services import payment_source as source_svc


def rs(amount: int | None) -> str:
    # Built-in PDF fonts have no rupee glyph.
    return f"Rs. {int(amount or 0):,}"


def _table(rows: list[list], col_widths=None, total_row: bool = False) -> Table:
    tbl = Table(rows, colWidths=col_widths, repeatRows=1)
    style = [
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef7")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#b8c2d0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]
    if total_row:
        style.append(("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 8.5))
    tbl.setStyle(TableStyle(style))
    return tbl


def _flat_rows(db: Session, period: LedgerPeriod | None) -> list[list]:
    flats = db.scalars(select(Flat).order_by(Flat.number)).all()
    header = ["Flat", "Owner", "Maint.\npaid", "Maint.\npending", "Corpus\npaid", "Corpus\npending", "Projects\npaid", "Projects\npending"]
    rows = [header]
    totals = [0] * 6
    for flat in flats:
        mq = select(MaintenancePayment).where(MaintenancePayment.flat_id == flat.id)
        cq = select(CorpusPayment).where(CorpusPayment.flat_id == flat.id)
        if period:
            mq = mq.where(MaintenancePayment.period_id == period.id)
            cq = cq.where(CorpusPayment.period_id == period.id)
        maint = db.scalars(mq).all()
        corpus = db.scalars(cq).all()
        projects = db.scalars(
            select(ProjectPayment)
            .join(Project)
            .where(ProjectPayment.flat_id == flat.id, Project.deleted_at.is_(None))
        ).all()
        vals = [
            sum(m.amount for m in maint if m.is_paid),
            sum(m.amount for m in maint if not m.is_paid),
            sum(c.amount for c in corpus if c.is_paid),
            campaigns_svc.remaining_total_for_flat(db, flat.id),
            sum(min(p.paid, p.amount) for p in projects),
            sum(p.remaining for p in projects),
        ]
        totals = [a + b for a, b in zip(totals, vals)]
        owner = flat.contact_name or "—"
        if flat.owner_notes:
            owner += f" ({flat.owner_notes})"
        rows.append([flat.number, owner] + [rs(v) for v in vals])
    rows.append(["Total", ""] + [rs(v) for v in totals])
    return rows


def build_summary_pdf(db: Session, period: LedgerPeriod | None) -> bytes:
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("Cell", parent=styles["BodyText"], fontSize=8.5, leading=10))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
        title=f"{PORTAL_NAME} — summary",
    )
    periods = db.scalars(select(LedgerPeriod).order_by(LedgerPeriod.year, LedgerPeriod.month)).all()
    if period:
        scope = ledger_svc.month_label(period)
    elif periods:
        scope = f"All time ({ledger_svc.month_label(periods[0])} – {ledger_svc.month_label(periods[-1])})"
    else:
        scope = "All time"

    story = [
        Paragraph(f"{PORTAL_NAME} — Summary report", styles["Title"]),
        Paragraph(f"<b>{scope}</b> · generated {format_ist_stamp()}", styles["Normal"]),
        Spacer(1, 6 * mm),
        Paragraph("Balances", styles["Heading2"]),
    ]
    if period:
        s = ledger_svc.period_fund_summary(db, period)
        balance_rows = [
            ["Item", "Amount"],
            ["Opening Account Balance", rs(s["opening"])],
            ["Maintenance collections", rs(s["collections"])],
            ["Paid from Account Balance", rs(s["expenses"])],
            ["Closing Account Balance", rs(s["closing"])],
            ["Month status", "Closed" if period.is_closed else "Open"],
        ]
    else:
        unsettled = sum(e.amount for e in ledger_svc.unsettled_expenses(db))
        balance_rows = [
            ["Item", "Amount"],
            ["Maintenance collected (all months)", rs(ledger_svc.maintenance_collected(db))],
            ["Paid from Account Balance (all months)", rs(ledger_svc.settlements_from_fund(db))],
            ["Current Account Balance", rs(ledger_svc.compute_fund_balance(db))],
            ["Owner advances awaiting settlement", rs(unsettled)],
        ]
    story += [_table(balance_rows, [110 * mm, 50 * mm]), Spacer(1, 5 * mm)]

    story.append(Paragraph("Payments by flat", styles["Heading2"]))
    note = "Maintenance and corpus paid in the selected month; corpus pending is what each flat still owes on open collections; projects show all active project shares." if period else "All months; corpus pending is what each flat still owes on open collections; projects show all active project shares."
    story.append(Paragraph(note, styles["Italic"]))
    story += [_table(_flat_rows(db, period), [12 * mm, 40 * mm] + [21.5 * mm] * 6, total_row=True), Spacer(1, 5 * mm)]

    eq = (
        select(Expense)
        .where(Expense.counted())
        .options(selectinload(Expense.paid_by_flat))
        .order_by(Expense.expense_date, Expense.id)
    )
    if period:
        eq = eq.where(Expense.period_id == period.id)
    expenses = db.scalars(eq).all()
    story.append(Paragraph("Expenses", styles["Heading2"]))
    exp_rows = [["Date", "Category", "Description", "Amount", "Paid by", "Settled"]]
    for e in expenses:
        exp_rows.append([
            ddmmyyyy(e.expense_date),
            Paragraph(escape(e.category), styles["Cell"]),
            Paragraph(escape(e.description), styles["Cell"]),
            rs(e.amount),
            source_svc.label(e),
            "Yes" if e.is_settled else "No",
        ])
    exp_rows.append(["Total", "", "", rs(sum(e.amount for e in expenses)), "", ""])
    story += [_table(exp_rows, [20 * mm, 28 * mm, 58 * mm, 24 * mm, 30 * mm, 16 * mm], total_row=True), Spacer(1, 5 * mm)]

    sq = (
        select(Settlement)
        .join(Expense, Settlement.expense_id == Expense.id)
        .where(Settlement.reverted_at.is_(None), Expense.counted())
        .options(selectinload(Settlement.expense))
        .order_by(Settlement.settled_on, Settlement.id)
    )
    if period:
        sq = sq.where(Expense.period_id == period.id)
    settlements = db.scalars(sq).all()
    story.append(Paragraph("Settlements (paid from Account Balance)", styles["Heading2"]))
    st_rows = [["Settled on", "Expense", "Amount", "Note"]]
    for st in settlements:
        st_rows.append([
            ddmmyyyy(st.settled_on),
            Paragraph(escape(st.expense.description), styles["Cell"]),
            rs(st.amount),
            Paragraph(escape(st.note or "—"), styles["Cell"]),
        ])
    st_rows.append(["Total", "", rs(sum(s.amount for s in settlements)), ""])
    story.append(_table(st_rows, [22 * mm, 70 * mm, 26 * mm, 58 * mm], total_row=True))

    doc.build(story)
    return buf.getvalue()


def _with_payee(text: str | None, row) -> str:
    """Append the UPI ID the payment was made to, as recorded on the payment (never the live setting)."""
    vpa = getattr(row, "payee_vpa", None) if row is not None else None
    if not vpa:
        return text or ""
    return f"{text} · paid to {vpa}" if text else f"paid to {vpa}"


def build_flat_statement_pdf(db: Session, flats: list[Flat], period: LedgerPeriod | None) -> bytes:
    """Owner statement for one or more flats: maintenance, corpus, projects and expense advances."""
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("Cell", parent=styles["BodyText"], fontSize=8.5, leading=10))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
        title=f"{PORTAL_NAME} — flat statement",
    )
    scope = ledger_svc.month_label(period) if period else "All time"
    story = [
        Paragraph(f"{escape(PORTAL_NAME)} — Flat statement", styles["Title"]),
        Paragraph(f"<b>{escape(scope)}</b> · generated {format_ist_stamp()}", styles["Normal"]),
        Spacer(1, 5 * mm),
    ]

    def cell(text):
        return Paragraph(escape(text or "—"), styles["Cell"])

    for flat in flats:
        owner = flat.contact_name or "—"
        story.append(Paragraph(f"Flat {escape(flat.number)} — {escape(owner)}", styles["Heading2"]))

        mq = (select(MaintenancePayment).join(LedgerPeriod, MaintenancePayment.period_id == LedgerPeriod.id)
              .where(MaintenancePayment.flat_id == flat.id).order_by(LedgerPeriod.year, LedgerPeriod.month))
        if period:
            mq = mq.where(MaintenancePayment.period_id == period.id)
        maint = db.scalars(mq.options(selectinload(MaintenancePayment.period))).all()
        rows = [["Month", "Fee", "Status", "Paid on", "Note"]]
        for m in maint:
            rows.append([ledger_svc.month_label(m.period), rs(m.amount), "Paid" if m.is_paid else "Pending",
                         ddmmyyyy(m.paid_on) if m.paid_on else "—", cell(_with_payee(m.payment_note, m if m.is_paid else None))])
        paid = sum(m.amount for m in maint if m.is_paid)
        pending = sum(m.amount for m in maint if not m.is_paid)
        rows.append(["Total", rs(paid + pending), f"Paid {rs(paid)}", f"Pending {rs(pending)}", ""])
        story += [Paragraph("Maintenance", styles["Heading3"]),
                  _table(rows, [34 * mm, 24 * mm, 30 * mm, 30 * mm, 62 * mm], total_row=True)]

        totals = campaigns_svc.flat_corpus_totals(db, flat.id)
        rows = [["Corpus goal", "Share", "Paid", "Remaining", "Due"]]
        for r in totals["rows"]:
            rows.append([cell(r["campaign"].name), rs(r["share"]), rs(r["paid"]), rs(r["remaining"]),
                         ddmmyyyy(r["due_date"]) if r["due_date"] else "—"])
        rows.append(["Total (open goals)", rs(totals["share"]), rs(totals["paid"]), rs(totals["remaining"]), ""])
        story += [Paragraph("Corpus", styles["Heading3"]),
                  _table(rows, [64 * mm, 28 * mm, 28 * mm, 28 * mm, 32 * mm], total_row=True)]

        cq = select(CorpusPayment).where(CorpusPayment.flat_id == flat.id, CorpusPayment.is_paid.is_(True))
        if period:
            cq = cq.where(CorpusPayment.period_id == period.id)
        cps = db.scalars(cq.order_by(CorpusPayment.paid_on)).all()
        if cps:
            rows = [["Paid on", "Goal", "Amount", "Mode / note"]]
            for p in cps:
                goal = p.campaign.name if p.campaign else (p.description or "Corpus")
                if p.line_item:
                    goal += f" › {p.line_item.title}"
                rows.append([ddmmyyyy(p.paid_on) if p.paid_on else "—", cell(goal), rs(p.amount),
                             cell(_with_payee(" · ".join(x for x in (p.payment_mode, p.payment_note) if x), p))])
            rows.append(["Total", "", rs(sum(p.amount for p in cps)), ""])
            story += [Paragraph(f"Corpus payments ({escape(scope)})", styles["Heading3"]),
                      _table(rows, [24 * mm, 80 * mm, 26 * mm, 50 * mm], total_row=True)]

        pps = db.scalars(
            select(ProjectPayment).join(Project)
            .where(ProjectPayment.flat_id == flat.id, Project.deleted_at.is_(None))
            .options(selectinload(ProjectPayment.project))
        ).all()
        if pps:
            labels = {"paid": "Paid", "part": "Part paid", "pending": "Pending"}
            rows = [["Project", "Share", "Paid", "Status", "Last paid"]]
            for p in pps:
                rows.append([cell(p.project.name), rs(p.amount), rs(p.paid), labels[p.status],
                             ddmmyyyy(p.paid_on) if p.paid_on else "—"])
            rows.append(["Total", rs(sum(p.amount for p in pps)), rs(sum(p.paid for p in pps)),
                         f"Pending {rs(sum(p.remaining for p in pps))}", ""])
            story += [Paragraph("Projects", styles["Heading3"]),
                      _table(rows, [64 * mm, 26 * mm, 26 * mm, 36 * mm, 28 * mm], total_row=True)]

        eq = select(Expense).where(Expense.paid_by_flat_id == flat.id, Expense.counted())
        if period:
            eq = eq.where(Expense.period_id == period.id)
        exps = db.scalars(eq.order_by(Expense.expense_date)).all()
        if exps:
            rows = [["Date", "Description", "Amount", "Reimbursed"]]
            for e in exps:
                rows.append([ddmmyyyy(e.expense_date), cell(e.description), rs(e.amount), "Yes" if e.is_settled else "No"])
            rows.append(["Total", "", rs(sum(e.amount for e in exps)), ""])
            story += [Paragraph("Common expenses paid by this flat (advances)", styles["Heading3"]),
                      _table(rows, [24 * mm, 96 * mm, 30 * mm, 30 * mm], total_row=True)]
        story.append(Spacer(1, 6 * mm))

    doc.build(story)
    return buf.getvalue()
