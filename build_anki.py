"""Build an Anki .apkg (and a TSV fallback) from chunks.json.

An .apkg is just a zip holding a SQLite collection, so this writes the schema
directly rather than taking a genanki dependency.

Cards are production-oriented: the front shows the Chinese meaning plus the
source sentence with the chunk blanked out, so recall has to go zh -> en in
context. Recognition-only (en -> zh) cards are deliberately not generated.
"""

import hashlib
import json
import re
import sqlite3
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
LESSONS = HERE / "lessons.json"
CHUNKS = HERE / "chunks.all.json"
APKG = HERE / "EnglishPod_Chunks.apkg"
TSV = HERE / "EnglishPod_Chunks.tsv"

DECK_NAME = "EnglishPod::Chunks"
MODEL_NAME = "EnglishPod Chunk (zh -> en)"
FIELDS = ["Prompt_zh", "Chunk_en", "Sentence_blank", "Sentence_full", "Lesson", "LessonId", "Audio"]

QFMT = """<div class="prompt">{{Prompt_zh}}</div>
<div class="sentence">{{Sentence_blank}}</div>
<div class="lesson">{{Lesson}}</div>"""

AFMT = """{{FrontSide}}<hr id=answer>
<div class="chunk">{{Chunk_en}}</div>
<div class="sentence">{{Sentence_full}}</div>
{{Audio}}"""

CSS = """.card {
  font-family: -apple-system, "PingFang SC", "Helvetica Neue", sans-serif;
  font-size: 20px; text-align: center; color: #1a1a1a; background: #fdfdfd;
}
.prompt { font-size: 24px; margin-bottom: 18px; }
.chunk { font-size: 28px; font-weight: 600; color: #1769b5; margin: 14px 0; }
.sentence { font-size: 18px; color: #444; line-height: 1.6; }
.blank { color: #1769b5; font-weight: 600; letter-spacing: 1px; }
.lesson { margin-top: 20px; font-size: 13px; color: #999; }"""

SCHEMA = """
CREATE TABLE col (id integer primary key, crt integer not null, mod integer not null,
  scm integer not null, ver integer not null, dty integer not null, usn integer not null,
  ls integer not null, conf text not null, models text not null, decks text not null,
  dconf text not null, tags text not null);
CREATE TABLE notes (id integer primary key, guid text not null, mid integer not null,
  mod integer not null, usn integer not null, tags text not null, flds text not null,
  sfld integer not null, csum integer not null, flags integer not null, data text not null);
CREATE TABLE cards (id integer primary key, nid integer not null, did integer not null,
  ord integer not null, mod integer not null, usn integer not null, type integer not null,
  queue integer not null, due integer not null, ivl integer not null, factor integer not null,
  reps integer not null, lapses integer not null, left integer not null, odue integer not null,
  odid integer not null, flags integer not null, data text not null);
CREATE TABLE graves (usn integer not null, oid integer not null, type integer not null);
CREATE TABLE revlog (id integer primary key, cid integer not null, usn integer not null,
  ease integer not null, ivl integer not null, lastIvl integer not null, factor integer not null,
  time integer not null, type integer not null);
CREATE INDEX ix_notes_usn on notes (usn);
CREATE INDEX ix_cards_usn on cards (usn);
CREATE INDEX ix_revlog_usn on revlog (usn);
CREATE INDEX ix_cards_nid on cards (nid);
CREATE INDEX ix_cards_sched on cards (did, queue, due);
CREATE INDEX ix_revlog_cid on revlog (cid);
CREATE INDEX ix_notes_csum on notes (csum);
"""


def field_checksum(text):
    """Anki stores the first 8 hex digits of the sort field's SHA1 as an int."""
    return int(hashlib.sha1(text.encode()).hexdigest()[:8], 16)


def guid_for(key):
    """Stable per-chunk guid so re-importing updates notes instead of duplicating."""
    digest = hashlib.sha1(key.encode()).digest()[:8]
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    num = int.from_bytes(digest, "big")
    out = ""
    while num:
        num, rem = divmod(num, len(alphabet))
        out += alphabet[rem]
    return out or "a"


def normalize(text):
    """Fold the differences that are artefacts, not content.

    The source PDF wraps mid-word, so a hyphenated word can carry a stray space
    ("two- by-four"); quote style varies too. Everything else must match the
    source exactly.
    """
    text = text.lower().replace("’", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"\s*-\s*", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def blank_out(sentence, chunk):
    """Replace the chunk with a blank of proportional length, case-insensitively."""
    pattern = re.compile(re.escape(chunk), re.I)
    if not pattern.search(sentence):
        return None
    blank = '<span class="blank">' + "_" * max(6, len(chunk)) + "</span>"
    return pattern.sub(blank, sentence, count=1)


def build_notes(chunks, lessons):
    by_id = {l["id"]: l for l in lessons}
    notes, skipped = [], []
    for c in chunks:
        lesson = by_id.get(c["lesson_id"])
        if not lesson:
            skipped.append((c["chunk"], "unknown lesson " + c["lesson_id"]))
            continue
        # Annotation must quote the dialogue, never paraphrase or truncate it.
        body = normalize(" ".join(t["line"] for t in lesson["turns"]) + " " + lesson["narration"])
        if normalize(c["sentence"]) not in body:
            skipped.append((c["chunk"], "sentence is not verbatim in " + lesson["id"]))
            continue
        if normalize(c["chunk"]) not in body:
            skipped.append((c["chunk"], "chunk is not verbatim in " + lesson["id"]))
            continue
        blanked = blank_out(c["sentence"], c["chunk"])
        if blanked is None:
            skipped.append((c["chunk"], "chunk not found in its sentence"))
            continue
        label = f"{lesson['id']} · {lesson['title']}"
        notes.append(
            {
                "guid": guid_for(c["lesson_id"] + "|" + c["chunk"]),
                "fields": [
                    c["zh"],
                    c["chunk"],
                    blanked,
                    c["sentence"],
                    label,
                    lesson["id"],
                    "",
                ],
                "tags": " ".join(
                    t for t in [lesson["id"], (lesson["category"] or "").replace(" ", "_"), lesson["level"] or ""] if t
                ),
            }
        )
    return notes, skipped


def write_collection(path, notes):
    now_ms = int(time.time() * 1000)
    now_s = int(time.time())
    deck_id, model_id = 1596785400000, 1596785400001

    model = {
        "id": model_id,
        "name": MODEL_NAME,
        "type": 0,
        "mod": now_s,
        "usn": -1,
        "sortf": 0,
        "did": deck_id,
        "tmpls": [
            {
                "name": "Recall",
                "ord": 0,
                "qfmt": QFMT,
                "afmt": AFMT,
                "bqfmt": "",
                "bafmt": "",
                "did": None,
                "bfont": "",
                "bsize": 0,
            }
        ],
        "flds": [
            {
                "name": name,
                "ord": i,
                "sticky": False,
                "rtl": False,
                "font": "Arial",
                "size": 20,
                "description": "",
                "plainText": False,
                "collapsed": False,
                "excludeFromSearch": False,
            }
            for i, name in enumerate(FIELDS)
        ],
        "css": CSS,
        "latexPre": "\\documentclass[12pt]{article}\n\\begin{document}\n",
        "latexPost": "\\end{document}",
        "latexsvg": False,
        "req": [[0, "any", [0, 2]]],
    }

    deck = {
        "id": deck_id,
        "mod": now_s,
        "name": DECK_NAME,
        "usn": -1,
        "lrnToday": [0, 0],
        "revToday": [0, 0],
        "newToday": [0, 0],
        "timeToday": [0, 0],
        "collapsed": False,
        "browserCollapsed": False,
        "desc": "EnglishPod dialogue chunks, zh -> en production.",
        "dyn": 0,
        "conf": 1,
        "extendNew": 0,
        "extendRev": 0,
    }

    dconf = {
        "1": {
            "id": 1,
            "mod": 0,
            "name": "Default",
            "usn": 0,
            "maxTaken": 60,
            "autoplay": True,
            "timer": 0,
            "replayq": True,
            "new": {
                "bury": False,
                "delays": [1.0, 10.0],
                "initialFactor": 2500,
                "ints": [1, 4, 0],
                "order": 1,
                "perDay": 20,
            },
            "rev": {
                "bury": False,
                "ease4": 1.3,
                "ivlFct": 1.0,
                "maxIvl": 36500,
                "perDay": 200,
                "hardFactor": 1.2,
            },
            "lapse": {
                "delays": [10.0],
                "leechAction": 1,
                "leechFails": 8,
                "minInt": 1,
                "mult": 0.0,
            },
            "dyn": False,
            "newMix": 0,
        }
    }

    conf = {
        "nextPos": 1,
        "estTimes": True,
        "activeDecks": [1],
        "sortType": "noteFld",
        "timeLim": 0,
        "sortBackwards": False,
        "addToCur": True,
        "curDeck": 1,
        "newBury": True,
        "newSpread": 0,
        "dueCounts": True,
        "curModel": model_id,
        "collapseTime": 1200,
        "schedVer": 2,
    }

    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute(
        "insert into col values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            1,
            now_s,
            now_ms,
            now_ms,
            11,
            0,
            0,
            0,
            json.dumps(conf),
            json.dumps({str(model_id): model}),
            json.dumps({"1": dict(deck, id=1, name="Default"), str(deck_id): deck}),
            json.dumps(dconf),
            "{}",
        ),
    )

    for i, note in enumerate(notes):
        nid = now_ms + i
        flds = "\x1f".join(note["fields"])
        db.execute(
            "insert into notes values (?,?,?,?,?,?,?,?,?,?,?)",
            (nid, note["guid"], model_id, now_s, -1, note["tags"], flds,
             note["fields"][0], field_checksum(note["fields"][0]), 0, ""),
        )
        db.execute(
            "insert into cards values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (nid, nid, deck_id, 0, now_s, -1, 0, 0, i, 0, 0, 0, 0, 0, 0, 0, 0, ""),
        )

    db.commit()
    db.close()


def main():
    lessons = json.loads(LESSONS.read_text())
    chunks = json.loads(CHUNKS.read_text())
    notes, skipped = build_notes(chunks, lessons)

    # Reject before writing, so a failed build never leaves a partial deck.
    if skipped:
        print(f"{len(skipped)} chunk(s) rejected, nothing written:")
        for chunk, why in skipped:
            print(f"  {chunk!r} -- {why}")
        raise SystemExit(1)

    tmp = HERE / "collection.anki2"
    write_collection(tmp, notes)
    with zipfile.ZipFile(APKG, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp, "collection.anki2")
        z.writestr("media", "{}")
    tmp.unlink()

    rows = ["\t".join(FIELDS + ["Tags"])]
    rows += ["\t".join(n["fields"] + [n["tags"]]).replace("\n", " ") for n in notes]
    TSV.write_text("\n".join(rows))

    lessons_covered = len({c["lesson_id"] for c in chunks})
    print(f"{len(notes)} notes from {lessons_covered} lessons -> {APKG.name}, {TSV.name}")


if __name__ == "__main__":
    main()
