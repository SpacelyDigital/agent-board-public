"""Synthesized and supplied audio resources, staging and local-only delivery."""

import hashlib
import http.client
import importlib.util
import io
import json
from pathlib import Path
import re
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import install
from build_support import stage_runtime_assets
from server import BoardHTTPServer, Handler

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("original_alert_generator", ROOT / "tools/generate_alert_sounds.py")
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class AlertSoundTests(unittest.TestCase):
    def test_nine_synthesized_files_are_reproducible_distinct_bounded_pcm(self):
        hashes = set()
        for sound_id, _, duration in generator.SYNTHESIZED_SOUNDS:
            raw = (ROOT / "static/sounds" / (sound_id + ".wav")).read_bytes()
            with self.subTest(sound=sound_id):
                self.assertEqual(raw, generator.make_sound(sound_id))
                with wave.open(io.BytesIO(raw), "rb") as audio:
                    self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()), (1, 2, 22050, "NONE"))
                    self.assertLess(abs(audio.getnframes() / audio.getframerate() - duration), .0001)
                    self.assertLess(audio.getnframes() / audio.getframerate(), 2)
                    pcm = audio.readframes(audio.getnframes())
                values = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
                self.assertLessEqual(max(abs(value) for value in values), round(.141 * 32767))
                self.assertGreater(max(abs(value) for value in values), 2000)
                self.assertEqual(values[0], 0)
                self.assertLess(abs(values[-1]), 20)
                self.assertLess(len(raw), 100_000)
                hashes.add(hashlib.sha256(raw).hexdigest())
        self.assertEqual(len(hashes), 9)

    def test_supplied_fart_is_the_pinned_metadata_free_pcm_recording(self):
        raw = (ROOT / "static/sounds/fart.wav").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         "da103b9e5a4936d572452c5c55f6e1acffc30ae05f2efeb3c6004d0b5b73aad5")
        self.assertEqual(len(raw), 13868)
        self.assertEqual(raw[:4], b"RIFF")
        self.assertEqual(raw[8:12], b"WAVE")
        self.assertEqual(struct.unpack_from("<I", raw, 4)[0], len(raw) - 8)
        chunks = []
        offset = 12
        while offset < len(raw):
            chunk, size = struct.unpack_from("<4sI", raw, offset)
            chunks.append(chunk)
            offset += 8 + size + size % 2
        self.assertEqual(offset, len(raw))
        self.assertEqual(chunks, [b"fmt ", b"data"])
        with wave.open(io.BytesIO(raw), "rb") as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()),
                             (1, 2, 22050, "NONE"))
            self.assertEqual(audio.getnframes(), 6912)
            samples = struct.unpack("<6912h", audio.readframes(6912))
        self.assertLessEqual(max(abs(value) for value in samples), round(.141 * 32767))

    def test_generator_leaves_supplied_recording_unchanged_and_never_creates_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sounds = root / "static/sounds"
            sounds.mkdir(parents=True)
            recorded = sounds / "fart.wav"
            recorded.write_bytes(b"owned recording canary")
            recorded.chmod(0o600)
            before = recorded.stat()
            with patch.object(generator, "__file__", str(root / "tools/generate_alert_sounds.py")):
                generator.main()
                self.assertEqual(recorded.read_bytes(), b"owned recording canary")
                after = recorded.stat()
                self.assertEqual((before.st_ino, before.st_mode, before.st_mtime_ns),
                                 (after.st_ino, after.st_mode, after.st_mtime_ns))
                self.assertEqual({p.name for p in sounds.iterdir()},
                                 {key + ".wav" for key in install.ALERT_SOUND_IDS})
                recorded.unlink()
                generator.main()
                self.assertFalse(recorded.exists())

    def test_supplied_recording_cannot_be_synthesized(self):
        with self.assertRaises(ValueError):
            generator.make_sound("fart")

    def test_generator_native_and_web_fixed_choices_agree(self):
        choices = [key for key, _, _ in generator.SOUNDS]
        self.assertEqual(list(install.ALERT_SOUND_IDS), choices)
        web = json.loads(re.search(r"const alertSounds = (\[[\s\S]*?\]);", (ROOT / "static/app.js").read_text())[1])
        native = json.loads(re.search(r"static let ids = (\[.*?\])", (ROOT / "MacApp.swift").read_text())[1])
        self.assertEqual(web, [[key, label] for key, label, _ in generator.SOUNDS])
        self.assertEqual(native, choices)

    def test_same_bytes_staged_for_main_native_and_all_web_profiles(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp); resources = target / "Resources"; resources.mkdir()
            install.copy_alert_sounds(resources)
            self.assertEqual({p.name for p in resources.iterdir()}, {"agent-board-" + key + ".wav" for key in install.ALERT_SOUND_IDS})
            for store, strategy in [(False, "hidden"), (True, "hidden"), (True, "us-external")]:
                staged = stage_runtime_assets(target / (str(store) + strategy), store_profile=store, support_strategy=strategy)
                for key in install.ALERT_SOUND_IDS:
                    original = (ROOT / "static/sounds" / (key + ".wav")).read_bytes()
                    self.assertEqual((staged / "static/sounds" / (key + ".wav")).read_bytes(), original)
                    self.assertEqual((resources / ("agent-board-" + key + ".wav")).read_bytes(), original)
                    self.assertEqual((resources / ("agent-board-" + key + ".wav")).stat().st_mode & 0o777, 0o644)

    def test_native_staging_rejects_substituted_or_invalid_audio(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); sounds = root / "static/sounds"; sounds.mkdir(parents=True)
            resources = root / "Resources"; resources.mkdir()
            first = sounds / "soft-ding.wav"
            first.symlink_to(ROOT / "static/sounds/soft-ding.wav")
            with patch.object(install, "ROOT", root), self.assertRaises(ValueError): install.copy_alert_sounds(resources)
            first.unlink()
            with wave.open(str(first), "wb") as audio:
                audio.setparams((2, 2, 22050, 1, "NONE", "not compressed")); audio.writeframes(b"\0" * 4)
            with patch.object(install, "ROOT", root), self.assertRaises(ValueError): install.copy_alert_sounds(resources)

    def test_http_serves_only_fixed_local_sound_routes(self):
        server = BoardHTTPServer(("127.0.0.1", 0), Handler)
        server.monitor = SimpleNamespace(store_profile=True, sandbox=True)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        try:
            for key in install.ALERT_SOUND_IDS:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request("GET", "/sounds/" + key + ".wav")
                response = connection.getresponse(); data = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "audio/wav")
                self.assertEqual(data, (ROOT / "static/sounds" / (key + ".wav")).read_bytes())
                self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
                connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", "/sounds/unreviewed.wav"); response = connection.getresponse(); response.read()
            self.assertEqual(response.status, 404); connection.close()
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=2)
