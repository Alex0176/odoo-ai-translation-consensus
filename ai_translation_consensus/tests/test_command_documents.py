# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Documents for the local command line kind: the ``{images}`` placeholder. A dummy script stands in for the tool."""
import base64
import json
import os
import shlex
import sys
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import BaseCase, TransactionCase, tagged

from .. import providers
from ..providers import chat, documents
from ..providers.http import ProviderError

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 20).decode()
JPG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"y" * 20).decode()


def make_pdf(pages):
    kids = " ".join("%d 0 R" % (3 + i) for i in range(pages))
    body = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    body += ("2 0 obj<</Type/Pages/Kids[%s]/Count %d>>endobj\n" % (kids, pages)).encode()
    for i in range(pages):
        body += ("%d 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n" % (3 + i)).encode()
    body += ("trailer<</Root 1 0 R/Size %d>>\n%%%%EOF\n" % (3 + pages)).encode()
    return base64.b64encode(body).decode()


# prints what the tool sees: its arguments, the prompt on stdin, whether the files exist, the size of the first one
DUMMY = ("import sys, os, json; data = sys.stdin.read(); a = sys.argv[1:]; b = [x.split('=', 1)[-1] for x in a]; "
         "print(json.dumps({'argv': a, 'stdin': data, 'cwd': os.getcwd(), "
         "'exists': [os.path.isfile(x) for x in b if x.endswith(('.png', '.jpg'))], "
         "'sizes': [os.path.getsize(x) for x in b if x.endswith(('.png', '.jpg'))]}))")


def dummy(*args):
    return shlex.join([sys.executable, "-c", DUMMY] + list(args))


def file(mimetype, data, name="f"):
    return {"mimetype": mimetype, "data_b64": data, "name": name}


@tagged("standard", "at_install")
class TestCommandDocuments(BaseCase):
    def provider(self, command, **cfg):
        cfg.setdefault("allow_command", True)
        return providers.build("command", dict(name="cmd", command=command, **cfg))

    def read(self, command, files, **cfg):
        return json.loads(self.provider(command, **cfg).read_document("sys", "user", files))

    def test_capability_follows_the_command(self):
        cls = providers.LocalCommand
        self.assertTrue(cls.documents_capable({"command": "codex exec -i {images} -"}))
        self.assertFalse(cls.documents_capable({"command": "codex exec -"}))
        self.assertFalse(cls.documents_capable({}))
        self.assertFalse(cls.can_read_documents)     # the class says nothing, the command does

    def test_placeholder_becomes_separate_arguments(self):
        out = self.read(dummy("-i", "{images}", "--end"),
                        [file("image/png", PNG), file("image/jpeg", JPG)])
        paths = out["argv"][1:3]
        self.assertEqual(out["argv"][0], "-i")
        self.assertEqual(out["argv"][3], "--end")
        self.assertTrue(paths[0].endswith(".png") and paths[1].endswith(".jpg"))
        self.assertEqual(out["exists"], [True, True])
        self.assertEqual(out["sizes"], [28, 24])     # written unchanged
        self.assertTrue(all(os.path.dirname(p) == out["cwd"] for p in paths))
        self.assertIn("sys", out["stdin"])           # the prompt still goes through stdin

    def test_placeholder_inside_an_argument_is_repeated(self):
        out = self.read(dummy("--image={images}"), [file("image/png", PNG), file("image/png", PNG)])
        self.assertEqual(len(out["argv"]), 2)
        self.assertTrue(all(a.startswith("--image=/") for a in out["argv"]))

    def test_pdf_pages_become_png_and_are_limited(self):
        out = self.read(dummy("{images}"), [file("application/pdf", make_pdf(7))])
        self.assertEqual(len(out["argv"]), documents.PDF_PAGES)
        self.assertTrue(all(a.endswith(".png") for a in out["argv"]))
        self.assertEqual(out["exists"], [True] * documents.PDF_PAGES)
        out = self.read(dummy("{images}"), [file("application/pdf", make_pdf(2)), file("image/png", PNG)])
        self.assertEqual(len(out["argv"]), 3)

    def test_pdf_is_rendered_at_150_dpi(self):
        seen = []
        real = documents.pdf_to_png

        def spy(data, pages=documents.PDF_PAGES, dpi=documents.PDF_DPI):
            seen.append((pages, dpi))
            return real(data, pages=pages, dpi=dpi)
        with patch.object(documents, "pdf_to_png", spy):
            self.read(dummy("{images}"), [file("application/pdf", make_pdf(1))])
        self.assertEqual(seen, [(5, 150)])

    def test_the_temporary_directory_is_removed(self):
        out = self.read(dummy("{images}"), [file("image/png", PNG)])
        self.assertFalse(os.path.exists(out["cwd"]))
        self.assertFalse(os.path.exists(out["argv"][0]))
        # also when the tool fails
        dirs = []
        real = chat.subprocess.run

        def run(cmd, **kw):
            dirs.append(kw["cwd"])
            return real(cmd, **kw)
        with patch.object(chat.subprocess, "run", run):
            with self.assertRaises(ProviderError):
                self.provider(shlex.join([sys.executable, "-c", "raise SystemExit(2)", "{images}"])
                              ).read_document("s", "u", [file("image/png", PNG)])
        self.assertTrue(dirs)
        self.assertFalse(os.path.exists(dirs[0]))

    def test_no_shell_interpretation(self):
        marker = "/tmp/cmd_doc_t_must_not_exist"
        evil = file("image/png", PNG, name="a; touch %s; $(touch %s).png" % (marker, marker))
        self.read(dummy("{images}", "; touch %s" % marker), [evil])
        self.assertFalse(os.path.exists(marker))

    def test_text_request_leaves_the_placeholder_out(self):
        prov = self.provider(dummy("-i", "{images}", "--end"))
        out = json.loads(prov._complete("sys", "user"))
        self.assertEqual(out["argv"], ["--end"])

    def test_errors(self):
        with self.assertRaises(ProviderError) as ctx:       # no placeholder: cannot read documents
            self.provider(dummy()).read_document("s", "u", [file("image/png", PNG)])
        self.assertIn("cannot read documents", str(ctx.exception))
        with self.assertRaises(ProviderError) as ctx:       # even when called directly
            self.provider(dummy())._complete_document("s", "u", [file("image/png", PNG)])
        self.assertIn("{images}", str(ctx.exception))
        with patch.object(documents.shutil, "which", lambda n: None if n == "pdftoppm" else "/bin/sh"):
            with self.assertRaises(ProviderError) as ctx:
                self.provider(dummy("{images}")).read_document("s", "u", [file("application/pdf", make_pdf(1))])
        self.assertIn("pdftoppm", str(ctx.exception))
        with self.assertRaises(ProviderError):
            self.provider(dummy("{images}")).read_document("s", "u", [file("image/png", "%%%not-base64")])
        with self.assertRaises(ProviderError):
            self.provider(dummy("{images}")).read_document("s", "u", [file("text/plain", PNG)])
        with self.assertRaises(ProviderError) as ctx:       # the system parameter stays the precondition
            self.provider(dummy("{images}"), allow_command=False).read_document(
                "s", "u", [file("image/png", PNG)])
        self.assertIn("disabled", str(ctx.exception))


@tagged("standard", "at_install")
class TestCommandDocumentsModel(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ai.translation.provider"].search([]).write({"active": False})

    def setUp(self):
        super().setUp()
        self.Provider = self.env["ai.translation.provider"]
        self.purpose = self.env["ai.translation.purpose"].search([("code", "=", "invoice_manual")])

    def test_ability_is_computed_per_provider(self):
        plain = self.Provider.create({"name": "p", "kind": "command", "command": "codex exec -"})
        reader = self.Provider.create({"name": "r", "kind": "command", "command": "codex exec -i {images} -"})
        self.assertFalse(plain.can_read_documents)
        self.assertTrue(reader.can_read_documents)
        plain.command = "codex exec -i {images} -"
        self.assertTrue(plain.can_read_documents)
        found = self.Provider.search([("can_read_documents", "=", True)])
        self.assertIn(reader, found)
        self.assertIn(plain, found)
        plain.command = "codex exec -"
        self.assertNotIn(plain, self.Provider.search([("can_read_documents", "=", True)]))
        self.assertIn(plain, self.Provider.search([("can_read_documents", "=", False)]))
        self.assertNotIn(reader, self.Provider.search([("can_read_documents", "=", False)]))

    def test_invoice_purpose_constraint(self):
        with self.assertRaises(ValidationError):
            self.Provider.create({"name": "p", "kind": "command", "command": "codex exec -",
                                  "purpose_ids": [(6, 0, self.purpose.ids)]})
        prov = self.Provider.create({"name": "r", "kind": "command", "command": "codex exec -i {images} -",
                                     "purpose_ids": [(6, 0, self.purpose.ids)]})
        with self.assertRaises(ValidationError):            # taking the placeholder away afterwards
            prov.command = "codex exec -"

    def test_data_protection_switch_still_applies(self):
        prov = self.Provider.create({"name": "r", "kind": "command", "command": "codex exec -i {images} -",
                                     "purpose_ids": [(6, 0, self.purpose.ids)]})
        self.assertFalse(prov.is_local)
        self.assertIn("data protection", prov._documents_blocker("invoice_manual"))
        self.assertNotIn(prov, self.Provider._pick_for_documents("invoice_manual"))
        prov.allow_documents_external = True
        self.assertEqual(prov._documents_blocker("invoice_manual"), "")
        self.assertIn(prov, self.Provider._pick_for_documents("invoice_manual"))
