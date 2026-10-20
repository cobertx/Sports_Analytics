"""Build implementation manuals from the model READMEs, without network access.

Install requirements/requirements-docs.txt, then run: python scripts/build_model_docs.py
Final PDFs are published in output/pdf and alongside their model source files.
"""

from hashlib import sha256
from html import escape
from pathlib import Path
import re
import shutil
import textwrap

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, KeepTogether, PageBreak, PageTemplate,
    Paragraph, Preformatted, Spacer, Table, TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/pdf"
INK = colors.HexColor("#142B40")
TEAL = colors.HexColor("#007F83")
MUTED = colors.HexColor("#526574")
PALE = colors.HexColor("#F0F5F7")
WIDTH, HEIGHT = A4
MARGIN = 47
CONTENT = WIDTH - 2 * MARGIN


def ascii_text(value):
    return (value.replace("\u2013", "-").replace("\u2014", "-")
            .replace("\u2011", "-").replace("\u2019", "'")
            .replace("\u2018", "'").replace("\u201c", '"')
            .replace("\u201d", '"').replace("\u00a0", " "))


def inline(value):
    """Support the small Markdown subset used in the model documentation."""
    value = ascii_text(value)
    value = re.sub(r"\[([^]]+)\]\(([^)]+)\)", r"\1", value)
    # Protect code spans before processing emphasis.
    spans = []
    def code(match):
        spans.append('<font name="Courier" size="8.2" color="#235260">'
                     + escape(match.group(1)) + '</font>')
        return f"CODETOKEN{len(spans)-1}END"
    value = re.sub(r"`([^`]+)`", code, value)
    value = escape(value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", value)
    value = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", value)
    for index, span in enumerate(spans):
        value = value.replace(f"CODETOKEN{index}END", span)
    return value


def styles():
    base = getSampleStyleSheet()
    common = dict(fontName="Helvetica", textColor=INK, splitLongWords=True)
    return {
        "body": ParagraphStyle("Body", fontSize=9.5, leading=14, spaceAfter=8, **common),
        "h1": ParagraphStyle("Section", fontSize=15, leading=19, spaceBefore=16,
                             spaceAfter=9, keepWithNext=True, **common),
        "h2": ParagraphStyle("Subsection", fontSize=11.2, leading=15, spaceBefore=12,
                             spaceAfter=6, keepWithNext=True, **common),
        "h3": ParagraphStyle("Minor", fontSize=10, leading=14, spaceBefore=10,
                             spaceAfter=5, keepWithNext=True, **common),
        "bullet": ParagraphStyle("BulletBody", fontSize=9.5, leading=14, leftIndent=12,
                                 firstLineIndent=-10, spaceAfter=5, **common),
        "cell": ParagraphStyle("Cell", fontSize=8.3, leading=11.5, spaceAfter=0, **common),
        "headcell": ParagraphStyle("HeadCell", fontName="Helvetica-Bold", fontSize=8.1,
                                   leading=11.2, textColor=colors.white),
        "code": ParagraphStyle("Code", fontName="Courier", fontSize=7.4, leading=10.8,
                               textColor=INK, backColor=PALE, borderPadding=9,
                               spaceBefore=4, spaceAfter=12),
        "cover": ParagraphStyle("Cover", fontName="Helvetica-Bold", fontSize=36,
                                leading=40, textColor=INK, spaceAfter=16),
        "subtitle": ParagraphStyle("Subtitle", fontName="Helvetica", fontSize=16,
                                   leading=22, textColor=MUTED, spaceAfter=24),
        "kicker": ParagraphStyle("Kicker", fontName="Helvetica-Bold", fontSize=10,
                                 leading=15, textColor=TEAL, spaceAfter=14),
        "small": ParagraphStyle("Small", parent=base["Normal"], fontSize=8.1,
                                leading=12, textColor=MUTED, spaceAfter=9),
    }


class Guide(BaseDocTemplate):
    def __init__(self, path, title, label):
        super().__init__(str(path), pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
                         topMargin=51, bottomMargin=48, title=title, author="RX Sports Analytics",
                         subject="Code implementation, lookback, season transitions and market probabilities")
        self.label = label
        self.addPageTemplates(PageTemplate(id="body", frames=[Frame(
            MARGIN, 48, CONTENT, HEIGHT-99, leftPadding=0, rightPadding=0,
            topPadding=0, bottomPadding=0)], onPage=self.decorate))
        self.heading_index = 0

    def beforeDocument(self):
        self.heading_index = 0

    def decorate(self, canvas, doc):
        canvas.saveState()
        canvas.setFillColor(TEAL)
        canvas.rect(0, HEIGHT-8, WIDTH, 8, fill=True, stroke=False)
        if doc.page > 1:
            canvas.setFont("Helvetica-Bold", 8)
            canvas.setFillColor(MUTED)
            canvas.drawString(MARGIN, HEIGHT-30, "RX  /  " + self.label)
            canvas.setStrokeColor(colors.HexColor("#D8E3E8"))
            canvas.line(MARGIN, 36, WIDTH-MARGIN, 36)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, 23, "Implementation guide  |  Code reviewed 20 October 2026")
        canvas.drawRightString(WIDTH-MARGIN, 23, str(doc.page))
        canvas.restoreState()

    def afterFlowable(self, flowable):
        level = getattr(flowable, "toc_level", None)
        if level is not None:
            key = f"section-{self.heading_index}"
            self.heading_index += 1
            self.canv.bookmarkPage(key)
            title = flowable.getPlainText()
            self.canv.addOutlineEntry(title, key, level=level, closed=False)
            self.notify("TOCEntry", (level, title, self.page, key))


def markdown_table(lines, sheet):
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in lines]
    rows = [row for row in rows if not all(re.fullmatch(r":?-+:?", cell) for cell in row)]
    count = len(rows[0])
    if count == 2:
        fractions = [0.33, 0.67]
    elif count == 3:
        fractions = [0.28, 0.36, 0.36]
    else:
        fractions = [1/count] * count
    data = [[Paragraph(inline(cell), sheet["headcell" if index == 0 else "cell"])
             for cell in row] for index, row in enumerate(rows)]
    table = Table(data, colWidths=[CONTENT*f for f in fractions], repeatRows=1,
                  hAlign="LEFT", splitByRow=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 0), (-1, 0), .7, TEAL),
        ("LINEBELOW", (0, -1), (-1, -1), .4, colors.HexColor("#CAD7DD")),
    ]))
    return [table, Spacer(1, 11)]


def manuscript(markdown, sheet):
    lines = markdown.splitlines()
    story, index = [], 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line.startswith("# "):
            index += 1
            continue
        if line.startswith("```"):
            code = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                original = ascii_text(lines[index]).expandtabs(4)
                code.extend(textwrap.wrap(original, width=103, subsequent_indent="    ",
                                          replace_whitespace=False, drop_whitespace=False,
                                          break_long_words=True, break_on_hyphens=False) or [""])
                index += 1
            story.append(KeepTogether([Preformatted("\n".join(code), sheet["code"])]))
            index += 1
            continue
        if line.startswith("|"):
            table_lines = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                table_lines.append(lines[index].strip())
                index += 1
            story.extend(markdown_table(table_lines, sheet))
            continue
        heading = re.match(r"^(#{2,4}) (.*)", line)
        if heading:
            depth = len(heading.group(1))-2
            node = Paragraph(inline(heading.group(2)), sheet[f"h{depth+1}"])
            if depth < 2:
                node.toc_level = depth
            story.append(node)
            index += 1
            continue
        bullet = re.match(r"^(?:- |(\d+)\. )(.*)", line)
        paragraph = [bullet.group(2) if bullet else line]
        index += 1
        while index < len(lines):
            following = lines[index].strip()
            if (not following or following.startswith(("#", "|", "```", "- "))
                    or re.match(r"^\d+\. ", following)):
                break
            paragraph.append(following)
            index += 1
        text = inline(" ".join(paragraph))
        if bullet:
            prefix = f"{bullet.group(1)}. " if bullet.group(1) else "- "
            story.append(Paragraph(prefix+text, sheet["bullet"]))
        else:
            story.append(Paragraph(text, sheet["body"]))
    # Keep the short closing validation/limitations section intact, avoiding a
    # two-line continuation on a page otherwise devoted to build provenance.
    last_section = max(i for i, item in enumerate(story) if getattr(item, "toc_level", None) == 0)
    return story[:last_section] + [KeepTogether(story[last_section:])]


def build(method):
    directory = (ROOT / "models" / "Player_Stats" / {"WHP": "Weighted_Historical_Performance", "UO": "Usage_Opportunity", "OAM": "Opponent_Adjusted_Matchup", "PGE": "Pace_Game_Environment"}[method] if method in {"WHP", "UO", "OAM", "PGE"}
                 else ROOT / "models" / "Game_Outcome" / method)
    source = directory / "README.md"
    markdown = source.read_text(encoding="utf-8")
    sheet = styles()
    destination = OUT / f"{method}_Implementation_Guide.pdf"
    document = Guide(destination, f"{method} implementation guide", f"{method} MODEL IMPLEMENTATION")
    story = [Spacer(1, 55), Paragraph("RX / SPORTS ANALYTICS", sheet["kicker"]),
             Paragraph(f"{method}<br/>Implementation Guide", sheet["cover"]),
             Paragraph("NBA and NFL player environment models<br/>From shared pace and game script to prop probabilities" if method == "PGE" else
                       "NBA and NFL opponent-adjusted player models<br/>From neutral history to matchup probabilities" if method == "OAM" else
                       "NBA and NFL player opportunity models<br/>From source observations to market probabilities" if method == "UO" else
                       "NBA, NFL and NCAAF player models<br/>From source observations to market probabilities" if method == "WHP" else
                       "NFL and NBA models<br/>From source observations to market probabilities", sheet["subtitle"]),
             HRFlowable(width=CONTENT, thickness=2, color=TEAL), Spacer(1, 22),
             Paragraph("LOOKBACK / NEW SEASONS / CODE LOGIC / PRICING", sheet["kicker"]),
             Paragraph("A guide to the implementation in this repository, including the exact history windows, minimum samples, seasonal carryover, chronological calibration, saved forecast behavior and limitations.", sheet["body"]),
             Spacer(1, 22),
             Paragraph("Read section 0 first for the answers to how many games are used and what happens when a new season has little or no history.", sheet["body"]),
             Paragraph("The original research PDF is retained separately as a source reference. This manual explains what the code actually implements; advanced proposals in the source papers are not represented as completed features.", sheet["small"]),
             Spacer(1, 18), Paragraph("Code review date: 20 October 2026<br/>Documentation source: "+source.relative_to(ROOT).as_posix(), sheet["small"]),
             PageBreak(), Paragraph("Contents", sheet["h1"])]
    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle("TOC0", fontName="Helvetica-Bold", fontSize=9, leading=12,
                       spaceBefore=6, textColor=INK, leftIndent=0, firstLineIndent=0),
        ParagraphStyle("TOC1", fontName="Helvetica", fontSize=8.2, leading=10.5,
                       spaceBefore=2, textColor=MUTED, leftIndent=13, firstLineIndent=0),
    ]
    story.extend([toc, PageBreak()])
    story.extend(manuscript(markdown, sheet))
    story.append(PageBreak() if method in {"OAM", "PGE"} else Spacer(1, 18))
    story.append(Paragraph("Build provenance", sheet["h2"]))
    source_files = {"WHP": [p.relative_to(ROOT).as_posix() for p in sorted(directory.glob("*.py"))],
                    "UO": [p.relative_to(ROOT).as_posix() for p in sorted(directory.glob("*.py"))] + ["sports_analytics/usage_opportunity_service.py"],
                    "OAM": [p.relative_to(ROOT).as_posix() for p in sorted(directory.glob("*.py"))] + ["sports_analytics/opponent_adjusted_service.py"] +
                           ["models/Player_Stats/Usage_Opportunity/" + name for name in
                            ["nba_model.py", "nfl_model.py", "opportunity_model.py", "efficiency_model.py", "distributions.py", "historical_data.py"]],
                    "PGE": [p.relative_to(ROOT).as_posix() for p in sorted(directory.glob("*.py"))] + ["sports_analytics/pace_game_environment_service.py"] +
                           ["models/Player_Stats/Usage_Opportunity/" + name for name in
                            ["nba_model.py", "nfl_model.py", "opportunity_model.py", "efficiency_model.py", "distributions.py", "historical_data.py"]],
                    "PPP": ["models/Game_Outcome/PPP/PPP.py", "models/Game_Outcome/PPP/NBA_PPP.py", "sports_analytics/ppp_service.py",
                     "sports_analytics/nba_ppp_service.py", "sports_analytics/nfl_model_data.py",
                     "sports_analytics/nba_model_data.py"],
                    "EPA": ["models/Game_Outcome/EPA/EPA.py", "models/Game_Outcome/EPA/NBA_EPA.py", "models/Game_Outcome/EPA/nba_epa_training.py",
                     "models/Game_Outcome/EPA/nba_epa_data.py", "sports_analytics/epa_service.py",
                     "sports_analytics/nba_epa_service.py", "sports_analytics/ppp_service.py"],
                    "ELO": ["models/Game_Outcome/ELO/ELO.py", "sports_analytics/elo_service.py",
                            "sports_analytics/ppp_service.py", "sports_analytics/nfl_model_data.py",
                            "sports_analytics/nba_model_data.py"]}[method]
    story.append(Paragraph("The following SHA-256 hashes identify the reviewed calculation/service files. They are documentation provenance, not a runtime cache identity.", sheet["small"]))
    provenance_style = (ParagraphStyle("UOProvenance", parent=sheet["small"], leading=10, spaceAfter=6)
                        if method in {"UO", "OAM", "PGE"} else sheet["small"])
    for name in [source.relative_to(ROOT).as_posix(), *source_files]:
        digest = sha256((ROOT/name).read_bytes()).hexdigest()
        story.append(Paragraph(escape(name) + '<br/><font name="Courier" size="7.4">'
                               + digest + '</font>', provenance_style))
    document.multiBuild(story)
    model_copy = directory / destination.name
    shutil.copyfile(destination, model_copy)
    print(f"Built {model_copy.relative_to(ROOT)} ({destination.stat().st_size:,} bytes)")


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--methods", nargs="+", choices=["PPP", "EPA", "ELO", "WHP", "UO", "OAM", "PGE"], default=["PPP", "EPA", "ELO", "WHP", "UO", "OAM", "PGE"])
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    for method in dict.fromkeys(args.methods):
        build(method)


if __name__ == "__main__":
    main()
