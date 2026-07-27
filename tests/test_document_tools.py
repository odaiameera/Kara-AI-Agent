from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import document_tools


class ImageOcrTests(unittest.TestCase):
    def test_ocr_image_extracts_text_from_real_png(self) -> None:
        from PIL import Image, ImageDraw, ImageFont

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            target = root / "nct-result.png"
            image = Image.new("RGB", (1200, 220), "white")
            draw = ImageDraw.Draw(image)
            try:
                font = ImageFont.truetype("DejaVuSans.ttf", 58)
            except OSError:
                font = ImageFont.load_default(size=58)
            draw.text((30, 65), "NCT FAIL DANGEROUS TYRE 1.6 MM", fill="black", font=font)
            image.save(target)

            with patch.object(document_tools.config, "FILE_READ_ROOTS", (root,)):
                result = json.loads(document_tools.ocr_image(str(target)))

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["type"], "image_ocr")
        normalized = result["text"].upper().replace(".", "")
        self.assertIn("NCT FAIL", normalized)
        self.assertIn("DANGEROUS TYRE", normalized)
        self.assertIn("16 MM", normalized)

    def test_windows_ocr_uses_trusted_executable_and_minimal_environment(self) -> None:
        completed = MagicMock(returncode=0, stdout="safe text", stderr="")
        trusted = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
        with patch.dict("os.environ", {"KARA_TEST_SECRET": "must-not-leak"}), patch.object(
            document_tools, "_trusted_powershell_path", return_value=trusted
        ), patch.object(document_tools.subprocess, "run", return_value=completed) as run:
            text = document_tools._windows_ocr_text(Path(r"C:\Temp\scan.png"))

        self.assertEqual(text, "safe text")
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], trusted)
        self.assertNotIn("KARA_TEST_SECRET", kwargs["env"])
        self.assertEqual(kwargs["env"]["KARA_OCR_IMAGE_PATH"], r"C:\Temp\scan.png")

    def test_ocr_image_enforces_file_and_pixel_limits_before_ocr(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            target = root / "large.png"
            Image.new("RGB", (11, 11), "white").save(target)

            with patch.object(document_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                document_tools, "MAX_IMAGE_PIXELS", 100
            ), patch.object(document_tools, "_windows_ocr_text") as ocr:
                pixel_result = json.loads(document_tools.ocr_image(str(target)))
            with patch.object(document_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                document_tools, "MAX_DOCUMENT_BYTES", 1
            ), patch.object(document_tools, "_windows_ocr_text") as byte_ocr:
                byte_result = json.loads(document_tools.ocr_image(str(target)))

        self.assertFalse(pixel_result["ok"], pixel_result)
        self.assertIn("pixel", pixel_result["error"].casefold())
        ocr.assert_not_called()
        self.assertFalse(byte_result["ok"], byte_result)
        self.assertIn("too large", byte_result["error"].casefold())
        byte_ocr.assert_not_called()

    def test_resource_limit_settings_reject_invalid_values(self) -> None:
        with patch.dict("os.environ", {"KARA_TEST_LIMIT": "0"}):
            with self.assertRaises(RuntimeError):
                document_tools._positive_int_setting("KARA_TEST_LIMIT", 1, maximum=10)
        with patch.dict("os.environ", {"KARA_TEST_LIMIT": "nan"}):
            with self.assertRaises(RuntimeError):
                document_tools._positive_float_setting("KARA_TEST_LIMIT", 1, maximum=10)

    def test_ocr_image_reads_small_document_text(self) -> None:
        from PIL import Image, ImageDraw, ImageFont

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            target = root / "small-report.png"
            image = Image.new("RGB", (588, 1280), "white")
            draw = ImageDraw.Draw(image)
            try:
                font = ImageFont.truetype("DejaVuSans.ttf", 11)
            except OSError:
                font = ImageFont.load_default(size=11)
            draw.text((28, 610), "PARKING BRAKE PERFORMANCE 14 PERCENT", fill="black", font=font)
            image.save(target)

            with patch.object(document_tools.config, "FILE_READ_ROOTS", (root,)):
                result = json.loads(document_tools.ocr_image(str(target)))

        self.assertTrue(result["ok"], result)
        self.assertIn("PARKING BRAKE PERFORMANCE", result["text"].upper())


class PdfReadingTests(unittest.TestCase):
    def test_read_pdf_extracts_embedded_text_from_real_pdf(self) -> None:
        import pymupdf

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            target = root / "inspection.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "Vehicle inspection: parking brake performance 14 percent")
            document.save(target)
            document.close()

            with patch.object(document_tools.config, "FILE_READ_ROOTS", (root,)):
                result = json.loads(document_tools.read_pdf(str(target)))

        self.assertTrue(result["ok"])
        self.assertEqual(result["type"], "pdf")
        self.assertEqual(result["page_count"], 1)
        self.assertEqual(result["pages"][0]["method"], "embedded_text")
        self.assertIn("parking brake performance 14 percent", result["pages"][0]["text"])

    def test_read_pdf_ocrs_page_with_only_incidental_embedded_text(self) -> None:
        import pymupdf
        from PIL import Image, ImageDraw, ImageFont

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            image_path = root / "mixed-scan.png"
            pdf_path = root / "mixed-scan.pdf"
            image = Image.new("RGB", (1400, 300), "white")
            draw = ImageDraw.Draw(image)
            try:
                font = ImageFont.truetype("DejaVuSans.ttf", 64)
            except OSError:
                font = ImageFont.load_default(size=64)
            draw.text((35, 95), "TYRE TREAD BELOW LEGAL LIMIT", fill="black", font=font)
            image.save(image_path)
            document = pymupdf.open()
            page = document.new_page(width=700, height=150)
            page.insert_image(page.rect, filename=str(image_path))
            page.insert_text((10, 10), "1")
            document.save(pdf_path)
            document.close()

            with patch.object(document_tools.config, "FILE_READ_ROOTS", (root,)):
                result = json.loads(document_tools.read_pdf(str(pdf_path)))

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pages"][0]["method"], "ocr")
        self.assertIn("TREAD BELOW LEGAL LIMIT", result["pages"][0]["text"].upper())

    def test_read_pdf_preserves_short_embedded_text_when_ocr_is_empty(self) -> None:
        import pymupdf

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            pdf_path = root / "short-text.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "page 1")
            document.save(pdf_path)
            document.close()

            with patch.object(document_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                document_tools, "_windows_ocr_text", return_value=""
            ):
                result = json.loads(document_tools._read_pdf_local(str(pdf_path)))

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pages"][0]["text"], "page 1")
        self.assertEqual(result["pages"][0]["method"], "embedded_text")
        self.assertIn("no text", result["pages"][0]["warning"].casefold())

    def test_read_pdf_worker_ignores_inherited_pythonpath(self) -> None:
        import pymupdf

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            poison = root / "poison"
            poison.mkdir()
            (poison / "pymupdf.py").write_text(
                "raise RuntimeError('inherited PYTHONPATH reached worker')\n",
                encoding="utf-8",
            )
            pdf_path = root / "safe.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "Worker environment is isolated")
            document.save(pdf_path)
            document.close()

            with patch.object(document_tools.config, "FILE_READ_ROOTS", (root,)), patch.dict(
                "os.environ", {"PYTHONPATH": str(poison)}
            ):
                result = json.loads(document_tools.read_pdf(str(pdf_path)))

        self.assertTrue(result["ok"], result)
        self.assertIn("Worker environment is isolated", result["pages"][0]["text"])

    def test_read_pdf_does_not_offer_nonexistent_next_page_after_text_truncation(self) -> None:
        import pymupdf

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            pdf_path = root / "long-page.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            document.save(pdf_path)
            document.close()

            with patch.object(document_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                document_tools, "MAX_EXTRACTED_CHARS", 10
            ):
                result = json.loads(document_tools.read_pdf(str(pdf_path)))
                resumed = json.loads(document_tools.read_pdf(str(pdf_path), start_char=10))

        self.assertTrue(result["truncated"], result)
        self.assertEqual(result["next_page"], 1)
        self.assertEqual(result["next_char"], 10)
        self.assertTrue(result["pages"][0]["text_truncated"])
        self.assertTrue(resumed["ok"], resumed)
        self.assertTrue(resumed["pages"][0]["text"].startswith("KLMNOP"), resumed)

    def test_read_pdf_exact_character_limit_is_not_truncated(self) -> None:
        import pymupdf

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            pdf_path = root / "exact-page.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "ABCDEFGHIJ")
            document.save(pdf_path)
            document.close()

            with patch.object(document_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                document_tools, "MAX_EXTRACTED_CHARS", 10
            ):
                result = json.loads(document_tools.read_pdf(str(pdf_path)))

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["truncated"], result)
        self.assertFalse(result["pages"][0]["text_truncated"])
        self.assertIsNone(result["next_page"])
        self.assertIsNone(result["next_char"])

    def test_read_pdf_rejects_outside_root_sensitive_malformed_and_encrypted_files(self) -> None:
        import pymupdf

        with tempfile.TemporaryDirectory() as allowed_raw, tempfile.TemporaryDirectory() as outside_raw:
            allowed = Path(allowed_raw).resolve()
            outside = Path(outside_raw).resolve()
            outside_pdf = outside / "outside.pdf"
            outside_pdf.write_bytes(b"not opened")
            sensitive_pdf = allowed / "credentials.pdf"
            sensitive_pdf.write_bytes(b"not opened")
            malformed_pdf = allowed / "malformed.pdf"
            malformed_pdf.write_bytes(b"not a pdf")
            plain_pdf = allowed / "plain.pdf"
            encrypted_pdf = allowed / "encrypted.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(plain_pdf)
            document.close()
            document = pymupdf.open(plain_pdf)
            document.save(
                encrypted_pdf,
                encryption=pymupdf.PDF_ENCRYPT_AES_256,
                owner_pw="owner",
                user_pw="reader",
            )
            document.close()

            with patch.object(document_tools.config, "FILE_READ_ROOTS", (allowed,)):
                outside_result = json.loads(document_tools.read_pdf(str(outside_pdf)))
                sensitive_result = json.loads(document_tools.read_pdf(str(sensitive_pdf)))
                malformed_result = json.loads(document_tools.read_pdf(str(malformed_pdf)))
                encrypted_result = json.loads(document_tools.read_pdf(str(encrypted_pdf)))

        self.assertFalse(outside_result["ok"], outside_result)
        self.assertIn("outside", outside_result["error"].casefold())
        self.assertFalse(sensitive_result["ok"], sensitive_result)
        self.assertIn("sensitive", sensitive_result["error"].casefold())
        self.assertFalse(malformed_result["ok"], malformed_result)
        self.assertFalse(encrypted_result["ok"], encrypted_result)
        self.assertIn("password", encrypted_result["error"].casefold())

    def test_read_pdf_enforces_total_worker_timeout(self) -> None:
        import pymupdf

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            pdf_path = root / "timeout.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "A normal PDF used to exercise worker isolation")
            document.save(pdf_path)
            document.close()

            with patch.object(document_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                document_tools, "PDF_TIMEOUT_SECONDS", 0.001, create=True
            ):
                result = json.loads(document_tools.read_pdf(str(pdf_path)))

        self.assertFalse(result["ok"], result)
        self.assertIn("time limit", result["error"].casefold())

    def test_read_pdf_timeout_terminates_worker_process_tree(self) -> None:
        import pymupdf

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            pdf_path = root / "timeout-tree.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(pdf_path)
            document.close()

            process = MagicMock()
            process.pid = 1234
            process.communicate.side_effect = [
                document_tools.subprocess.TimeoutExpired("pdf-worker", 1),
                ("", ""),
            ]
            with patch.object(document_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                document_tools.subprocess, "Popen", return_value=process
            ) as popen, patch.object(
                document_tools, "_assign_windows_worker_job", return_value=None
            ), patch.object(document_tools, "_terminate_process_tree") as terminate:
                result = json.loads(document_tools.read_pdf(str(pdf_path)))

        self.assertFalse(result["ok"], result)
        terminate.assert_called_once_with(process)
        worker_args = popen.call_args.args[0]
        self.assertIn("-I", worker_args)
        self.assertNotIn("-m", worker_args)

    def test_read_pdf_refuses_oversized_ocr_render(self) -> None:
        import pymupdf

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            pdf_path = root / "large-render.pdf"
            document = pymupdf.open()
            document.new_page(width=700, height=900)
            document.save(pdf_path)
            document.close()

            with patch.object(document_tools.config, "FILE_READ_ROOTS", (root,)), patch.object(
                document_tools, "MAX_IMAGE_PIXELS", 100
            ):
                result = json.loads(document_tools.read_pdf(str(pdf_path)))

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pages"][0]["method"], "ocr_failed")
        self.assertIn("pixel limit", result["pages"][0]["warning"].casefold())

    def test_read_pdf_ocrs_image_only_page(self) -> None:
        import pymupdf
        from PIL import Image, ImageDraw, ImageFont

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            image_path = root / "scan.png"
            pdf_path = root / "scan.pdf"
            image = Image.new("RGB", (1400, 300), "white")
            draw = ImageDraw.Draw(image)
            try:
                font = ImageFont.truetype("DejaVuSans.ttf", 64)
            except OSError:
                font = ImageFont.load_default(size=64)
            draw.text((35, 95), "SCANNED PARKING BRAKE FAILURE", fill="black", font=font)
            image.save(image_path)
            document = pymupdf.open()
            page = document.new_page(width=700, height=150)
            page.insert_image(page.rect, filename=str(image_path))
            document.save(pdf_path)
            document.close()

            with patch.object(document_tools.config, "FILE_READ_ROOTS", (root,)):
                result = json.loads(document_tools.read_pdf(str(pdf_path)))

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pages"][0]["method"], "ocr")
        self.assertIn("PARKING BRAKE FAILURE", result["pages"][0]["text"].upper())


if __name__ == "__main__":
    unittest.main()
