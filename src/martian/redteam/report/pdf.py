"""A portable review of one saved scan and its recommended fixes."""

from __future__ import annotations

from html import escape
from io import BytesIO
from typing import TYPE_CHECKING

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    CondPageBreak,
    Flowable,
    LongTable,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    TableStyle,
)

from martian._privacy import safe_text
from martian.redteam.report import build_report
from martian.redteam.report.remediation import build_remediation

if TYPE_CHECKING:
    from reportlab.pdfgen.canvas import Canvas

    from martian.redteam.model import RedteamRun

PDF_NAME = "redteam-report.pdf"

_MARGIN = 42
_INK = HexColor("#202830")
_MUTED = HexColor("#53646D")
_ACCENT = HexColor("#1D5F63")
_LINE = HexColor("#D7E1E3")
_WASH = HexColor("#EFF5F5")

_BODY = ParagraphStyle("Body", fontName="Helvetica", fontSize=10, leading=15, textColor=_INK, spaceAfter=7)
_CAPTION = ParagraphStyle("Caption", parent=_BODY, fontSize=9, leading=13, textColor=_MUTED)
_TITLE = ParagraphStyle("Title", parent=_BODY, fontName="Helvetica-Bold", fontSize=25, leading=29, spaceAfter=9)
_SECTION = ParagraphStyle(
    "Section", parent=_BODY, fontName="Helvetica-Bold", fontSize=14, leading=19, spaceBefore=13, keepWithNext=True
)
_TABLE_SECTION = ParagraphStyle("Table section", parent=_SECTION, keepWithNext=False)
_SUBHEADING = ParagraphStyle("Subheading", parent=_BODY, fontName="Helvetica-Bold", spaceBefore=8, keepWithNext=True)
_CELL = ParagraphStyle("Cell", parent=_BODY, fontSize=9, leading=13, spaceAfter=0)
_CODE = ParagraphStyle("Example", parent=_BODY, fontName="Courier", fontSize=8, leading=12, textColor=_ACCENT)


def _plain(value: object) -> str:
    # The standard PDF faces use WinAnsi. Keep unsupported characters legible as code points.
    return safe_text(value).encode("cp1252", "backslashreplace").decode("cp1252")


def _text(value: object) -> str:
    return escape(" ".join(_plain(value).split()))


def _page(canvas: Canvas, _doc: SimpleDocTemplate) -> None:
    width, height = A4
    canvas.saveState()
    canvas.setFillColor(_ACCENT)
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(_MARGIN + 6, height - 34, "MARTIAN RED TEAM")
    canvas.setStrokeColor(_LINE)
    canvas.line(_MARGIN + 6, 44, width - _MARGIN - 6, 44)
    canvas.setFillColor(_MUTED)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(_MARGIN + 6, 30, "Scan and remediation report")
    canvas.drawRightString(width - _MARGIN - 6, 30, f"Page {canvas.getPageNumber()}")
    canvas.restoreState()


def render_scan_pdf(run: RedteamRun) -> bytes:
    """Render a finished scan with structured evidence and defensive implementation guidance."""
    if run.state in ("queued", "running"):
        raise ValueError("PDF reports are available when the scan finishes")

    summary = build_report(run)
    plan = build_remediation(run)
    outcomes = summary.outcomes
    outcome_text = (
        f"{outcomes.decided} of {outcomes.attempts} recorded attempts reached a verdict. "
        f"{outcomes.errors} with errors, {outcomes.undetermined} undetermined, {outcomes.stopped} stopped."
    )
    risk = plan.overall_risk.title() if plan.overall_risk else "Unrated"
    started = _text(run.started_at.isoformat(sep=" ", timespec="seconds"))
    finished = _text(run.finished_at.isoformat(sep=" ", timespec="seconds")) if run.finished_at else "Unrecorded"
    story: list[Flowable] = [
        Paragraph("Scan and remediation report", _TITLE),
        Paragraph(f"<b>Scan</b> {_text(run.id)}<br/><b>Project</b> {_text(run.project_id)}", _BODY),
        Paragraph(
            f"<b>{_text(run.state.title())}</b> · Started {started}<br/>Finished {finished}",
            _CAPTION,
        ),
        Paragraph("Executive summary", _SECTION),
        Paragraph(
            f"<b>{plan.confirmed}</b> confirmed &nbsp;&nbsp; <b>{plan.attempted}</b> attempted &nbsp;&nbsp; "
            f"<b>{plan.suspected}</b> suspected findings. Highest confirmed severity <b>{risk}</b>.",
            _BODY,
        ),
        Paragraph(outcome_text, _BODY),
    ]
    if plan.incomplete:
        story.append(
            Paragraph("Scan coverage is incomplete. This report reflects the evidence recorded so far.", _BODY)
        )

    if plan.findings:
        story.extend((CondPageBreak(110), Paragraph("Recorded findings", _TABLE_SECTION)))
        priorities = {action.category: action.priority for action in plan.actions}
        rows = [[Paragraph(f"<b>{heading}</b>", _CELL) for heading in ("Finding", "Severity", "Evidence", "Action")]]
        for row in plan.findings:
            details = f"<b>{_text(row.title)}</b><br/>Finding {_text(row.finding_id)}"
            if row.example_attempt_id:
                details += f"<br/>Attempt {_text(row.example_attempt_id)}"
            hits = "hit" if row.hit_attempts == 1 else "hits"
            evidence = f"{row.counted_as.title()} · Level {row.evidence_level}<br/>{row.hit_attempts} recorded {hits}"
            rows.append(
                [
                    Paragraph(details, _CELL),
                    Paragraph(_text((row.severity or "Unrated").title()), _CELL),
                    Paragraph(evidence, _CELL),
                    Paragraph(str(priorities[row.category]), _CELL),
                ]
            )
        story.append(
            LongTable(
                rows,
                colWidths=(236, 66, 124, 49),
                repeatRows=1,
                splitInRow=1,
                hAlign="LEFT",
                style=TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("BACKGROUND", (0, 0), (-1, 0), _WASH),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                        ("TOPPADDING", (0, 0), (-1, -1), 7),
                        ("LINEBELOW", (0, 0), (-1, -1), 0.45, _LINE),
                    ]
                ),
            )
        )
        story.append(Paragraph("Prioritized remediation", _SECTION))
        for action in plan.actions:
            story.append(Paragraph(f"{action.priority}. {_text(action.title)}", _SUBHEADING))
            story.append(Paragraph(f"Typical effort {_text(action.effort.title())}", _CAPTION))
            for step in action.steps:
                story.append(Paragraph(_text(step), _BODY, bulletText="•"))
            if action.example:
                story.append(Preformatted(_plain(action.example), _CODE, maxLineLength=92))
    else:
        story.append(Paragraph("Recorded findings", _SECTION))
        story.append(Paragraph("This scan recorded no findings.", _BODY))

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title="Martian scan and remediation report",
        author="Martian Security",
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=53,
        bottomMargin=58,
        pageCompression=1,
        invariant=1,
    )
    doc.build(story, onFirstPage=_page, onLaterPages=_page)
    return buffer.getvalue()
