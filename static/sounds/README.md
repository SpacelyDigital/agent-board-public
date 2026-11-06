# Alert sounds

These ten short sounds are original offline synthesis for Agent Board, generated
by `tools/generate_alert_sounds.py` using Python's standard library. No recorded
voice, music, provider sound, downloaded sample or external service is used.

Each file is mono, 22,050Hz, 16-bit linear PCM WAV and shorter than two seconds.
The dashboard plays these local files. Packaging also copies the same bytes to
the main app's Resources with `agent-board-` filename prefixes for native alerts.
Soft Ding preserves the previous 740Hz fade. No separate rights are granted to
Agent Board's own assets by the third-party software notices.
