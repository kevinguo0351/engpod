"""Extract EnglishPod dialogues from Dialougue.pdf into structured JSON.

The PDF has no text layer helpers (no pdftotext available), so this walks the
raw objects: inflate content streams, resolve each subset font's /ToUnicode
CMap, and replay the text-showing operators while tracking fill colour.

Titles are rendered in blue, dialogue in black -- that colour split is the only
reliable lesson boundary, since every word is its own text-show operator and
line breaks carry no structure.
"""

import json
import re
import zlib
from collections import Counter
from pathlib import Path

PDF = Path(__file__).parent / "Dialougue.pdf"
OUT = Path(__file__).parent / "lessons.json"

TITLE_RGB = (0.09019, 0.40784, 0.7098)
LEVELS = ("Newbie", "Elementary", "Upper-Intermediate", "Intermediate", "Advanced")
ID_RE = re.compile(r"\(\s*([A-Z])\s*(\d[\d\s]{2,}\d)\s*\)")
SPEAKER_RE = re.compile(r"(?:(?<=^)|(?<=[\s\"]))([A-Z][A-Za-z.']{0,11}):")


def load_objects(data):
    return {
        int(m.group(1)): m.group(2)
        for m in re.finditer(rb"(\d+)\s+0\s+obj(.*?)endobj", data, re.S)
    }


def stream_of(body):
    m = re.search(rb"stream\r?\n", body)
    if not m:
        return None
    raw = body[m.end() : body.rfind(b"endstream")]
    try:
        return zlib.decompress(raw)
    except zlib.error:
        return raw


def parse_cmap(stream):
    cm = {}
    for blk in re.findall(rb"beginbfchar(.*?)endbfchar", stream, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            cm[int(src, 16)] = bytes.fromhex(dst.decode()).decode("utf-16-be", "replace")
    for blk in re.findall(rb"beginbfrange(.*?)endbfrange", stream, re.S):
        for lo, hi, dst in re.findall(
            rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk
        ):
            lo, hi, base = int(lo, 16), int(hi, 16), int(dst, 16)
            for i in range(lo, hi + 1):
                cm[i] = chr(base + i - lo)
    return cm


def build_font_map(objs):
    """Resource name (F1, F2...) -> decoding CMap."""
    cmaps = {}
    for num, body in objs.items():
        st = stream_of(body)
        if st and (b"beginbfchar" in st or b"beginbfrange" in st):
            cm = parse_cmap(st)
            if cm:
                cmaps[num] = cm

    by_font = {}
    for num, body in objs.items():
        if b"/BaseFont" in body:
            tu = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", body)
            if tu and int(tu.group(1)) in cmaps:
                by_font[num] = cmaps[int(tu.group(1))]

    by_name = {}
    for body in objs.values():
        for name, ref in re.findall(rb"/(F\d+)\s+(\d+)\s+0\s+R", body):
            if int(ref) in by_font:
                by_name[name.decode()] = by_font[int(ref)]
    return by_name


def page_contents(objs):
    kids = []
    for body in objs.values():
        if b"/Pages" in body and b"/Kids" in body:
            block = re.search(rb"/Kids\s*\[(.*?)\]", body, re.S).group(1)
            kids = [int(n) for n in re.findall(rb"(\d+)\s+0\s+R", block)]
            break
    contents = []
    for k in kids:
        ref = re.search(rb"/Contents\s+(\d+)\s+0\s+R", objs.get(k, b""))
        if ref:
            contents.append(int(ref.group(1)))
    return contents


TOKEN_RE = re.compile(
    rb"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg"
    rb"|/(F\d+)\s+[\d.]+\s+Tf"
    rb"|\[((?:[^\[\]]|\\.)*)\]\s*TJ"
    rb"|<([0-9A-Fa-f]+)>\s*Tj"
    rb"|(-?[\d.]+)\s+(-?[\d.]+)\s+Td"
)

# F1 is used only for the running header and the page number, never for content.
CHROME_FONT = "F1"


def decode_runs(stream, fonts):
    """Yield (text, is_title) runs in reading order.

    Line wraps need opposite treatment in the two text styles, measured across
    all 120 pages: dialogue wraps on word boundaries and drops the space (6648
    of 6648 samples), while titles wrap mid-word and keep their spaces explicit
    (402 mid-word splits). So a wrap inserts a space in dialogue only.
    """
    font = None
    is_title = False
    line_y = None
    wrapped = False
    runs = []
    for tok in TOKEN_RE.finditer(stream):
        if tok.group(1):
            rgb = tuple(float(tok.group(i)) for i in (1, 2, 3))
            is_title = all(abs(a - b) < 0.01 for a, b in zip(rgb, TITLE_RGB))
        elif tok.group(4):
            font = tok.group(4).decode()
        elif tok.group(7) is not None:
            y = float(tok.group(8))
            wrapped = line_y is not None and y != line_y
            line_y = y
        elif font != CHROME_FONT:
            cmap = fonts.get(font, {})
            payload = tok.group(5) if tok.group(5) is not None else b"<" + tok.group(6) + b">"
            chars = []
            for hexstr in re.findall(rb"<([0-9A-Fa-f]+)>", payload):
                h = hexstr.decode()
                chars += [cmap.get(int(h[i : i + 2], 16), "") for i in range(0, len(h), 2)]
            text = "".join(chars)
            if text:
                if wrapped and not is_title:
                    runs.append((" ", is_title))
                runs.append((text, is_title))
                wrapped = False
    return runs


def clean(text):
    text = text.replace("&quot;", '"').replace("&", "\u2026")
    text = text.replace("\u2010", "-").replace("\u2019", "'").replace("\u201c", '"')
    text = text.replace("\u201d", '"')
    return re.sub(r"\s+", " ", text).strip()


def split_turns(body):
    """Split a dialogue body into [{speaker, line}] on speaker labels.

    Most lessons use A:/B:/C:, but the story series uses character names
    (Steven:, Veronica:). A bare capitalised word followed by a colon is only
    treated as a speaker when it recurs, which keeps mid-sentence colons out.
    """
    marks = list(SPEAKER_RE.finditer(body))
    counts = Counter(m.group(1) for m in marks)
    marks = [m for m in marks if len(m.group(1)) == 1 or counts[m.group(1)] >= 2]

    turns = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        line = clean(body[m.end() : end])
        if line:
            turns.append({"speaker": m.group(1), "line": line})
    return turns


def parse_title(raw):
    """'Elementary - Daily Life - Hotel Upgrade' -> (level, category, title).

    From C0240 on the source stops printing a level, and a handful of lessons
    print it after the category instead of before it.
    """
    raw = clean(raw)
    level = next((lv for lv in LEVELS if raw.startswith(lv)), None)
    if level:
        raw = raw[len(level) :]
    parts = [p for p in (p.strip(" -") for p in raw.split(" - ")) if p]
    if not parts:
        return level, None, ""
    title = parts.pop()
    for part in list(parts):
        if part in LEVELS:
            level = level or part
            parts.remove(part)
    return level, " / ".join(parts) or None, title


def main():
    data = PDF.read_bytes()
    objs = load_objects(data)
    fonts = build_font_map(objs)

    runs = []
    for page_no, ref in enumerate(page_contents(objs), 1):
        stream = stream_of(objs.get(ref, b"")) or b""
        for text, is_title in decode_runs(stream, fonts):
            runs.append((text, is_title, page_no))

    # Merge consecutive runs of the same colour into one block.
    blocks = []
    for text, is_title, page_no in runs:
        if blocks and blocks[-1][1] == is_title:
            blocks[-1][0] += text
        else:
            blocks.append([text, is_title, page_no])

    lessons = []
    pending_title = None
    for text, is_title, page_no in blocks:
        if is_title:
            pending_title = (text, page_no)
            continue
        # A title block ends with "(ID)"; everything after it is dialogue.
        if pending_title:
            head, page_no = pending_title
            pending_title = None
            found = list(ID_RE.finditer(head))
            if found:
                lesson_id = found[-1].group(1) + re.sub(r"\s+", "", found[-1].group(2))
                level, category, title = parse_title(head[: found[-1].start()])
                lessons.append(
                    {
                        "id": lesson_id,
                        "level": level,
                        "category": category,
                        "title": title,
                        "page": page_no,
                        "turns": [],
                        "narration": "",
                    }
                )
        if lessons:
            turns = split_turns(text)
            if turns:
                lessons[-1]["turns"] += turns
            else:
                # "The Night Before Christmas" is a poem with no speaker labels.
                lessons[-1]["narration"] += clean(text)

    for lesson in lessons:
        lesson["word_count"] = sum(len(t["line"].split()) for t in lesson["turns"])
        lesson["word_count"] += len(lesson["narration"].split())

    OUT.write_text(json.dumps(lessons, ensure_ascii=False, indent=2))
    print(f"{len(lessons)} lessons -> {OUT.name}")
    missing_level = [l["id"] for l in lessons if not l["level"]]
    no_turns = [l["id"] for l in lessons if not l["turns"]]
    print(f"no level: {len(missing_level)} {missing_level[:10]}")
    print(f"no turns: {len(no_turns)} {no_turns[:10]}")


if __name__ == "__main__":
    main()
