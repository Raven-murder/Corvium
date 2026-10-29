from html import escape
from importlib.util import find_spec
from io import BytesIO
import os
from pathlib import Path
import re
from threading import RLock

from .config import APP_NAME, StorageError
from .diagrams import DiagramRenderError, diagram_outline, render_diagram_png
from . import notes

_FONT_LOCK = RLock()


class NotesExportError(RuntimeError):
    public_message = True


def pdf_available():
    return find_spec("reportlab") is not None


def filename(title, extension):
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:100] or "note"
    return stem + "." + extension


def export_txt(note):
    pieces = [note["title"], ""]
    for part in notes.preview_parts(note["body"]):
        if part["kind"] == "markdown":
            pieces.append(part["text"])
        else:
            diagram = part["diagram"]
            title = diagram["title"] if diagram else part["title"]
            outline = diagram_outline(diagram["spec"]) if diagram else "Diagram unavailable in this workspace."
            pieces.append("[Diagram: " + title + "]\n" + "\n".join("    " + line for line in outline.splitlines()))
    return "\n".join(pieces).encode("utf-8")


def export_markdown(note):
    return ("# " + note["title"] + "\n\n" + note["body"]).encode("utf-8")


def _escaped(text):
    return escape(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(text)), quote=False)


def _inline(text):
    text = _escaped(text)
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda match: "<b>" + (match[1] or match[2]) + "</b>", text)
    return re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)|(?<!_)_([^_]+)_(?!_)",
                  lambda match: "<i>" + (match[1] or match[2]) + "</i>", text)


def _font_family():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    with _FONT_LOCK:
        if "CorviumSans" in pdfmetrics.getRegisteredFontNames():
            return "CorviumSans"
        candidates = (
            (Path(os.getenv("SystemRoot", "C:/Windows")) / "Fonts", ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf")),
            (Path("/usr/share/fonts/truetype/dejavu"), ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique.ttf")),
        )
        for directory, names in candidates:
            if not all((directory / name).is_file() for name in names):
                continue
            try:
                for suffix, name in zip(("", "-Bold", "-Italic", "-BoldItalic"), names):
                    pdfmetrics.registerFont(TTFont("CorviumSans" + suffix, str(directory / name)))
                pdfmetrics.registerFontFamily("CorviumSans", normal="CorviumSans", bold="CorviumSans-Bold",
                                               italic="CorviumSans-Italic", boldItalic="CorviumSans-BoldItalic")
                return "CorviumSans"
            except Exception:
                continue
    return "Helvetica"


def _diagram_flowables(diagram, title, width, height, styles):
    from reportlab.platypus import Image, Paragraph, Spacer
    if diagram is None:
        return [Paragraph(_escaped("[Diagram unavailable: " + title + "]"), styles["Body"])]
    try:
        png = render_diagram_png(diagram["spec"])
    except DiagramRenderError:
        return [Paragraph(_escaped("[Diagram: " + diagram["title"] + "]"), styles["Body"])] + [
            Paragraph(_escaped(line).replace(" ", "&#160;"), styles["Outline"]) for line in diagram_outline(diagram["spec"]).splitlines()]
    from PIL import Image as PillowImage
    result = []
    with PillowImage.open(BytesIO(png)) as image:
        scale = width / image.width
        strip_height = max(1, int((height - 90) / scale))
        for top in range(0, image.height, strip_height):
            bottom = min(image.height, top + strip_height)
            output = BytesIO()
            with image.crop((0, top, image.width, bottom)) as strip:
                strip.save(output, format="PNG")
            result.append(Image(BytesIO(output.getvalue()), width=width, height=(bottom - top) * scale))
            result.append(Spacer(1, 8))
    return result


def _markdown_flowables(text, styles):
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, Spacer
    result, paragraph, bullets = [], [], []

    def flush():
        if paragraph:
            result.append(Paragraph(_inline(" ".join(paragraph)), styles["Body"]))
            paragraph.clear()
        if bullets:
            result.append(ListFlowable([ListItem(Paragraph(_inline(item), styles["Body"])) for item in bullets],
                                       bulletType="bullet", leftIndent=16, bulletFontName=styles["Body"].fontName, bulletFontSize=9))
            bullets.clear()

    for line in text.splitlines():
        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        bullet = re.match(r"^\s*[-*+]\s+(.+)$", line)
        if heading:
            flush()
            result.append(Paragraph(_inline(heading[2]), styles["H" + str(len(heading[1]))]))
        elif bullet:
            if paragraph:
                flush()
            bullets.append(bullet[1])
        elif not line.strip():
            flush()
            result.append(Spacer(1, 5))
        else:
            if bullets:
                flush()
            paragraph.append(line.strip())
    flush()
    return result


def export_pdf(items, all_notes=False):
    if not pdf_available():
        raise NotesExportError("PDF export is unavailable until reportlab is available.")
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    from reportlab.platypus.tableofcontents import TableOfContents

    family = _font_family()
    bold = "CorviumSans-Bold" if family == "CorviumSans" else "Helvetica-Bold"
    styles = {"Body": ParagraphStyle("NoteBody", fontName=family, fontSize=10.5, leading=15, spaceAfter=7, splitLongWords=True),
              "Title": ParagraphStyle("NoteTitle", fontName=bold, fontSize=20, leading=25, spaceAfter=16, keepWithNext=True),
              "Outline": ParagraphStyle("NoteOutline", fontName=family, fontSize=9, leading=12, leftIndent=14),
              "Cover": ParagraphStyle("NotesCover", fontName=bold, fontSize=26, leading=34, spaceAfter=24)}
    for level, size in ((1, 17), (2, 14), (3, 12)):
        styles["H" + str(level)] = ParagraphStyle("NoteH" + str(level), fontName=bold, fontSize=size, leading=size + 5,
                                                   spaceBefore=12, spaceAfter=8, keepWithNext=True)

    class NotesDocument(SimpleDocTemplate):
        def afterFlowable(self, flowable):
            if hasattr(flowable, "note_anchor"):
                self.canv.bookmarkPage(flowable.note_anchor)
                self.notify("TOCEntry", (0, flowable.note_title, self.page, flowable.note_anchor))

    output = BytesIO()
    document = NotesDocument(output, pagesize=A4, leftMargin=45, rightMargin=45, topMargin=45, bottomMargin=45,
                             title=APP_NAME + " Notes" if all_notes else items[0]["title"], author=APP_NAME)
    story = []
    if all_notes:
        story.extend([Spacer(1, 150), Paragraph(_escaped(APP_NAME + " Notes"), styles["Cover"]),
                      Paragraph(_escaped(f"{len(items)} saved notes"), styles["Body"]), PageBreak(),
                      Paragraph("Contents", styles["Title"])])
        contents = TableOfContents()
        contents.levelStyles = [ParagraphStyle("ContentsEntry", fontName=family, fontSize=11, leading=16, leftIndent=0,
                                               firstLineIndent=0, rightIndent=30, spaceBefore=5, textColor=colors.HexColor("#111827"))]
        story.extend([contents, PageBreak()])
    for index, note in enumerate(items):
        if index:
            story.append(PageBreak())
        title = Paragraph(_escaped(note["title"]), styles["Title"])
        title.note_anchor, title.note_title = "note-" + str(index), _escaped(note["title"])
        story.append(title)
        for part in notes.preview_parts(note["body"]):
            if part["kind"] == "markdown":
                story.extend(_markdown_flowables(part["text"], styles))
            else:
                story.extend(_diagram_flowables(part["diagram"], part["title"], document.width, document.height, styles))

    def footer(canvas, current):
        canvas.saveState()
        canvas.setFont(family, 9)
        canvas.drawString(current.leftMargin, 22, APP_NAME)
        canvas.drawRightString(A4[0] - current.rightMargin, 22, str(current.page))
        canvas.restoreState()

    document.multiBuild(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def download_data(note, format_name, workspace, all_notes=False):
    """Bind deferred download work to the workspace and database generation at click time."""
    from .workspaces import database_identity, workspace_context
    identity = database_identity(workspace)
    snapshot = dict(note) if note is not None else None

    def produce():
        try:
            with workspace_context(workspace.workspace_id, identity):
                if all_notes:
                    return export_pdf(notes.list_notes(), all_notes=True)
                if format_name == "txt":
                    return export_txt(snapshot)
                if format_name == "md":
                    return export_markdown(snapshot)
                return export_pdf([snapshot])
        except (StorageError, NotesExportError):
            raise
        except Exception:
            raise NotesExportError("This export could not be prepared. Please try again.") from None
    return produce
