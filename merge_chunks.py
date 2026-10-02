"""Merge the per-batch annotation files into one canonical chunks file.

Annotation is fanned out to one file per batch (chunks/batch_NN.json) plus the
hand-made pilot (chunks.json). Both the Anki build and the web reviewer read the
merged result, so the merge lives in one place.
"""

import json
from pathlib import Path

HERE = Path(__file__).parent
PILOT = HERE / "chunks.json"
BATCHES = HERE / "chunks"
MERGED = HERE / "chunks.all.json"


def load_all():
    sources = [PILOT] + sorted(BATCHES.glob("batch_*.json"))
    chunks, seen = [], set()
    for path in sources:
        if not path.exists():
            continue
        batch = json.loads(path.read_text())
        kept = 0
        for c in batch:
            key = (c["lesson_id"], c["chunk"].lower())
            if key in seen:
                continue
            seen.add(key)
            chunks.append(c)
            kept += 1
        print(f"  {path.name}: {len(batch)} chunks, {kept} kept")
    return chunks


def main():
    chunks = load_all()
    chunks.sort(key=lambda c: (c["lesson_id"][1:], c["lesson_id"]))
    MERGED.write_text(json.dumps(chunks, ensure_ascii=False, indent=1))
    lessons = len({c["lesson_id"] for c in chunks})
    print(f"\n{len(chunks)} chunks across {lessons} lessons -> {MERGED.name}")


if __name__ == "__main__":
    main()
