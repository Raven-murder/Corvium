from io import BytesIO
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

with patch("dotenv.load_dotenv"):
    from src import db, ingest, ocr, workspaces


def response(content=None, finish_reason="stop", choices=True):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)] if choices else [])


class OCRRobustnessTests(unittest.TestCase):
    def setUp(self):
        output = BytesIO()
        with Image.new("RGB", (20, 20), "white") as image:
            image.save(output, format="PNG")
        self.image = output.getvalue()

    def test_empty_or_truncated_response_retries_once_with_double_budget(self):
        for first in (response(None), response(" "), response("Partial transcription", "length"), response(choices=False)):
            with self.subTest(first=first):
                with patch.object(ocr, "get_client") as client:
                    create = client.return_value.chat.completions.create
                    create.side_effect = [first, response("Complete transcription")]
                    self.assertEqual(ocr.transcribe_image(self.image, "vision-model"), "Complete transcription")
                self.assertEqual(create.call_count, 2)
                first_call, second_call = create.call_args_list
                self.assertGreaterEqual(first_call.kwargs["max_tokens"], 4000)
                self.assertEqual(second_call.kwargs["max_tokens"], first_call.kwargs["max_tokens"] * 2)
                self.assertEqual(first_call.kwargs["messages"], second_call.kwargs["messages"])
                self.assertEqual(first_call.kwargs["model"], second_call.kwargs["model"])

    def test_second_empty_response_returns_empty_without_raising(self):
        for second in (response(None, "length"), response(" "), response(choices=False), SimpleNamespace()):
            with self.subTest(second=second):
                with patch.object(ocr, "get_client") as client:
                    create = client.return_value.chat.completions.create
                    create.side_effect = [response(None, "length"), second]
                    self.assertEqual(ocr.transcribe_image(self.image, "vision-model"), "")
                    self.assertEqual(create.call_count, 2)

    def test_second_truncated_response_does_not_trigger_a_third_request(self):
        with patch.object(ocr, "get_client") as client:
            create = client.return_value.chat.completions.create
            create.side_effect = [response("Partial", "length"), response("More transcription", "length")]
            self.assertEqual(ocr.transcribe_image(self.image, "vision-model"), "More transcription")
            self.assertEqual(create.call_count, 2)

    def test_complete_first_response_does_not_retry(self):
        with patch.object(ocr, "get_client") as client:
            create = client.return_value.chat.completions.create
            create.return_value = response("Complete transcription")
            self.assertEqual(ocr.transcribe_image(self.image, "vision-model"), "Complete transcription")
            create.assert_called_once()

    def test_empty_retry_is_saved_as_no_text_found(self):
        with tempfile.TemporaryDirectory() as temp, workspaces.storage_context(temp):
            path = Path(temp) / "scan.png"
            path.write_bytes(self.image)
            with patch.object(ingest, "get_vision_model", return_value="vision-model"), patch.object(ocr, "get_vision_model", return_value="vision-model"), patch.object(ocr, "get_client") as client, patch.object(ingest, "embed_texts") as embed:
                client.return_value.chat.completions.create.side_effect = [response(None, "length"), response(None, "length")]
                result = ingest.ingest_files([path])[0]
            self.assertIsNone(result["error"])
            self.assertEqual(result["status"], "no text found")
            self.assertEqual(result["count"], 0)
            embed.assert_not_called()
            with db.database_session() as connection:
                document = connection.execute("SELECT * FROM documents").fetchone()
                self.assertEqual(document["status"], "no text found")
                self.assertEqual((Path(temp) / document["stored_path"]).read_bytes(), self.image)


if __name__ == "__main__":
    unittest.main()
