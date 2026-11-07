#!/usr/bin/env python3
"""Generate nine PCM alerts offline, preserving the supplied Fart recording."""

import io
import math
from pathlib import Path
import struct
import wave

SAMPLE_RATE = 22050
SOUNDS = (
    ("soft-ding", "Soft Ding", .34),
    ("chime", "Chime", .90),
    ("bell", "Bell", 1.10),
    ("glass", "Glass", .65),
    ("marimba", "Marimba", .60),
    ("arcade", "Arcade", .45),
    ("radar", "Radar", .70),
    ("space-signal", "Space Signal", .85),
    ("pop", "Pop", .16),
    ("fart", "Fart", 6912 / SAMPLE_RATE),
)
SYNTHESIZED_SOUNDS = tuple(sound for sound in SOUNDS if sound[0] != "fart")


def tone(t, frequency, decay, onset=0):
    t -= onset
    return 0 if t < 0 else math.sin(math.tau * frequency * t) * math.exp(-decay * t) * min(1, t / .005)


def make_sound(sound_id):
    duration = next((length for key, _, length in SYNTHESIZED_SOUNDS if key == sound_id), None)
    if duration is None:
        raise ValueError("Only the nine synthesized alert sounds can be generated.")
    phase = 0
    values = []
    for index in range(round(duration * SAMPLE_RATE)):
        t = index / SAMPLE_RATE
        if sound_id == "soft-ding":
            # Preserve the previous foreground 740Hz, .08-gain fade.
            gain = .08 * min(1, t / .015) * math.exp(-math.log(80) * max(0, t - .015) / .305)
            value = math.sin(math.tau * 740 * t) * gain
        elif sound_id == "chime":
            value = sum(tone(t, f, 6, n * .13) for n, f in enumerate((740, 988, 1319)))
        elif sound_id == "bell":
            value = tone(t, 660, 4) + .45 * tone(t, 1370, 7) + .2 * tone(t, 1850, 9)
        elif sound_id == "glass":
            value = tone(t, 1900, 11) + .5 * tone(t, 2710, 14) + .2 * tone(t, 3910, 20)
        elif sound_id == "marimba":
            value = tone(t, 392, 8) + .35 * tone(t, 840, 16) + .15 * tone(t, 1360, 21)
        elif sound_id == "arcade":
            note = min(2, int(t / .10))
            local = t - note * .10
            f = (440, 660, 880)[note]
            value = sum(math.sin(math.tau * f * h * local) / h for h in (1, 3, 5)) * math.exp(-12 * local)
        elif sound_id == "radar":
            value = tone(t, 920, 12) + tone(t, 1120, 12, .28)
        elif sound_id == "space-signal":
            phase += math.tau * (420 + 650 * t + 22 * math.sin(math.tau * 8 * t)) / SAMPLE_RATE
            value = math.sin(phase) * math.exp(-3 * t) * min(1, t / .02)
        elif sound_id == "pop":
            phase += math.tau * (90 + 700 * math.exp(-40 * t)) / SAMPLE_RATE
            value = math.sin(phase) * math.exp(-32 * t)
        edge = min(1, t / .003, (duration - t) / .015)
        values.append(value * max(0, edge))
    scale = 1 if sound_id == "soft-ding" else .14 / max(abs(value) for value in values)
    pcm = b"".join(struct.pack("<h", round(value * scale * 32767)) for value in values)
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, SAMPLE_RATE, len(values), "NONE", "not compressed"))
        audio.writeframes(pcm)
    return output.getvalue()


def main():
    destination = Path(__file__).resolve().parents[1] / "static/sounds"
    destination.mkdir(parents=True, exist_ok=True)
    for sound_id, _, _ in SYNTHESIZED_SOUNDS:
        (destination / (sound_id + ".wav")).write_bytes(make_sound(sound_id))


if __name__ == "__main__":
    main()
