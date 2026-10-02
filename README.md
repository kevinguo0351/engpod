# ENGpod

Study tooling for the EnglishPod dialogue collection — 359 dialogues parsed out of
the source PDF into structured data, then turned into an Anki deck and a web
reviewer.

## Pipeline

```
Dialougue.pdf  --extract.py-->     lessons.json    (359 dialogues, structured)
lessons.json   --annotation-->     chunks/*.json   (spoken-English chunks + 中文释义)
chunks.json    --build_anki.py-->  EnglishPod_Chunks.apkg  (+ .tsv fallback)
lessons.json   --------------->    index.html      (web reviewer, GitHub Pages)
```

### `extract.py`

Zero-dependency PDF parser (no poppler or pypdf on the target machine). Inflates
the content streams, resolves each subset font's `/ToUnicode` CMap, and replays
the text operators. Lesson boundaries come from **fill colour** — titles render
blue, dialogue black — because every word is its own text-show operator and the
line breaks carry no structure.

Output per lesson: `id`, `level`, `category`, `title`, `page`,
`turns[{speaker, line}]`, `narration`, `word_count`.

```sh
python3 extract.py     # -> lessons.json
```

### `build_anki.py`

Writes a `.apkg` directly (it is a zip around a SQLite collection) plus a TSV
fallback. Cards are **production-oriented**: the front gives the Chinese meaning
and the source sentence with the chunk blanked, so recall runs 中 → 英 in
context. Recognition-only cards are deliberately not generated.

The build **rejects any chunk whose `sentence` is not a verbatim contiguous span
of its lesson** and writes nothing in that case — annotation must quote the
dialogue, never paraphrase it.

```sh
python3 build_anki.py  # -> EnglishPod_Chunks.apkg, EnglishPod_Chunks.tsv
```

## Source material

`Dialougue.pdf` is copyrighted EnglishPod content (Praxis Language) and is
git-ignored. `lessons.json` is derived from it; consider that before making this
repository public.

Audio is not redistributed here — lessons are matched to a Bilibili playlist by
lesson ID.
