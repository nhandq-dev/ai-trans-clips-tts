from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.synthesis import generate_tts  # noqa: E402
from app.services.voice_catalog import VOICES, greeting_for  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-render one demo MP3 per voice.")
    parser.add_argument("--out", default=str(ROOT / "samples"))
    parser.add_argument("--force", action="store_true", help="Re-render existing samples")
    args = parser.parse_args()

    out_dir = Path(args.out)
    generated = 0
    failed: list[tuple[str, str]] = []

    for voice in VOICES:
        dest = out_dir / voice["language"] / f"{voice['id']}.mp3"
        if dest.exists() and not args.force:
            generated += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            generate_tts(
                greeting_for(voice["language"]),
                language=voice["language"],
                voice=voice["id"],
                out_path=str(dest),
                fmt="mp3",
            )
            print(f"OK   {voice['language']}/{voice['id']} ({dest.stat().st_size} bytes)")
            generated += 1
        except Exception as exc:  # noqa: BLE001
            failed.append((voice["id"], str(exc)))
            print(f"FAIL {voice['language']}/{voice['id']}: {exc}")

    print(f"\n{generated}/{len(VOICES)} ready, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
