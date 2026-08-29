import shutil
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

import musicbot
import render_with_ace_step
import telegram_poller


TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".runtime" / "test-temp"


@contextmanager
def temporary_directory():
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TEST_TMP_ROOT / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield str(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


class MusicBotTests(unittest.TestCase):
    def test_slugify_keeps_filename_safe(self):
        self.assertEqual(musicbot.slugify("Neon Night Drive!"), "neon-night-drive")

    def test_fallback_renderer_writes_wav(self):
        with temporary_directory() as temp:
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

    def test_command_output_uses_configured_format(self):
        old_config = musicbot.CONFIG
        try:
            musicbot.CONFIG = musicbot.Config(output_format="mp3")
            path = musicbot.choose_output_path({"title": "Neon Night"}, suffix=f".{musicbot.CONFIG.output_format}")
            self.assertEqual(path.suffix, ".mp3")
        finally:
            musicbot.CONFIG = old_config

    def test_clamp_duration_allows_30_minutes(self):
        old_config = musicbot.CONFIG
        try:
            musicbot.CONFIG = musicbot.Config(max_duration_seconds=1800)
            self.assertEqual(musicbot.clamp_duration(1800), 1800)
            self.assertEqual(musicbot.clamp_duration(9999), 1800)
        finally:
            musicbot.CONFIG = old_config

    def test_infer_variation_plan_detects_progression_words(self):
        plan = musicbot.infer_variation_plan("30 minute EDM track that gets faster and higher with breakdowns", 1800)

        self.assertIn("tempo", plan)
        self.assertIn("synth register", plan)
        self.assertIn("breakdowns", plan)

    def test_variant_filename_includes_quality_and_label(self):
        path = musicbot.choose_output_path({"title": "Neon Night", "quality": "draft", "variant_label": "B"}, suffix=".mp3")
        self.assertTrue(path.name.endswith("-neon-night-draft-b.mp3"))

    def test_job_history_persists_and_loads(self):
        old_config = musicbot.CONFIG
        old_history_path = musicbot.HISTORY_INITIALIZED_PATH
        try:
            with temporary_directory() as temp:
                root = Path(temp)
                musicbot.CONFIG = musicbot.Config(
                    output_dir=root / "out",
                    temp_dir=root / "temp",
                    history_db=root / "history.sqlite3",
                )
                musicbot.HISTORY_INITIALIZED_PATH = None
                job = {
                    "id": "job123",
                    "status": "completed",
                    "stage": "done",
                    "payload": {"prompt": "make a synthwave track", "quality": "draft", "variant_label": "A"},
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:01:00Z",
                    "status_url": "/jobs/job123",
                    "output_path": str(root / "out" / "track.mp3"),
                }

                musicbot.save_job(job)
                loaded = musicbot.load_job("job123")

                self.assertIsNotNone(loaded)
                self.assertEqual(loaded["payload"]["prompt"], "make a synthwave track")
                self.assertEqual(loaded["payload"]["variant_label"], "A")
        finally:
            musicbot.CONFIG = old_config
            musicbot.HISTORY_INITIALIZED_PATH = old_history_path

    def test_recover_active_jobs_requeues_running_job(self):
        old_config = musicbot.CONFIG
        old_history_path = musicbot.HISTORY_INITIALIZED_PATH
        old_jobs = musicbot.JOBS
        old_queue = musicbot.JOB_QUEUE
        try:
            with temporary_directory() as temp:
                root = Path(temp)
                musicbot.CONFIG = musicbot.Config(
                    output_dir=root / "out",
                    temp_dir=root / "temp",
                    history_db=root / "history.sqlite3",
                )
                musicbot.HISTORY_INITIALIZED_PATH = None
                musicbot.JOBS = {}
                musicbot.JOB_QUEUE = musicbot.queue.Queue()
                musicbot.save_job(
                    {
                        "id": "stale1",
                        "status": "running",
                        "stage": "generating_draft",
                        "payload": {"prompt": "recover me", "quality": "draft"},
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:01:00Z",
                        "status_url": "/jobs/stale1",
                    }
                )

                recovered = musicbot.recover_active_jobs()

                self.assertEqual(recovered, 1)
                self.assertEqual(musicbot.JOB_QUEUE.get_nowait(), "stale1")
                self.assertEqual(musicbot.JOBS["stale1"]["stage"], "queued_after_restart")
        finally:
            musicbot.CONFIG = old_config
            musicbot.HISTORY_INITIALIZED_PATH = old_history_path
            musicbot.JOBS = old_jobs
            musicbot.JOB_QUEUE = old_queue

    def test_render_path_stays_in_job_dir_then_publishes_to_output_dir(self):
        old_config = musicbot.CONFIG
        try:
            with temporary_directory() as temp:
                root = Path(temp)
                output_dir = root / "dropbox"
                job_dir = root / "job"
                job_dir.mkdir()
                musicbot.CONFIG = musicbot.Config(output_dir=output_dir, temp_dir=root / "temp")
                render_path = musicbot.choose_render_path(job_dir, {"title": "Neon Night"}, ".mp3")
                self.assertEqual(render_path.parent, job_dir)
                render_path.write_bytes(b"x" * 2048)

                published = musicbot.publish_output(render_path, {"title": "Neon Night"})

                self.assertEqual(published.parent, output_dir)
                self.assertTrue(published.exists())
                self.assertEqual(published.read_bytes(), b"x" * 2048)
        finally:
            musicbot.CONFIG = old_config

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

    def test_parse_requested_duration(self):
        self.assertEqual(telegram_poller.parse_requested_duration("make this 3 minutes"), 180)
        self.assertEqual(telegram_poller.parse_requested_duration("make this 1:30"), 90)
        self.assertEqual(telegram_poller.parse_requested_duration("make this 10 seconds"), 20)
        self.assertEqual(telegram_poller.parse_requested_duration("make a 30-minute EDM track"), 1800)
        self.assertEqual(telegram_poller.parse_requested_duration("make a half hour mix"), 1800)

    def test_quality_detection(self):
        self.assertEqual(telegram_poller.parse_quality("make a draft"), "draft")
        self.assertEqual(telegram_poller.parse_quality("make a better high quality version"), "better")

    def test_variation_plan_detection(self):
        plan = telegram_poller.parse_variation_plan("make EDM that gets faster, darker, then has big drops")

        self.assertIn("tempo", plan)
        self.assertIn("deeper bass", plan)
        self.assertIn("drops", plan)

    def test_draft_variant_labels_are_clamped(self):
        old_max = telegram_poller.MAX_DRAFT_VARIANTS
        try:
            telegram_poller.MAX_DRAFT_VARIANTS = 3
            self.assertEqual(telegram_poller.draft_variant_labels(10), ["A", "B", "C"])
        finally:
            telegram_poller.MAX_DRAFT_VARIANTS = old_max

    def test_build_music_payload_marks_batch_variant(self):
        message = {
            "chat_id": 123,
            "message_id": 42,
            "prompt": "make a piano house track",
            "duration_seconds": 120,
            "quality": "draft",
            "username": "tester",
        }

        payload = telegram_poller.build_music_payload(message, batch_id="batch1", variant_label="B", seed=99)

        self.assertEqual(payload["batch_id"], "batch1")
        self.assertEqual(payload["variant_label"], "B")
        self.assertEqual(payload["seed"], 99)

    def test_long_render_uses_single_draft_variant(self):
        old_long_seconds = telegram_poller.LONG_RENDER_SECONDS
        old_long_variants = telegram_poller.LONG_DRAFT_VARIANTS
        old_draft_variants = telegram_poller.DRAFT_VARIANTS
        try:
            telegram_poller.LONG_RENDER_SECONDS = 600
            telegram_poller.LONG_DRAFT_VARIANTS = 1
            telegram_poller.DRAFT_VARIANTS = 3
            self.assertEqual(telegram_poller.draft_variant_count_for_message({"duration_seconds": 1800}), 1)
            self.assertEqual(telegram_poller.draft_variant_count_for_message({"duration_seconds": 120}), 3)
        finally:
            telegram_poller.LONG_RENDER_SECONDS = old_long_seconds
            telegram_poller.LONG_DRAFT_VARIANTS = old_long_variants
            telegram_poller.DRAFT_VARIANTS = old_draft_variants

    def test_long_duration_splits_into_crossfaded_segments(self):
        segments = render_with_ace_step.split_long_duration(1800)
        effective_duration = sum(segments) - render_with_ace_step.long_crossfade_seconds() * (len(segments) - 1)

        self.assertGreater(len(segments), 1)
        self.assertAlmostEqual(effective_duration, 1800, delta=1)

    def test_better_command_detection(self):
        update = {
            "message": {
                "message_id": 43,
                "chat": {"id": 123},
                "text": "/better abcdef123456",
                "from": {"username": "tester"},
            }
        }

        message = telegram_poller.extract_message(update, "token")

        self.assertEqual(message["intent"], "rerender")
        self.assertEqual(message["parent_job_id"], "abcdef123456")
        self.assertEqual(message["quality"], "better")

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
            telegram_poller.download_telegram_file = lambda token, descriptor, target_seconds=None: r"C:\tmp\clip.ogg"
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

    def test_send_generated_audio_uploads_audio_file(self):
        with temporary_directory() as temp:
            path = Path(temp) / "track.mp3"
            path.write_bytes(b"fake audio")
            calls = []
            old_call = telegram_poller.telegram_multipart_call
            try:
                telegram_poller.telegram_multipart_call = lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True}
                telegram_poller.send_generated_audio("token", 123, str(path), 42)
            finally:
                telegram_poller.telegram_multipart_call = old_call

            self.assertEqual(calls[0][1], {})
            self.assertEqual(calls[0][0][1], "sendAudio")
            self.assertEqual(calls[0][0][3], "audio")


if __name__ == "__main__":
    unittest.main()
