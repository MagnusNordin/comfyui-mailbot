"""
Tests for mail_to_comfyui.py, with ComfyUI and SMTP mocked.

    python -m unittest test_mail_to_comfyui -v
"""

import importlib
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

VOICES = ["magnus.m4a", "linda.m4a", "Åsa.wav"]  # "Åsa" stored decomposed, as from a Mac


def load_bot(kind="audio", **overrides):
    """Import the bot fresh with the example config for `kind`."""
    example = "tts.env.example" if kind == "audio" else ".env.example"
    env = {}
    with open(os.path.join(HERE, example)) as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                env[key] = value.strip()
    env.update(EMAIL_PASS="secret", ENV_FILE="/nonexistent")
    if kind == "audio":
        env["WORKFLOW_PATH"] = os.path.join(HERE, "workflows", "chatterbox_tts_api.json")
    else:
        env.update(WORKFLOW_PATH=os.path.join(HERE, "workflows", "sdxl_working_api.json"),
                   POSITIVE_NODE_ID="6", NEGATIVE_NODE_ID="7", SAVE_IMAGE_NODE_ID="19")
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=True):
        sys.modules.pop("mail_to_comfyui", None)
        return importlib.import_module("mail_to_comfyui")


class FakeComfy:
    """Stands in for requests (ComfyUI) and smtplib.SMTP_SSL."""

    def __init__(self, bot):
        self.bot, self.prompts, self.sent = bot, [], []
        out_key = bot.OUTPUT_HISTORY_KEY
        self.output = {out_key: [{"filename": "out.mp3" if bot.OUTPUT_KIND == "audio" else "out.png",
                                  "subfolder": "", "type": "output"}]}

    def post(self, url, json=None, timeout=None):
        self.prompts.append(json["prompt"])
        return self._resp({"prompt_id": "p1"})

    def get(self, url, params=None, timeout=None):
        if "/object_info/" in url:
            cls = url.rsplit("/", 1)[1]
            return self._resp({cls: {"input": {"required": {"audio": ["COMBO", {"options": VOICES}]}}}})
        if "/history/" in url:
            return self._resp({"p1": {"outputs": {self.bot.SAVE_IMAGE_NODE_ID: self.output}}})
        return self._resp(content=b"DATA")

    def _resp(self, data=None, content=b""):
        r = mock.Mock(content=content)
        r.json.return_value = data
        return r

    def smtp(self, *args, **kwargs):
        fake = self

        class Server:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def login(self, *args):
                pass

            def send_message(self, msg):
                fake.sent.append(msg)

        return Server()

    def run(self, raw):
        with mock.patch.object(self.bot.requests, "post", self.post), \
             mock.patch.object(self.bot.requests, "get", self.get), \
             mock.patch.object(self.bot.smtplib, "SMTP_SSL", self.smtp), \
             mock.patch("builtins.print"):
            self.bot.process_message(None, 1, raw)
        return self.sent[-1]


def mail(body, subject="", headers="Message-ID: <1@example.com>\r\n", sender="you@example.com"):
    return (f"From: {sender}\r\nSubject: {subject}\r\n{headers}"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}").encode()


class TextCleaning(unittest.TestCase):
    def setUp(self):
        self.bot = load_bot()

    def text(self, body):
        return self.bot.extract_prompts(body)[0]

    def test_outlook_signature_is_dropped(self):
        body = ("Nu ska vi prata om tryckluft.\r\n\r\nMed vänlig hälsning\r\n\r\nMarina\r\n"
                "IT-pedagog\r\nE-post: m@x.se<mailto:m@x.se>\r\n[cid:1c19]\r\n")
        self.assertEqual(self.text(body), "Nu ska vi prata om tryckluft.")

    def test_quoted_replies_are_dropped(self):
        self.assertEqual(self.text("Ny text.\r\n\r\n______________________________\r\nFrån: Ljud\r\nSkickat: i dag\r\n\r\ngammalt"), "Ny text.")
        self.assertEqual(self.text("Svar.\n\nDen 6 okt. 2026 skrev Nisse <n@x>:\n> gammalt"), "Svar.")
        self.assertEqual(self.text("Hej.\n-- \nMagnus"), "Hej.")

    def test_sign_off_words_inside_text_are_kept(self):
        body = "Vänliga hälsningar från oss alla, sa rektorn.\nFrån: Stockholm till Göteborg."
        self.assertEqual(self.text(body), "Vänliga hälsningar från oss alla, sa rektorn. Från: Stockholm till Göteborg.")

    def test_hard_wrapped_lines_are_joined_paragraphs_kept(self):
        body = "In Chapter 8 of Sapiens, Harari explores\nhow societies organized.\n\nSecond paragraph\nwraps too."
        self.assertEqual(self.text(body), "In Chapter 8 of Sapiens, Harari explores how societies organized.\n\nSecond paragraph wraps too.")


class SubjectParsing(unittest.TestCase):
    def parse(self, subject):
        bot = load_bot()
        fake = FakeComfy(bot)
        with mock.patch.object(bot.requests, "get", fake.get):
            return bot.parse_subject(subject)

    def test_voice_and_language_in_any_order(self):
        for subject in ("Linda engelska", "english linda", "SV: Linda English", "Re: linda, engelsk"):
            voice, language, _ = self.parse(subject)
            self.assertEqual((voice, language), ("linda.m4a", "English (en)"), subject)

    def test_language_alone_uses_default_voice(self):
        voice, language, note = self.parse("engelska")
        self.assertEqual((voice, language), (None, "English (en)"))
        self.assertIn("Voice: magnus (default)", note)

    def test_voice_alone_keeps_default_language(self):
        voice, language, note = self.parse("Linda")
        self.assertEqual((voice, language), ("linda.m4a", None))
        self.assertIn("Language: Swedish (sv) (default)", note)

    def test_decomposed_unicode_voice_matches(self):
        self.assertEqual(self.parse("Åsa")[0], "Åsa.wav")

    def test_unknown_voice_falls_back_and_lists_voices(self):
        voice, _, note = self.parse("Bertil")
        self.assertIsNone(voice)
        self.assertIn("no voice called 'Bertil'", note)
        self.assertIn("linda, magnus, åsa", note)


class Chunking(unittest.TestCase):
    ASTA = ("In Chapter 8 of Sapiens, Yuval Noah Harari explores how post-agricultural human societies organized "
            "large populations through imagined hierarchies and social discrimination. Harari argues that social "
            "divisions such as class, race, and gender are not natural or biological truths, but fictional constructs "
            "created by human imagination to enable mass cooperation.\n\nHarari explains that once a historical event "
            "establishes a hierarchy, society reinforces it through laws, cultural myths, and economic barriers. This "
            "creates a self-fulfilling vicious circle. Finally, the chapter examines gender hierarchy, noting that "
            "patriarchy has been almost universally adopted across human societies.")

    def setUp(self):
        self.bot = load_bot()

    def test_split_keeps_all_text_under_limit_at_sentence_ends(self):
        pieces = self.bot.split_text(self.ASTA, 300)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(p) <= 300 for p in pieces), [len(p) for p in pieces])
        self.assertEqual(" ".join(pieces), " ".join(self.ASTA.split()))
        self.assertTrue(all(p.endswith(".") for p in pieces))
        self.assertLess(max(map(len, pieces)) - min(map(len, pieces)), 150)  # roughly even

    def test_overlong_sentence_is_cut_between_words(self):
        pieces = self.bot.split_text("ord " * 200, 300)
        self.assertTrue(all(len(p) <= 300 for p in pieces))
        self.assertEqual(" ".join(pieces).split(), ["ord"] * 200)

    def test_short_text_is_one_piece(self):
        self.assertEqual(self.bot.split_text("Hej! Det här är ett test.", 300), ["Hej! Det här är ett test."])

    def test_workflow_joins_pieces_in_order_into_the_save_node(self):
        fake = FakeComfy(self.bot)
        fake.run(mail(self.ASTA, subject="Linda engelska"))
        wf = fake.prompts[0]
        tts = [nid for nid, n in wf.items() if n["class_type"] == "FL_ChatterboxMultilingualTTS"]
        joins = [nid for nid, n in wf.items() if n["class_type"] == "AudioConcat"]
        self.assertEqual(len(joins), len(tts) - 1)
        # Walk the chain back from the save node and read the text in order.
        def texts(ref):
            node = wf[ref[0]]
            if node["class_type"] == "AudioConcat":
                return texts(node["inputs"]["audio1"]) + texts(node["inputs"]["audio2"])
            return [node["inputs"]["text"]]
        self.assertEqual(" ".join(texts(wf["3"]["inputs"]["audio"])), " ".join(self.ASTA.split()))
        for nid in tts:
            inputs = wf[nid]["inputs"]
            self.assertEqual(inputs["language"], "English (en)")
            self.assertEqual(inputs["audio_prompt"], ["4", 0])
        self.assertEqual(wf["4"]["inputs"]["audio"], "linda.m4a")
        self.assertEqual(len({wf[nid]["inputs"]["seed"] for nid in tts}), len(tts))  # fresh seed per piece
        # Model stays loaded between pieces; the last piece unloads it as the workflow says.
        self.assertTrue(all(wf[nid]["inputs"]["keep_model_loaded"] for nid in tts[:-1]))
        self.assertFalse(wf[tts[-1]]["inputs"]["keep_model_loaded"])

    def test_short_text_workflow_is_unchanged_apart_from_inputs(self):
        fake = FakeComfy(self.bot)
        fake.run(mail("Hej! Det här är ett test."))
        wf = fake.prompts[0]
        self.assertEqual(sorted(wf), ["2", "3", "4"])
        self.assertEqual(wf["3"]["inputs"]["audio"], ["2", 0])
        self.assertEqual(wf["2"]["inputs"]["language"], "Swedish (sv)")


class Replies(unittest.TestCase):
    def test_audio_reply(self):
        bot = load_bot()
        msg = FakeComfy(bot).run(mail("Hej allihop.", subject="Linda engelska"))
        self.assertEqual(msg["Subject"], "Re: Linda engelska")
        att = next(msg.iter_attachments())
        self.assertEqual(att.get_content_type(), "audio/mpeg")
        body = msg.get_body().get_content()
        self.assertIn("Voice: linda", body)
        self.assertIn("Language: English (en)", body)
        self.assertTrue(msg["Date"])
        self.assertTrue(msg["Message-ID"].endswith("@example.com>"))  # the bot's own domain

    def test_outlook_folded_headers_and_empty_subject(self):
        bot = load_bot()
        headers = "Message-ID:\r\n <GV0P280MB@SWEP280.PROD.OUTLOOK.COM>\r\nReferences:\r\n <a@x>\r\n <b@y>\r\n"
        msg = FakeComfy(bot).run(mail("Hej.", headers=headers))
        self.assertEqual(msg["In-Reply-To"], "<GV0P280MB@SWEP280.PROD.OUTLOOK.COM>")
        self.assertEqual(msg["References"], "<a@x> <b@y>")
        self.assertEqual(msg["Subject"], "Your audio")

    def test_image_bot_unaffected(self):
        bot = load_bot("image")
        fake = FakeComfy(bot)
        msg = fake.run(mail("Prompt: a fox in snow\n"))
        wf = fake.prompts[0]
        self.assertEqual(wf["6"]["inputs"]["text"], "a fox in snow")
        self.assertNotIn("AudioConcat", {n["class_type"] for n in wf.values()})
        self.assertEqual(next(msg.iter_attachments()).get_content_type(), "image/png")


if __name__ == "__main__":
    unittest.main()
