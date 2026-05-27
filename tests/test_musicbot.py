import tempfile
import unittest
from pathlib import Path

import musicbot


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
            brief = musicbot.fallback_music_brief("minor lofi piano", 90, None)
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


if __name__ == "__main__":
    unittest.main()
