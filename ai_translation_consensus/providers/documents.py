# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Documents (images, PDF files) for multimodal providers. Odoo independent.

A file is ``{"mimetype", "data_b64", "name"}``. PDF pages are turned into PNG images with
``pdftoppm`` (poppler-utils) for services that only take images; nothing else is needed.
"""
import base64
import binascii
import os
import shutil
import subprocess
import tempfile

from .http import ProviderError

IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
PDF_TYPE = "application/pdf"
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
DOCUMENT_TYPES = IMAGE_TYPES + (PDF_TYPE,)
MAX_FILES = 5
PDF_PAGES = 5            # pages of a PDF sent as images
PDF_DPI = 110
PDF_TIMEOUT = 60


def check_files(files):
    """The files as a clean list; raises ProviderError for anything that is not a document."""
    if not files:
        raise ProviderError("no document given")
    if len(files) > MAX_FILES:
        raise ProviderError("at most %d documents per request" % MAX_FILES)
    clean = []
    for item in files:
        mimetype = (item.get("mimetype") or "").lower()
        if mimetype not in DOCUMENT_TYPES:
            raise ProviderError("unsupported document type %r" % mimetype)
        data = item.get("data_b64")
        if not data or not isinstance(data, str):
            raise ProviderError("document without content")
        clean.append({"mimetype": mimetype, "data_b64": data, "name": str(item.get("name") or "")[:120]})
    return clean


def pdf_to_png(data_b64, pages=PDF_PAGES, dpi=PDF_DPI):
    """List of base64 PNG pages (first ``pages`` pages) of a base64 PDF, rendered by ``pdftoppm``."""
    exe = shutil.which("pdftoppm")
    if not exe:
        raise ProviderError("this service takes no PDF files and pdftoppm (poppler-utils) is not "
                            "installed to turn the pages into images")
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        raise ProviderError("the PDF is not valid base64")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.pdf")
        with open(src, "wb") as handle:
            handle.write(raw)
        try:
            proc = subprocess.run([exe, "-png", "-r", str(dpi), "-f", "1", "-l", str(pages), src,
                                   os.path.join(tmp, "page")],
                                  capture_output=True, timeout=PDF_TIMEOUT, check=False, cwd=tmp)
        except subprocess.TimeoutExpired:
            raise ProviderError("turning the PDF into images timed out")
        if proc.returncode != 0:
            raise ProviderError("pdftoppm failed: %s" % proc.stderr.decode("utf-8", "replace")[:150])
        names = sorted(n for n in os.listdir(tmp) if n.startswith("page") and n.endswith(".png"))
        if not names:
            raise ProviderError("the PDF has no pages")
        out = []
        for name in names[:pages]:
            with open(os.path.join(tmp, name), "rb") as handle:
                out.append(base64.b64encode(handle.read()).decode())
        return out
