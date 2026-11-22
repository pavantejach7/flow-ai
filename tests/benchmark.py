"""Benchmark a known 16 kHz mono WAV without keyboard/paste permissions."""
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d

cfg = d.read_config(d.ROOT / "config.toml")
audio = Path(sys.argv[1]).read_bytes()
results = []
(d.ROOT / ".run").mkdir(exist_ok=True, mode=0o700)
for model in [cfg["whisper"]["model"], cfg["whisper"]["fallback_model"]]:
    cfg["whisper"]["model"] = model
    s = d.Services(cfg)
    try:
        s.start()
        for run in range(3):
            start = time.perf_counter()
            transcript = s.transcribe(audio, d.dictionary(cfg))
            transcribed = time.perf_counter()
            cleaned = s.clean(transcript, d.dictionary(cfg))
            end = time.perf_counter()
            result = {"model": model, "run": run + 1, "whisper_s": round(transcribed-start, 3),
                      "cleanup_s": round(end-transcribed, 3), "pipeline_s": round(end-start, 3),
                      "transcript": transcript, "cleaned": cleaned}
            print(json.dumps(result), flush=True)
            results.append(result)
    finally:
        s.close()
(d.ROOT / "tests" / "benchmark-results.json").write_text(json.dumps(results, indent=2) + "\n")
