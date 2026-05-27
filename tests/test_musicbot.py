import tempfile
import unittest
from pathlib import Path

import musicbot
import telegram_poller


class MusicBotTests(unittest.TestCase):
    def test_slugify_keeps_filename_safe(self):
        self.assertEqual(musicbot.slugify("Neon Night Drive!"), "neon-night-drive")

    def test_fallback_renderer_writes_wav(self):
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp) / "out"
            temp_dir = Path(temp) / "temp"
            musicbot.CONFIG = musicbot.Config(
                output_dir=output_dir,
                temp_dir=temp_dir,
                generator_command="",
                fallback_tone_seconds=1,
            )
            brief = musicbot.fallback_music_brief("minor lofi piano", 90, None, 1234)
            path = musicbot.choose_output_path(brief)
            musicbot.render_fallback_tone(path, brief)

            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 1000)

    def test_prompt_requires_some_input(self):
        self.assertIsNone(musicbot.first_url("make a lofi track"))
        self.assertEqual(musicbot.first_url("use https://example.com/a.wav please"), "https://example.com/a.wav")

    def test_ollama_base_url_comes_from_generate_url(self):
        old_config = musicbot.CONFIG
        try:
            musicbot.CONFIG = musicbot.Config(ollama_url="http://127.0.0.1:11434/api/generate")
            self.assertEqual(musicbot.ollama_base_url(), "http://127.0.0.1:11434")
        finally:
            musicbot.CONFIG = old_config

    def test_music_command_detection(self):
        self.assertTrue(telegram_poller.is_music_text("/music make a lofi loop"))
        self.assertTrue(telegram_poller.is_music_text("create a synthwave track"))
        self.assertFalse(telegram_poller.is_music_text("what is on my calendar"))

    def test_audio_message_is_music_source(self):
        update = {
            "message": {
                "message_id": 42,
                "chat": {"id": 123},
                "caption": "turn this into a loop",
                "voice": {"file_id": "abc"},
                "from": {"username": "tester"},
            }
        }

        old_downloader = telegram_poller.download_telegram_file
        try:
            telegram_poller.download_telegram_file = lambda token, descriptor: r"C:\tmp\clip.ogg"
            message = telegram_poller.extract_message(update, "token")
            self.assertEqual(message["intent"], "music")
            self.assertEqual(message["source_path"], r"C:\tmp\clip.ogg")
        finally:
            telegram_poller.download_telegram_file = old_downloader

    def test_normalize_audio_falls_back_without_ffmpeg(self):
        old_find = telegram_poller.find_ffmpeg
        try:
            telegram_poller.find_ffmpeg = lambda: None
            self.assertEqual(telegram_poller.normalize_audio(Path("clip.ogg")), "clip.ogg")
        finally:
            telegram_poller.find_ffmpeg = old_find


if __name__ == "__main__":
    unittest.main()
