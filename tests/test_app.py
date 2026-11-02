import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

import app as music_app
from mutagen import File as MutagenFile
from mutagen.id3 import ID3


class MusicDownloaderTests(unittest.TestCase):
    def test_safe_folder_name_removes_windows_reserved_characters(self):
        self.assertEqual(music_app.safe_folder_name('Rock: 80/90? *hits*'), "Rock 80 90 hits")

    def test_vtt_to_text_removes_timestamps_and_repeated_lines(self):
        raw = """WEBVTT

00:00:01.000 --> 00:00:03.000
<c>Hola mundo</c>

00:00:03.000 --> 00:00:05.000
Hola mundo

00:00:05.000 --> 00:00:07.000
Otra línea
"""
        self.assertEqual(music_app.vtt_to_text(raw), "Hola mundo\nOtra línea")

    def test_health_and_validation_endpoints(self):
        client = music_app.app.test_client()
        self.assertEqual(client.get("/api/health").status_code, 200)
        self.assertEqual(client.post("/api/playlist", json={"url": "not-a-url"}).status_code, 400)
        self.assertEqual(client.post("/api/downloads", json={"songs": []}).status_code, 400)

    def test_music_playlist_url_is_normalized(self):
        source = "https://music.youtube.com/playlist?list=PL123&si=tracking"
        self.assertEqual(music_app.normalize_youtube_url(source), "https://www.youtube.com/playlist?list=PL123")

    def test_ansi_codes_are_removed_from_errors(self):
        self.assertEqual(music_app.clean_error_text("\x1b[31mERROR:\x1b[0m cookies"), "ERROR: cookies")
        self.assertEqual(music_app.clean_error_text("ERROR: ERROR: DPAPI"), "ERROR: DPAPI")

    def test_build_command_uses_requested_quality_without_fixed_bitrate(self):
        song = {"id": "abc", "index": 7, "title": "Demo", "url": "https://youtube.com/watch?v=abc"}
        with tempfile.TemporaryDirectory() as temp_dir:
            job = music_app.DownloadJob(
                "job", [song], Path(temp_dir),
                {"format": "mp3", "cover": True, "cover_size": 500, "lyrics": True, "concurrency": 4, "timeout_minutes": 15},
            )
            joined = " ".join(music_app.build_command(job, song))
        self.assertIn("--audio-quality 0", joined)
        self.assertNotIn("320", joined)
        self.assertIn("--convert-thumbnails jpg", joined)
        self.assertIn("scale=500:500", joined)
        self.assertIn("--concurrent-fragments 1", joined)
        self.assertIn("--remote-components ejs:github", joined)
        self.assertIn("youtube:player_client=default,mweb", joined)
        self.assertNotIn("%(id)s", joined)
        self.assertNotIn("0007 -", joined)
        self.assertIn(".__musicdl_0007_abc__", joined)
        self.assertNotIn("--write-subs", joined)
        self.assertNotIn("--write-thumbnail", joined)

    def test_lyrics_are_a_separate_best_effort_request(self):
        song = {"id": "abc", "index": 7, "title": "Demo", "url": "https://youtube.com/watch?v=abc"}
        with tempfile.TemporaryDirectory() as temp_dir:
            job = music_app.DownloadJob("job", [song], Path(temp_dir), {"lyrics": True})
            joined = " ".join(music_app.build_lyrics_command(job, song))
        self.assertIn("--skip-download", joined)
        self.assertIn("--write-subs", joined)
        self.assertIn("--write-auto-subs", joined)

    def test_token_errors_get_mweb_and_safari_fallbacks(self):
        profiles = music_app.retry_profiles_for_error(
            "mweb client https formats require a GVS PO Token which was not provided; HTTP Error 403"
        )
        self.assertEqual(profiles, ["mweb", "web_safari"])

    def test_rate_limit_gets_alternate_client(self):
        self.assertEqual(
            music_app.retry_profiles_for_error("HTTP Error 429: Too Many Requests"),
            ["web_safari"],
        )

    def test_unavailable_video_gets_alternate_client_probe(self):
        self.assertEqual(
            music_app.retry_profiles_for_error("ERROR: Video unavailable"),
            ["web_embedded,android_vr"],
        )

    def test_build_command_accepts_a_retry_client_profile(self):
        song = {"id": "abc", "index": 1, "title": "Demo", "url": "https://youtube.com/watch?v=abc"}
        with tempfile.TemporaryDirectory() as temp_dir:
            job = music_app.DownloadJob("job", [song], Path(temp_dir), {"format": "opus"})
            joined = " ".join(music_app.build_command(job, song, "web_safari"))
        self.assertIn("youtube:player_client=web_safari", joined)

    def test_failed_download_cleanup_removes_auxiliary_files(self):
        song = {"id": "abc", "index": 2, "title": "Demo", "url": "https://youtube.com/watch?v=abc"}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            thumbnail = root / "0002 - Demo [abc].jpg"
            subtitles = root / "0002 - Demo [abc].es.vtt"
            thumbnail.write_bytes(b"temporary")
            subtitles.write_text("WEBVTT", encoding="utf-8")
            job = music_app.DownloadJob("job", [song], root, {"lyrics": True})
            music_app.embed_lyrics_and_cleanup(job, song, success=False)
            self.assertFalse(thumbnail.exists())
            self.assertFalse(subtitles.exists())

    def test_existing_video_ids_are_removed_from_filenames(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_file = root / "0048 - Ojos Marrones [6Pkw_7uW2ng].opus"
            old_file.write_bytes(b"audio")
            renamed = music_app.remove_video_ids_from_filenames(root)
            self.assertEqual(renamed, 1)
            self.assertFalse(old_file.exists())
            self.assertTrue((root / "Ojos Marrones.opus").exists())

    def test_final_names_have_no_order_and_keep_duplicate_titles(self):
        song = {"id": "abcdefghijk", "index": 12, "title": "Demo"}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / ".__musicdl_0012_abcdefghijk__ Demo.opus"
            first.write_bytes(b"audio")
            (root / "Demo.opus").write_bytes(b"existing")
            job = music_app.DownloadJob("job", [song], root, {"lyrics": False})
            music_app.embed_lyrics_and_cleanup(job, song, success=True)
            self.assertFalse(first.exists())
            self.assertTrue((root / "Demo.opus").exists())
            self.assertTrue((root / "Demo (2).opus").exists())

    @unittest.skipUnless(music_app.ffmpeg_available(), "FFmpeg is required")
    def test_lyrics_are_embedded_in_mp3_m4a_and_opus(self):
        formats = {
            "mp3": ["-c:a", "libmp3lame"],
            "m4a": ["-c:a", "aac"],
            "opus": ["-c:a", "libopus"],
        }
        for extension, codec_args in formats.items():
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                audio_path = root / f"0001 - Demo.{extension}"
                subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.1",
                    *codec_args, str(audio_path),
                ], check=True)
                (root / "0001 - Demo.es.vtt").write_text(
                    "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nLetra de prueba\n",
                    encoding="utf-8",
                )
                song = {"id": "abcdefghijk", "index": 1, "title": "Demo"}
                job = music_app.DownloadJob("job", [song], root, {"lyrics": True})
                music_app.embed_lyrics_and_cleanup(job, song, success=True)
                audio_path = root / f"Demo.{extension}"
                if extension == "mp3":
                    self.assertTrue(ID3(audio_path).getall("USLT"))
                elif extension == "m4a":
                    self.assertIn("©lyr", MutagenFile(audio_path).tags)
                else:
                    self.assertIn("LYRICS", MutagenFile(audio_path).tags)

    def test_preflight_uses_auth_and_ejs_without_downloading(self):
        song = {"url": "https://youtube.com/watch?v=abc"}
        command = music_app.build_preflight_command({"format": "opus", "auth_browser": "firefox"}, song)
        joined = " ".join(command)
        self.assertIn("--simulate --get-url", joined)
        self.assertIn("--remote-components ejs:github", joined)
        self.assertIn("youtube:player_client=default,mweb", joined)
        self.assertIn("--cookies-from-browser firefox", joined)

    @patch.object(music_app, "pot_provider_ready", return_value=True)
    @patch.object(music_app.subprocess, "run")
    def test_unavailable_first_song_does_not_block_the_whole_queue(self, run_mock, _provider_mock):
        run_mock.return_value.returncode = 1
        run_mock.return_value.stdout = ""
        run_mock.return_value.stderr = "ERROR: Video unavailable"
        ok, detail = music_app.preflight_download(
            {"format": "opus", "auth_browser": "none"},
            {"url": "https://youtube.com/watch?v=missing"},
        )
        self.assertTrue(ok)
        self.assertIn("Video unavailable", detail)

    @patch.object(music_app, "pot_provider_ready", return_value=True)
    @patch.object(music_app.subprocess, "run")
    def test_rate_limited_first_song_does_not_block_the_whole_queue(self, run_mock, _provider_mock):
        run_mock.return_value.returncode = 1
        run_mock.return_value.stdout = ""
        run_mock.return_value.stderr = (
            "WARNING: HTTP Error 429: Too Many Requests\n"
            "WARNING: Only images are available for download\n"
            "ERROR: Requested format is not available"
        )
        ok, detail = music_app.preflight_download(
            {"format": "opus", "auth_browser": "none"},
            {"url": "https://youtube.com/watch?v=rate-limited"},
        )
        self.assertTrue(ok)
        self.assertIn("429", detail)


if __name__ == "__main__":
    unittest.main()
