#!/usr/bin/env python3
"""
pdf2epub — conversor PDF -> EPUB3 com reconstrução de layout.

Uso rápido:
    python pdf2epub.py livro.pdf                 # gera livro.epub ao lado
    python pdf2epub.py pasta/ -o saida/          # converte todos os PDFs da pasta
    python pdf2epub.py --watch entrada/ -o saida/  # modo automático (vigia a pasta)

O que ele faz (motor "native"):
  * detecta o corpo do texto pelo tamanho de fonte dominante; fontes maiores viram <h1>..<h4>
  * remove cabeçalhos/rodapés repetidos e números de página
  * remove páginas de sumário (o EPUB ganha um sumário navegável próprio)
  * reconstrói parágrafos: junta linhas, junta parágrafos quebrados entre páginas/colunas
  * des-hifeniza ("extraordi-\\nnário" -> "extraordinário") usando um vocabulário do próprio livro
    e preservando ênclises/compostos ("assassiná-lo", "guarda-chuva")
  * notas de rodapé -> notas pop-up EPUB3 (epub:type="noteref"/"footnote")
  * itálico, negrito, sobrescrito, citações em bloco, listas com marcadores
  * imagens raster, figuras vetoriais (renderizadas em PNG) e tabelas
  * layout em 2 colunas
  * páginas escaneadas: OCR via Tesseract (se instalado) ou a página vira imagem
  * capa a partir da 1ª página, metadados, idioma detectado automaticamente
Motor alternativo: --engine calibre (usa ebook-convert do Calibre com heurísticas).
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import unicodedata
import hashlib
import io
import html
import re
import shutil
import statistics
import subprocess
import sys
import time
import uuid
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    import pymupdf
except ImportError:  # pragma: no cover
    sys.exit("PyMuPDF não instalado: python -m pip install pymupdf")

FLAGS = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_LIGATURES
SENT_END = tuple('.!?…"”»\'’)]')
BULLETS = "•●▪■◦‣∙▸►"
DIALOGUE = ("—", "–", "―")
CHAPTER_RE = re.compile(
    r"^(cap[íi]tulo|chapter|chapitre|capitolo|kapitel|cap\.)\s+([IVXLCDM]+|\d+|[a-zà-ú]+(?:\s[a-zà-ú]+)?)\b[\s.:—–-]*(.{0,80})$", re.I)
PART_RE = re.compile(r"^(parte|part|partie|teil|livro|book|livre|libro|volume|tomo)\s+([IVXLCDM]+|\d+|[a-zà-ú]+)\b[\s.:—–-]*(.{0,80})$", re.I)
SECTION_RE = re.compile(r"^(pref[áa]cio|pr[óo]logo|ep[íi]logo|introdu[çc][ãa]o|apresenta[çc][ãa]o|conclus[ãa]o|posf[áa]cio|ap[êe]ndice|"
                        r"preface|prologue|epilogue|introduction|foreword|afterword|conclusion|appendix|"
                        r"pr[ée]face|avant-propos|prefazione|introduzione|vorwort|einleitung)\b.{0,40}$", re.I)
CAPTION_RE = re.compile(r"^(fig(ura|ure)?\.?|tabela|table|tableau|tavola|quadro|gráfico|imagem|image)\s*[\dIVX]+", re.I)
PAGE_NUM_RE = re.compile(
    r"^[\W_]*(page|página|pagina|pág\.?|p\.|seite|s\.)?\s*(\d{1,4}|[ivxlcdm]{1,7})(\s*(of|de|di|/|von)\s*\d+)?[\W_]*$", re.I)
TOC_LINE_RE = re.compile(r"(^|[\s.·…_\u2024])\d{1,4}\s*$")
TOC_TITLE_RE = re.compile(r"^\s*(sum[áa]rio|[íi]ndice|contents|table of contents|table des mati[èe]res|indice|inhalt|inhaltsverzeichnis|sommaire)\s*$", re.I)
FN_MARK_RE = re.compile(r"^\s*(\d{1,3}|[*†‡§¶]+)(?=[\s.)\]]|[A-ZÀ-Ýa-zà-ý])")
INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
INVISIBLE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")

COMPOUND_SUFFIX = {
    "pt": {"lhas", "lhos", "lha", "lho", "mos", "vos", "nas", "lo", "la", "los", "las", "no", "na", "nos", "nas", "me", "te", "se", "lhe", "lhes", "vos", "o", "a",
           "os", "as", "mo", "ma", "to", "ta", "lho", "lha", "á", "ão", "ás", "ia", "iam", "emos", "ei", "ás"},
    "es": set(),
    "fr": {"t", "il", "ils", "elle", "elles", "on", "je", "tu", "nous", "vous", "moi", "toi", "ce", "là", "ci",
           "le", "la", "les", "lui", "leur", "y", "en", "même", "mêmes"},
    "it": set(), "en": set(), "de": set(),
}
STOPWORDS = {
    "pt": "não uma para com mais os dos como ao das à seu sua também pelo pela isso ela você são foi está há nos já muito essa este esta entre quando sobre".split(),
    "en": "the and of to is that it was for on are with his they be at have this from by not which you".split(),
    "es": "el los del las por con una su es al lo pero sus le ya este porque esta entre cuando muy sin sobre también hasta hay".split(),
    "fr": "le les des et est une du que qui dans pour pas au sur ne se ce il sont avec plus par cette".split(),
    "it": "il di che è la per un non una sono gli come da questo della nel alla anche più ma lo".split(),
    "de": "der die und den von zu das mit sich des auf für ist im dem nicht ein eine als auch es".split(),
}


# --------------------------------------------------------------------------- dados
@dataclass
class Run:
    text: str
    b: bool = False
    i: bool = False
    sup: bool = False
    ref: str | None = None


@dataclass
class Line:
    runs: list
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    page: int
    col: int = 0
    col_left: float = 0.0
    col_right: float = 0.0

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)

    @property
    def bold(self) -> bool:
        rs = [r for r in self.runs if r.text.strip()]
        return bool(rs) and all(r.b for r in rs)


@dataclass
class Block:
    kind: str                       # text | image | table | figure
    bbox: tuple
    lines: list = field(default_factory=list)
    image: str | None = None        # nome do arquivo de imagem
    rows: list | None = None
    col: int = 0


@dataclass
class Page:
    pno: int
    w: float
    h: float
    blocks: list = field(default_factory=list)
    skip: bool = False
    scanned: bool = False
    notes: list = field(default_factory=list)   # [(marker, uid)]


@dataclass
class Para:
    kind: str                       # p | h | img | table | li | mark
    runs: list = field(default_factory=list)
    lines: list = field(default_factory=list)
    level: int = 0
    cls: str = ""
    image: str | None = None
    caption: list | None = None
    rows: list | None = None
    page: int = 0
    anchor: str = ""
    title: str = ""

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


class Ctx:
    def __init__(self, opts):
        self.opts = opts
        self.images: dict[str, tuple] = {}       # name -> (bytes, media_type)
        self.img_pages: Counter = Counter()
        self.notes: dict[str, dict] = {}         # uid -> {"runs":[], "page":p, "marker":m}
        self.note_seq = 0
        self.vocab: Counter = Counter()
        self.parts: Counter = Counter()
        self.uncertain_joins: list[str] = []
        self.warnings: list[str] = []
        self.stats = Counter()
        self.lang = "en"
        self.hyph_rate = 0.0
        self.auto_hyphenated = True


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------- extração
def parse_line(l, pno) -> Line | None:
    spans = [s for s in l["spans"] if s["text"]]
    if not spans:
        return None
    cnt = Counter()
    for s in spans:
        cnt[round(s["size"], 1)] += max(1, len(s["text"].strip()))
    size = max(cnt, key=cnt.get)
    base = [s["origin"][1] for s in spans if abs(s["size"] - size) < 0.6]
    baseline = max(base) if base else max(s["origin"][1] for s in spans)
    runs = []
    for s in spans:
        f, font = s["flags"], s["font"].lower()
        b = bool(f & 16) or any(k in font for k in ("bold", "black", "heavy", "semibold", "demi"))
        it = bool(f & 2) or "italic" in font or "oblique" in font
        sup = bool(f & 1) or (s["size"] < size * 0.85 and s["origin"][1] < baseline - size * 0.2)
        runs.append(Run(INVISIBLE.sub("", s["text"]).replace("\r", " ").replace("\n", " "), b, it, sup))
    x0, y0, x1, y1 = l["bbox"]
    return Line(runs, x0, y0, x1, y1, size, pno)


def merge_same_baseline(lines):
    out = []
    for ln in lines:
        if out:
            p = out[-1]
            if abs(ln.y1 - p.y1) < 0.4 * p.size and ln.x0 >= p.x1 - 1:
                if ln.x0 - p.x1 > 0.15 * p.size and not p.text.endswith(" "):
                    p.runs.append(Run(" ", p.runs[-1].b, p.runs[-1].i))
                p.runs.extend(ln.runs)
                p.x1 = max(p.x1, ln.x1)
                p.y0, p.y1 = min(p.y0, ln.y0), max(p.y1, ln.y1)
                continue
        out.append(ln)
    return out


def text_lines_from_dict(d, pno):
    blocks = []
    for b in d["blocks"]:
        if b["type"] != 0:
            continue
        lines = []
        for l in b["lines"]:
            dx, dy = l["dir"]
            if abs(dy) > 0.05 or dx < 0:     # texto rotacionado (marcas d'água, lombadas)
                continue
            ln = parse_line(l, pno)
            if ln and ln.text.strip():
                lines.append(ln)
        lines = merge_same_baseline(lines)
        if lines:
            blocks.append(Block("text", bbox_of(lines), lines=lines))
    return blocks


def bbox_of(lines):
    return (min(l.x0 for l in lines), min(l.y0 for l in lines), max(l.x1 for l in lines), max(l.y1 for l in lines))


def inside(line, r):
    cx, cy = (line.x0 + line.x1) / 2, (line.y0 + line.y1) / 2
    return r.x0 - 1 <= cx <= r.x1 + 1 and r.y0 - 1 <= cy <= r.y1 + 1


def tesseract_ok():
    try:
        return bool(pymupdf.get_tessdata())
    except Exception:
        return False


def store_image(ctx, data: bytes, ext: str, cs: int, page, bbox) -> str | None:
    h = hashlib.md5(data).hexdigest()[:14]
    for ext_ok in ("jpg", "png"):
        if f"{h}.{ext_ok}" in ctx.images:
            return f"{h}.{ext_ok}"
    try:
        if ext in ("jpeg", "jpg") and cs in (1, 3):
            name, payload, mt = f"{h}.jpg", data, "image/jpeg"
        elif ext == "png":
            name, payload, mt = f"{h}.png", data, "image/png"
        else:
            pix = pymupdf.Pixmap(data)
            if pix.colorspace and pix.colorspace.n not in (1, 3):
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            name, payload, mt = f"{h}.png", pix.tobytes("png"), "image/png"
    except Exception:
        pix = page.get_pixmap(clip=pymupdf.Rect(bbox), dpi=200)
        name, payload, mt = f"{h}.png", pix.tobytes("png"), "image/png"
    ctx.images[name] = (payload, mt)
    return name


def extract_page(doc, pno, ctx) -> Page:
    opts = ctx.opts
    page = doc[pno]
    W, H = page.rect.width, page.rect.height
    pg = Page(pno, W, H)
    d = page.get_text("dict", flags=FLAGS)
    tblocks = text_lines_from_dict(d, pno)
    nwords = sum(len(l.text.split()) for b in tblocks for l in b.lines)

    raster = [b for b in d["blocks"] if b["type"] == 1]
    big_img = any((b["bbox"][2] - b["bbox"][0]) * (b["bbox"][3] - b["bbox"][1]) > 0.5 * W * H for b in raster)

    if nwords < 3 and big_img:                                # página escaneada
        pg.scanned = True
        if opts.ocr and tesseract_ok():
            try:
                tp = page.get_textpage_ocr(language=opts.ocr_lang, dpi=300, full=True)
                d = page.get_text("dict", flags=FLAGS, textpage=tp)
                tblocks = text_lines_from_dict(d, pno)
                ctx.stats["ocr_pages"] += 1
                pg.blocks = tblocks
                return pg
            except Exception as e:
                ctx.warnings.append(f"p.{pno+1}: OCR falhou ({e})")
        pix = page.get_pixmap(dpi=150)
        name = store_image(ctx, pix.tobytes("jpeg", jpg_quality=82), "jpeg", 3, page, page.rect)
        pg.blocks = [Block("image", tuple(page.rect), image=name)]
        ctx.stats["scanned_as_image"] += 1
        return pg

    sz = Counter()
    for b in tblocks:
        for l in b.lines:
            sz[round(l.size * 2) / 2] += len(l.text)
    page_body = max(sz, key=sz.get) if sz else 10.0
    all_l = [l for b in tblocks for l in b.lines]

    figs = []
    if opts.figures:
        try:
            clusters = page.cluster_drawings(x_tolerance=10, y_tolerance=10)
        except Exception:
            clusters = []
        for r in clusters:
            if r.width < 40 or r.height < 25 or r.width * r.height > 0.85 * W * H:
                continue
            # texto em tamanho de corpo dentro do retângulo => caixa sombreada / tabela, não figura
            body_words = sum(len(l.text.split()) for l in all_l if inside(l, r) and l.size >= page_body * 0.95)
            if body_words > 40:
                continue
            # inclui rótulos pequenos colados à figura (eixos, legendas internas)
            r = pymupdf.Rect(r)
            for _ in range(3):
                grow = False
                for l in all_l:
                    lr = pymupdf.Rect(l.x0, l.y0, l.x1, l.y1)
                    if l.size < page_body * 0.95 and not r.contains(lr) and (r + (-8, -8, 8, 8)).intersects(lr) \
                            and not CAPTION_RE.match(l.text.strip()):
                        r |= lr
                        grow = True
                if not grow:
                    break
            figs.append(r)
        # funde figuras sobrepostas
        merged = []
        for r in sorted(figs, key=lambda r: (r.y0, r.x0)):
            for k, m in enumerate(merged):
                if (m + (-4, -4, 4, 4)).intersects(r):
                    merged[k] = pymupdf.Rect(m) | r
                    break
            else:
                merged.append(pymupdf.Rect(r))
        figs = merged

    tables = []
    if opts.tables:
        try:
            with contextlib.redirect_stdout(io.StringIO()):     # silencia propaganda do pymupdf_layout
                found = page.find_tables().tables
        except Exception:
            found = []
        for t in found:
            if t.row_count < 2 or t.col_count < 2:
                continue
            rows = t.extract()
            cells = [c for row in rows for c in row]
            empty = sum(1 for c in cells if not (c or "").strip()) / max(1, len(cells))
            tr = pymupdf.Rect(t.bbox)
            clean = empty <= 0.35 and not any(len(c or "") > 300 for c in cells)
            if clean:
                figs = [f for f in figs if not (f & tr).get_area() > 0.5 * min(f.get_area(), tr.get_area())]
                tables.append((tr, rows))
            elif not any(f.contains(tr) for f in figs):
                figs.append(tr)                      # tabela "suja" (gráfico, colchetes): vira imagem fiel

    def keep(l):
        return not any(inside(l, r) for r, _ in tables) and not any(inside(l, r) for r in figs)

    for b in tblocks:
        b.lines = [l for l in b.lines if keep(l)]
    blocks = [Block("text", bbox_of(b.lines), lines=b.lines) for b in tblocks if b.lines]

    for r, rows in tables:
        blocks.append(Block("table", tuple(r), rows=rows))
        ctx.stats["tables"] += 1
    for r in figs:
        r = r & page.rect
        pix = page.get_pixmap(clip=r, dpi=200)
        name = store_image(ctx, pix.tobytes("png"), "png", 3, page, r)
        blocks.append(Block("figure", tuple(r), image=name))
        ctx.stats["vector_figures"] += 1
    for b in raster:
        x0, y0, x1, y1 = b["bbox"]
        if x1 - x0 < 20 or y1 - y0 < 20:
            continue
        if (x1 - x0) * (y1 - y0) > 0.8 * W * H and nwords > 20:   # fundo de página escaneada com OCR
            continue
        if any(pymupdf.Rect(b["bbox"]).intersects(f) for f in figs):
            continue
        rr = pymupdf.Rect(b["bbox"])
        overlay = [l for tb in blocks if tb.kind == "text" for l in tb.lines if inside(l, rr)]
        if overlay and sum(len(l.text.split()) for l in overlay) <= 40:
            for tb in blocks:
                if tb.kind == "text":
                    tb.lines = [l for l in tb.lines if not inside(l, rr)]
            blocks = [tb for tb in blocks if tb.kind != "text" or tb.lines]
            name = store_image(ctx, page.get_pixmap(clip=rr, dpi=200).tobytes("png"), "png", 3, page, rr)
        else:
            name = store_image(ctx, b["image"], b.get("ext", "png"), b.get("colorspace", 3), page, b["bbox"])
        if name:
            ctx.img_pages[name] += 1
            blocks.append(Block("image", (x0, y0, x1, y1), image=name))
    pg.blocks = blocks
    return pg


# --------------------------------------------------------------------------- análise global
def all_lines(pages):
    for pg in pages:
        for b in pg.blocks:
            if b.kind == "text":
                yield pg, b, from_lines(b)


def from_lines(b):
    return b.lines


def body_size(pages):
    c = Counter()
    for pg, b, lines in all_lines(pages):
        for l in lines:
            c[round(l.size * 2) / 2] += len(l.text)
    return max(c, key=c.get) if c else 10.0


def norm_hf(t):
    t = re.sub(r"\d+", "#", t.strip().lower())
    return re.sub(r"\s+", " ", t)


def remove_headers_footers(pages, body, ctx):
    zone = ctx.opts.hf_zone
    cand = Counter()
    for pg in pages:
        seen = set()
        for b in pg.blocks:
            if b.kind != "text":
                continue
            for l in b.lines:
                if (l.y1 < pg.h * zone or l.y0 > pg.h * (1 - zone)) and l.size <= body * 1.1:
                    seen.add(norm_hf(l.text))
        cand.update(seen)
    thr = 2 if len(pages) <= 10 else 3
    removed = 0
    for pg in pages:
        for b in pg.blocks:
            if b.kind != "text":
                continue
            kept = []
            for l in b.lines:
                inz = l.y1 < pg.h * zone or l.y0 > pg.h * (1 - zone)
                if inz and l.size <= body * 1.1 and (PAGE_NUM_RE.match(l.text.strip()) or cand[norm_hf(l.text)] >= thr):
                    removed += 1
                    continue
                kept.append(l)
            b.lines = kept
        pg.blocks = [b for b in pg.blocks if b.kind != "text" or b.lines]
    ctx.stats["header_footer_lines_removed"] = removed


def detect_toc_pages(pages, ctx):
    n = len(pages)
    prev_toc = False
    for pg in pages:
        lines = [l for b in pg.blocks if b.kind == "text" for l in b.lines]
        has_title = any(TOC_TITLE_RE.match(l.text) for l in lines[:4])
        if len(lines) < (2 if has_title else 5) or not (pg.pno < max(3, n * 0.2) or pg.pno > n * 0.85):
            prev_toc = False
            continue
        ratio = sum(1 for l in lines if TOC_LINE_RE.search(l.text.strip())) / len(lines)
        if ratio >= 0.6 or (ratio >= 0.3 and has_title) or (prev_toc and ratio >= 0.4):
            pg.skip = True
            prev_toc = True
            ctx.stats["toc_pages_removed"] += 1
        else:
            prev_toc = False


def extract_footnotes(pages, body, ctx):
    last_note = None
    for pg, lines in ((pg, sorted([(l, b) for b in pg.blocks if b.kind == "text" and b.col == col for l in b.lines],
                                  key=lambda t: t[0].y0))
                      for pg in pages for col in sorted({b.col for b in pg.blocks if b.kind == "text"} or {0})):
        k = len(lines)
        while k > 0 and lines[k - 1][0].size <= body * 0.9 and lines[k - 1][0].y0 > pg.h * 0.45:
            k -= 1
        cand = lines[k:]
        if not cand:
            continue

        def marker(l):
            r0 = next((r for r in l.runs if r.text.strip()), None)
            if r0 is not None and r0.sup and FN_MARK_RE.match(r0.text.strip() + " "):
                return r0.text.strip()
            m = FN_MARK_RE.match(l.text)
            return m.group(1) if m else None

        if marker(cand[0][0]) is None:
            if last_note and last_note["page"] == pg.pno - 1 and len(cand) <= 8:
                # continuação de nota da página anterior
                for l, b in cand:
                    last_note["runs"].append(Run(" "))
                    last_note["runs"].extend(l.runs)
                    b.lines.remove(l)
                pg.blocks = [b for b in pg.blocks if b.kind != "text" or b.lines]
            continue
        cur = None
        left = min(l.x0 for l, _ in cand)
        for l, b in cand:
            m = marker(l)
            if m is not None and l.x0 <= left + body * 2.5:
                if cur is None and getattr(ctx.opts, "debug", False):
                    print(f"   [debug] p.{pg.pno+1}: notas começam em {l.text[:60]!r}")
                ctx.note_seq += 1
                uid = f"fn{ctx.note_seq}"
                runs = strip_marker(l.runs, m)
                cur = {"runs": runs, "page": pg.pno, "marker": m, "uid": uid, "ref": False}
                ctx.notes[uid] = cur
                pg.notes.append((m, uid))
            elif cur is not None:
                cur["runs"].append(Run(" "))
                cur["runs"].extend(l.runs)
            else:
                continue                      # ainda não há nota aberta: a linha fica no corpo
            b.lines.remove(l)
        last_note = cur
        pg.blocks = [b for b in pg.blocks if b.kind != "text" or b.lines]
        ctx.stats["footnotes"] += 0
    ctx.stats["footnotes"] = len(ctx.notes)
    # liga as chamadas no corpo
    for pg in pages:
        if not pg.notes:
            continue
        mp = {}
        for m, uid in pg.notes:
            mp.setdefault(m, uid)
        for b in pg.blocks:
            if b.kind != "text":
                continue
            for l in b.lines:
                for r in l.runs:
                    t = r.text.strip()
                    if r.sup and t in mp and not ctx.notes[mp[t]]["ref"]:
                        r.ref = mp[t]
                        ctx.notes[mp[t]]["ref"] = True
        # fallback: chamada desenhada com glifo sobrescrito da fonte (mesmo tamanho) -> "palavra.1 "
        for m, uid in pg.notes:
            if ctx.notes[uid]["ref"] or not m.isdigit():
                continue
            pat = re.compile(r"(?<=[^\W\d]|[.,;:!?»”’)])" + re.escape(m) + r"(?=[\s.,;:)]|$)")
            done = False
            for b in pg.blocks:
                if done or b.kind != "text":
                    continue
                for l in b.lines:
                    for k, r in enumerate(l.runs):
                        if r.ref or r.sup:
                            continue
                        mm = pat.search(r.text)
                        if mm:
                            a, z = mm.span()
                            new = [Run(r.text[:a], r.b, r.i), Run(m, r.b, r.i, True, uid), Run(r.text[z:], r.b, r.i)]
                            l.runs[k:k + 1] = [x for x in new if x.text or x.ref]
                            ctx.notes[uid]["ref"] = True
                            done = True
                            break
                    if done:
                        break


def strip_marker(runs, m):
    runs = [Run(r.text, r.b, r.i, r.sup) for r in runs]
    while runs and not runs[0].text.strip():
        runs.pop(0)
    if runs:
        t = runs[0].text.lstrip()
        if t.startswith(m):
            runs[0].text = t[len(m):].lstrip(" .)]")
        if not runs[0].text and len(runs) > 1:
            runs.pop(0)
        if runs:
            runs[0].sup = False
    return runs


def order_blocks(pg, body):
    blocks = [b for b in pg.blocks]
    if not blocks:
        return []
    xmin = min(b.bbox[0] for b in blocks)
    xmax = max(b.bbox[2] for b in blocks)
    W = max(1.0, xmax - xmin)
    narrow = [b for b in blocks if b.kind == "text" and (b.bbox[2] - b.bbox[0]) < 0.55 * W and len(b.lines) >= 2]
    gx = None
    if len(narrow) >= 2:
        cov = [0] * (int(W) + 2)
        for b in narrow:
            for x in range(int(b.bbox[0] - xmin), int(b.bbox[2] - xmin) + 1):
                cov[x] += 1
        lo, hi = int(W * 0.3), int(W * 0.7)
        best, run, start = (0, None), 0, None
        for x in range(lo, hi):
            if cov[x] == 0:
                run, start = run + 1, (start if run else x)
                if run > best[0]:
                    best = (run, start)
            else:
                run = 0
        if best[0] >= 5:
            g = xmin + best[1] + best[0] / 2
            left = [b for b in narrow if b.bbox[2] <= g]
            right = [b for b in narrow if b.bbox[0] >= g]
            if left and right:
                gx = g
    if gx is None:
        for b in blocks:
            b.col = 0
        return sorted(blocks, key=lambda b: (round(b.bbox[1]), b.bbox[0]))
    out, seg = [], []

    def flush():
        L = sorted([b for b in seg if b.bbox[2] <= gx + 2], key=lambda b: b.bbox[1])
        R = sorted([b for b in seg if b.bbox[2] > gx + 2], key=lambda b: b.bbox[1])
        for b in L:
            b.col = 1
        for b in R:
            b.col = 2
        out.extend(L + R)
        seg.clear()

    for b in sorted(blocks, key=lambda b: b.bbox[1]):
        if b.bbox[0] < gx - 2 and b.bbox[2] > gx + 2:
            flush()
            b.col = 0
            out.append(b)
        else:
            seg.append(b)
    flush()
    pg.two_col = True
    return out


def assign_column_margins(pg, body):
    groups = defaultdict(list)
    for b in pg.blocks:
        if b.kind == "text":
            for l in b.lines:
                l.col = b.col
                groups[b.col].append(l)
    all_l = [l for g in groups.values() for l in g]
    if not all_l:
        return

    def margins(ls):
        body_ls = [l for l in ls if abs(l.size - body) < 1.2] or ls
        c = Counter(round(l.x0) for l in body_ls)
        left = min(k for k, v in c.items() if v == max(c.values()))
        xs = sorted(l.x1 for l in body_ls)
        right = xs[int(len(xs) * 0.9)] if len(xs) > 2 else xs[-1]
        return left, right

    page_m = margins(all_l)
    for col, ls in groups.items():
        m = margins(ls) if len(ls) >= 3 else page_m
        for l in ls:
            l.col_left, l.col_right = m


def build_vocab(pages, ctx):
    tok = re.compile(r"\w[\w’'-]*\w|\w")
    n_lines = n_hyph = 0
    for pg, b, lines in all_lines(pages):
        for l in lines:
            n_lines += 1
            if re.search(r"[^\W\d][-\u2010\u00ad]\s*$", l.text):
                n_hyph += 1
            ws = tok.findall(l.text)
            for w in ws[1:-1]:
                ctx.vocab[w.lower()] += 1
                if "-" in w:
                    for part in w.lower().split("-"):
                        ctx.parts[part] += 1
    ctx.hyph_rate = n_hyph / max(1, n_lines)
    # < 1% das linhas terminando em hífen => documento SEM hifenização automática (ex.: Word):
    # hífen de fim de linha é de palavra composta e deve ser mantido
    ctx.auto_hyphenated = ctx.hyph_rate >= 0.01
    ctx.stats["line_end_hyphen_rate_%"] = round(100 * ctx.hyph_rate, 2)


def detect_lang(pages):
    words = []
    for pg, b, lines in all_lines(pages):
        for l in lines:
            words.extend(w.lower() for w in re.findall(r"\w+", l.text))
        if len(words) > 20000:
            break
    c = Counter(words)
    score = {lg: sum(c[w] for w in sw) for lg, sw in STOPWORDS.items()}
    return max(score, key=score.get) if any(score.values()) else "en"


def heading_levels(pages, body):
    sizes = Counter()
    first = pages[0].pno if pages else 0
    for pg, b, lines in all_lines(pages):
        for l in lines:
            if l.size >= body * 1.18 and len(l.text) < 250 and (pg.pno != first or len(pages) == 1):
                sizes[round(l.size * 2) / 2] += 1
    clusters = []
    for s in sorted(sizes, reverse=True):
        if clusters and clusters[-1][-1] - s <= 0.6:
            clusters[-1].append(s)
        else:
            clusters.append([s])
    lv = {}
    for i, cl in enumerate(clusters):
        for s in cl:
            lv[s] = min(i + 1, 4)

    def level(size):
        s = round(size * 2) / 2
        if s in lv:
            return lv[s]
        if not lv or s > max(lv):
            return 1
        near = min(lv, key=lambda k: abs(k - s))
        return lv[near]
    return level


# --------------------------------------------------------------------------- parágrafos
def first_alpha(t):
    for ch in t:
        if ch.isalpha():
            return ch
    return ""


class Builder:
    def __init__(self, ctx, body, level_fn, typical_gap, indent_style):
        self.ctx, self.body, self.level_fn = ctx, body, level_fn
        self.gap, self.indent_style = typical_gap, indent_style
        self.paras: list[Para] = []
        self.cur: Para | None = None
        self.prev: Line | None = None
        self.pending: list[Para] = []
        self.discont = True
        self.anchor_seq = 0
        self.last_float = None

    def is_heading(self, l):
        if not (l.size >= self.body * 1.18 and len(l.text) < 250):
            return False
        t = l.text.strip()
        last = self.paras[-1] if (self.cur is None and self.paras) else None
        if (last is not None and last.kind == "h" and last.lines and last.lines[-1].page == l.page
                and abs(last.lines[-1].size - l.size) < 0.6 and 0 <= l.y0 - last.lines[-1].y1 < 1.6 * l.size):
            return True                                              # 2ª linha de um título
        return not (first_alpha(t).islower() and len(t) > 20)     # frase em destaque, não título

    def close(self):
        if self.cur is not None:
            self.paras.append(self.cur)
            self.cur = None
        if self.pending:
            self.paras.extend(self.pending)
            self.pending = []

    def page_break(self):
        self.discont = True

    def try_caption(self, block) -> bool:
        """Bloco 'Figura N...' logo após uma figura: vira legenda sem quebrar o parágrafo em curso."""
        f = self.last_float
        if f is None or f.caption is not None or f.kind != "img" or f.page != block.lines[0].page:
            return False
        if not CAPTION_RE.match(block.lines[0].text.strip()):
            return False
        fb = getattr(f, "bbox", None)
        if fb is None:
            return False
        sz = block.lines[0].size
        near = (0 <= block.bbox[1] - fb[3] < 3 * sz) or (0 <= fb[1] - block.bbox[3] < 3 * sz)
        overlap = min(block.bbox[2], fb[2]) - max(block.bbox[0], fb[0]) > 0
        if not (near and overlap):
            return False
        runs = []
        for l in block.lines:
            if runs:
                tail = runs[-1].text.rstrip()
                if tail.endswith("-") and l.text[:1].islower():
                    runs[-1].text = tail[:-1]
                else:
                    runs[-1].text = tail + " "
            runs.extend(Run(r.text, r.b, r.i, r.sup) for r in l.runs)
        f.caption = runs
        self.discont = True
        return True

    def add_float(self, p: Para):
        self.last_float = p
        if self.cur is not None and not self.cur.text.rstrip().endswith(SENT_END):
            self.pending.append(p)          # parágrafo continua depois da figura: adia
        else:
            self.close()
            self.paras.append(p)

    def add_line(self, l: Line):
        if self.prev is not None and (l.col != self.prev.col or l.y0 < self.prev.y0 - 2):
            self.discont = True
        if self.is_heading(l):
            last = self.paras[-1] if (self.cur is None and self.paras) else None
            lvl = self.level_fn(l.size)
            if (last and last.kind == "h" and last.level == lvl and last.lines and last.lines[-1].page == l.page
                    and 0 <= l.y0 - last.lines[-1].y1 < 1.6 * l.size):
                tail = last.runs[-1].text.rstrip() if last.runs else ""
                if tail.endswith("\xad"):
                    last.runs[-1].text = tail[:-1]
                else:
                    last.runs.append(Run(" "))
                last.runs.extend(l.runs)
                last.lines.append(l)
            else:
                self.close()
                self.paras.append(Para("h", runs=list(l.runs), lines=[l], level=lvl, page=l.page))
            self.prev, self.discont = None, False
            return
        if self.cur is None or self.new_para(self.prev, l):
            self.close()
            self.cur = Para("p", runs=[], lines=[], page=l.page)
            self.cur.runs = [Run(r.text, r.b, r.i, r.sup, r.ref) for r in l.runs]
            self.cur.lines = [l]
        else:
            self.join(l)
        self.prev, self.discont = l, False

    def new_para(self, prev, cur):
        if prev is None:
            return True
        size = cur.size
        if abs(cur.size - prev.size) > max(0.8, 0.08 * prev.size):
            return True
        t = cur.text.lstrip()
        if t[:1] in BULLETS and t[1:2] == " ":
            return True
        ends = prev.text.rstrip().endswith(SENT_END)
        if t.startswith(DIALOGUE) and (ends or prev.text.rstrip().endswith(":")):
            return True
        ts, ps = t.strip(), prev.text.strip()
        if any(rx.match(ts) for rx in (CHAPTER_RE, PART_RE)) or any(rx.match(ps) for rx in (CHAPTER_RE, PART_RE)):
            if len(ts) <= 90 and len(ps) <= 90 or len(ts) <= 30 or len(ps) <= 30:
                return True
        lm, rm = cur.x0 - cur.col_left, cur.col_right - cur.x1
        if lm > 2 * size and rm > 2 * size and abs(lm - rm) < 2 * size and ends:
            return True                                  # linha curta centralizada
        if self.discont:
            if cur.x0 - cur.col_left > 0.6 * size and cur.x0 - cur.col_left < 6 * size:
                return True
            if first_alpha(t).islower():
                return False
            if not ends:
                return False
            return not self.indent_style
        gap = cur.y0 - prev.y1
        if gap > self.gap + 0.6 * size:
            return True
        if re.match(r"^\[\d{1,3}\]\s", t) and ends:
            return True                                   # nova referência bibliográfica
        cl = self.cur.lines if self.cur else []
        if len(cl) >= 2 and cl[1].x0 - cl[0].x0 > 0.6 * size and cur.x0 <= cl[0].x0 + 1:
            return True                                   # recuo deslocado: linha volta à margem = novo item
        if cur.x0 - prev.x0 > 0.6 * size:
            hanging = len(cl) == 1 and prev.col_right - prev.x1 < 1.5 * size and prev.x0 <= prev.col_left + 1
            if not hanging:
                return True
        ref_right = max(l.x1 for l in self.cur.lines) if self.cur and len(self.cur.lines) >= 2 else prev.col_right
        avail = ref_right - prev.x1
        fw = t.split(" ", 1)[0]
        est = (cur.x1 - cur.x0) / max(1, len(cur.text)) * (len(fw) + 1)
        width = max(1.0, prev.col_right - prev.col_left)
        if avail > est + 0.5 * size and (ends or avail > 0.3 * width):
            return True
        return False

    def join(self, l: Line):
        runs = self.cur.runs
        while runs and not runs[-1].text.strip():
            runs.pop()
        if not runs:
            self.cur.runs = [Run(r.text, r.b, r.i, r.sup, r.ref) for r in l.runs]
            self.cur.lines.append(l)
            return
        runs[-1].text = runs[-1].text.rstrip()
        new = [Run(r.text, r.b, r.i, r.sup, r.ref) for r in l.runs]
        while new and not new[0].text.strip():
            new.pop(0)
        if not new:
            return
        new[0].text = new[0].text.lstrip()
        prev_t = runs[-1].text
        nxt = new[0].text
        if prev_t.endswith("\xad"):
            runs[-1].text = prev_t[:-1]
            self.ctx.stats["dehyphenated"] += 1
        elif prev_t[-1:] in "-‐" and prev_t[-2:-1].isdigit() and nxt[:1].isdigit():
            pass                                        # ISBN/telefone partido: une sem espaço
        elif prev_t[-1:] in "-‐" and nxt[:1] in "-‐" and nxt[1:2].isalpha():
            new[0].text = nxt[1:]                       # convenção PT: hífen repetido na linha seguinte
        elif (prev_t[-1:] in "-‐" and len(prev_t) >= 2 and prev_t[-2].isalpha() and nxt[:1].isalpha()):
            left = re.search(r"(\w+)[-‐]$", "".join(r.text for r in runs[-3:]))
            right = re.match(r"\w+", nxt)
            lw = left.group(1) if left else ""
            rw = right.group(0) if right else ""
            joined, hyph = (lw + rw).lower(), (lw + "-" + rw).lower()
            v = self.ctx.vocab
            suffixes = COMPOUND_SUFFIX.get(self.ctx.lang, set())
            parts = self.ctx.parts
            both_words = (len(lw) >= 2 and len(rw) >= 2 and (v[lw.lower()] or parts[lw.lower()])
                          and (v[rw.lower()] or parts[rw.lower()]))
            if (rw.lower() in suffixes or v[hyph] > v[joined] or (rw[:1].isupper() and v[joined] == 0)
                    or (v[joined] == 0 and both_words) or (v[joined] == 0 and not self.ctx.auto_hyphenated)):
                if v[joined] == 0 and v[hyph] == 0 and rw.lower() not in suffixes:
                    self.ctx.uncertain_joins.append(f"p.{l.page+1}: {lw}-|{rw} -> manteve {lw}-{rw}")
            else:
                runs[-1].text = prev_t[:-1]
                if v[joined] == 0:
                    self.ctx.uncertain_joins.append(f"p.{l.page+1}: {lw}-|{rw} -> {lw+rw}")
                self.ctx.stats["dehyphenated"] += 1
        else:
            runs[-1].text += " "
        runs.extend(new)
        self.cur.lines.append(l)


def post_process(paras, ctx, body, maxlvl):
    out = []
    for i, p in enumerate(paras):
        if p.kind == "p":
            t = p.text.strip()
            if not t:
                continue
            # título em negrito do tamanho do corpo
            if (len(p.lines) == 1 and p.lines[0].bold and len(t.split()) <= 12 and not t.endswith((".", ",", ";", ":"))
                    and abs(p.lines[0].size - body) <= 1.2 and i + 1 < len(paras) and paras[i + 1].kind == "p"
                    and not (paras[i + 1].lines and paras[i + 1].lines[0].bold)):
                p.kind, p.level = "h", min(4, maxlvl + 1)
                for r in p.runs:
                    r.b = False
            elif t[:1] in BULLETS and t[1:2] == " ":
                p.kind = "li"
                strip_lead(p.runs, 2)
            else:
                classify_block(p, body)
        if p.kind == "h":
            for r in p.runs:
                r.b = False
            if not p.text.strip():
                continue
        out.append(p)
    # títulos por padrão textual ("CAPÍTULO XII", "PARTE II", "PREFÁCIO") em fonte de corpo
    for p in out:
        t = re.sub(r"\s+", " ", p.text).strip()
        if p.kind in ("p", "h") and len(p.lines) <= 2 and len(t) <= 90:
            short_alone = p.kind == "h" or p.cls in ("center", "right") or (p.lines and p.lines[0].bold) or t.isupper()
            if PART_RE.match(t) and short_alone:
                p.kind, p.level = "h", 1
                p.chapterish = True
            elif CHAPTER_RE.match(t) and short_alone:
                p.kind, p.level = "h", 2
                p.chapterish = True
            elif SECTION_RE.match(t) and short_alone and len(t.split()) <= 4:
                p.kind, p.level = "h", 2
                p.chapterish = True
    merged = []
    for p in out:
        prev = merged[-1] if merged else None
        t = p.text.strip()
        if (prev is not None and getattr(prev, "chapterish", False) and not getattr(prev, "has_sub", False)
                and p.kind in ("h", "p") and not getattr(p, "chapterish", False) and p.page == prev.page
                and len(t.split()) <= 12 and not t.endswith((".", ",", ";"))
                and (p.kind == "h" or p.cls == "center" or all(r.i or r.b or not r.text.strip() for r in p.runs))):
            prev.runs = prev.runs + [Run("\n")] + [Run(r.text, False, r.i, r.sup, r.ref) for r in p.runs]
            prev.has_sub = True
            continue
        merged.append(p)
    out = merged
    for p in out:
        if p.kind == "h":
            p.cls = ""
    # legendas de figura
    res = []
    for p in out:
        if p.kind == "p" and res and res[-1].kind == "img" and res[-1].caption is None and CAPTION_RE.match(p.text.strip()):
            res[-1].caption = p.runs
            continue
        if p.kind == "p" and res and res[-1].kind == "p" and False:
            pass
        res.append(p)
    return res


def strip_lead(runs, n):
    while runs and n > 0:
        t = runs[0].text.lstrip() if n == 2 else runs[0].text
        runs[0].text = t[1:].lstrip()
        n = 0
    return runs


def classify_block(p, body):
    ls = p.lines
    if not ls:
        return
    l0 = ls[0]
    size = l0.size
    width = max(1.0, l0.col_right - l0.col_left)
    if len(ls) >= 2:
        L = min(l.x0 for l in ls[1:])
        R = max(l.x1 for l in ls[:-1])
        if L - l0.col_left > 0.7 * size and l0.col_right - R > 0.5 * size:
            p.cls = "quote"
        elif size < body * 0.93 and L - l0.col_left > 0.8 * size:
            p.cls = "quote"
    else:
        lm, rm = l0.x0 - l0.col_left, l0.col_right - l0.x1
        if lm > 2 * size and rm > 2 * size and abs(lm - rm) < max(2 * size, 0.08 * width):
            p.cls = "center"
        elif rm < 0.5 * size and lm > 0.3 * width:
            p.cls = "right"


def _norm_title(s):
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", s)


def _split_runs(runs, nchars_norm):
    """Divide runs após consumir `nchars_norm` caracteres alfanuméricos (normalizados)."""
    head, tail, count = [], [], 0
    for r in runs:
        if count >= nchars_norm:
            tail.append(r)
            continue
        cut = None
        for k, ch in enumerate(r.text):
            count += len(_norm_title(ch))
            if count >= nchars_norm:
                cut = k + 1
                break
        if cut is None:
            head.append(r)
        else:
            head.append(Run(r.text[:cut], r.b, r.i, r.sup, r.ref))
            rest = r.text[cut:]
            # leva junto pontuação final do título
            m = re.match(r"^[\s.:;,!?»”)\]]*", rest)
            head[-1].text += m.group(0).rstrip()
            rest = rest[m.end():]
            if rest:
                tail.append(Run(rest, r.b, r.i, r.sup, r.ref))
    return head, tail


def apply_outline(paras, toc, ctx):
    """Marca como título os parágrafos que correspondem às entradas do outline (na ordem), com o nível do outline."""
    entries = [(lvl, title.strip(), pg - 1) for lvl, title, pg in toc if title.strip() and pg >= 1]
    cursor, matched = 0, []
    for lvl, title, pg in entries:
        nt = _norm_title(title)
        if not nt:
            continue
        hit = None
        j = cursor
        while j < len(paras) and paras[j].page <= pg + 1:
            p = paras[j]
            if p.kind in ("p", "h") and p.page >= pg - 1:
                np_ = _norm_title(p.text)
                if np_ == nt or (np_.startswith(nt) and len(np_) <= len(nt) * 1.15 + 3):
                    hit = ("whole", j)
                    break
                if nt.startswith(np_) and len(np_) >= 4:
                    # título partido em vários parágrafos: funde os seguintes até casar
                    acc, k = np_, j
                    while k + 1 < len(paras) and len(acc) < len(nt) and paras[k + 1].page <= pg + 1:
                        k += 1
                        acc += _norm_title(paras[k].text)
                    if acc == nt or (acc.startswith(nt) and len(acc) <= len(nt) * 1.15 + 3):
                        hit = ("multi", j, k)
                        break
                if np_.startswith(nt) and len(np_) > len(nt) * 1.15 + 3 and len(nt) >= 6:
                    hit = ("prefix", j)          # título grudado no início de um parágrafo
                    break
                if 0.6 * len(nt) <= len(np_) <= 1.4 * len(nt) and \
                        difflib.SequenceMatcher(None, np_, nt).ratio() >= 0.88:
                    hit = ("whole", j)
                    break
            j += 1
        if hit is None:
            ctx.stats["outline_unmatched"] += 1
            continue
        if hit[0] == "multi":
            _, a, b = hit
            first = paras[a]
            for q in paras[a + 1:b + 1]:
                first.runs = first.runs + [Run(" ")] + q.runs
                first.lines = first.lines + q.lines
            del paras[a + 1:b + 1]
            idx = a
        elif hit[0] == "prefix":
            idx = hit[1]
            p = paras[idx]
            head, tail = _split_runs(p.runs, len(nt))
            body_p = Para("p", runs=tail, lines=p.lines, page=p.page, cls=p.cls)
            p.runs, p.cls = head, ""
            paras.insert(idx + 1, body_p)
        else:
            idx = hit[1]
        p = paras[idx]
        p.kind, p.level, p.cls = "h", min(lvl, 4), ""
        p.title = ""
        p.from_outline = True
        for r in p.runs:
            r.b = False
        matched.append((idx, lvl, title))
        cursor = idx + 1
    ctx.stats["outline_matched"] = len(matched)
    # "Capítulo II" + subtítulo como entrada seguinte na mesma página -> um só título
    out, skip = [], set()
    pos = {id(paras[i]): n for n, (i, _, _) in enumerate(matched)}
    for i, p in enumerate(paras):
        if i in skip:
            continue
        if getattr(p, "from_outline", False) and CHAPTER_RE.match(p.text.strip()) and \
                not CHAPTER_RE.match(p.text.strip()).group(3).strip():
            n = i + 1
            while n < len(paras) and not paras[n].text.strip():
                n += 1
            q = paras[n] if n < len(paras) else None
            if q is not None and getattr(q, "from_outline", False) and q.page == p.page and q.level >= p.level:
                p.runs = p.runs + [Run("\n")] + [Run(r.text, False, r.i, r.sup, r.ref) for r in q.runs]
                p.has_sub = True
                skip.add(n)
        out.append(p)
    # títulos que NÃO estão no outline não podem disputar o nível dos capítulos
    deepest = max((p.level for p in out if getattr(p, "from_outline", False)), default=1)
    for p in out:
        if p.kind == "h" and not getattr(p, "from_outline", False) and p.page > (out[0].page if out else 0) + 1:
            p.level = min(4, max(p.level, deepest + 1)) if p.level <= deepest else p.level
    return out


# --------------------------------------------------------------------------- HTML
def esc(t):
    return html.escape(INVALID_XML.sub("", t), quote=False)


def runs_html(runs, ctx=None, notes_used=None):
    merged = []
    for r in runs:
        if not r.text:
            continue
        key = (r.b, r.i, r.sup, r.ref)
        if merged and merged[-1][0] == key and r.ref is None:
            merged[-1][1].append(r.text)
        else:
            merged.append((key, [r.text]))
    out = []
    for (b, i, sup, ref), parts in merged:
        t = "".join(parts)
        if ref and ctx is not None:
            if notes_used is not None:
                notes_used.append(ref)
            out.append(f'<a epub:type="noteref" class="noteref" id="r{ref}" href="#{ref}"><sup>{esc(t.strip())}</sup></a>')
            continue
        lead = t[: len(t) - len(t.lstrip())]
        trail = t[len(t.rstrip()):]
        core = esc(t.strip())
        if not core:
            out.append(esc(t))
            continue
        if sup:
            core = f"<sup>{core}</sup>"
        if i:
            core = f"<em>{core}</em>"
        if b:
            core = f"<strong>{core}</strong>"
        out.append(esc(lead) + core + esc(trail))
    s = "".join(out)
    s = re.sub(r"[ \t]{2,}", " ", s).strip()
    return re.sub(r"\s*\n\s*", "<br/>", s)


XHTML_HEAD = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">
<head><meta charset="utf-8"/><title>{title}</title><link rel="stylesheet" type="text/css" href="../style.css"/></head>
<body>
"""

CSS = """
body { margin: 0 4%; line-height: 1.45; text-align: justify; hyphens: auto; -webkit-hyphens: auto; widows: 2; orphans: 2; }
p { margin: 0; text-indent: 1.3em; }
h1 + p, h2 + p, h3 + p, h4 + p, figure + p, blockquote + p, ul + p, table + p, p.first, .titlepage p { text-indent: 0; }
h1, h2, h3, h4 { text-align: left; hyphens: none; -webkit-hyphens: none; page-break-after: avoid; break-after: avoid; line-height: 1.25; }
h1 { font-size: 1.6em; margin: 2.5em 0 1.2em; page-break-before: always; break-before: page; }
h2 { font-size: 1.3em; margin: 1.6em 0 0.8em; }
h3 { font-size: 1.12em; margin: 1.3em 0 0.6em; }
h4 { font-size: 1em; margin: 1.2em 0 0.5em; font-style: italic; }
section.titlepage h1, section.titlepage h2 { text-align: center; page-break-before: auto; }
p.center { text-align: center; text-indent: 0; margin: 0.6em 0; }
p.right { text-align: right; text-indent: 0; }
blockquote { margin: 0.8em 1.5em; font-size: 0.95em; }
blockquote p { text-indent: 0; margin-bottom: 0.3em; }
figure { margin: 1em 0; text-align: center; page-break-inside: avoid; break-inside: avoid; }
figure img { max-width: 100%; max-height: 90vh; }
figcaption { font-size: 0.88em; font-style: italic; margin-top: 0.4em; text-align: center; }
table { border-collapse: collapse; margin: 1em auto; font-size: 0.88em; }
td, th { border: 1px solid #999; padding: 0.2em 0.4em; vertical-align: top; text-align: left; }
ul { margin: 0.6em 0 0.6em 1.2em; padding-left: 0.6em; }
li { margin: 0.15em 0; }
a.noteref { text-decoration: none; }
a.noteref sup { font-size: 0.7em; line-height: 0; }
section.footnotes { margin-top: 2.5em; font-size: 0.85em; }
section.footnotes hr { width: 30%; margin-left: 0; }
aside.fn p { text-indent: 0; margin: 0.35em 0; }
img.cover { width: 100%; }
"""


def render_section(paras, ctx, title):
    lang = ctx.lang
    body = []
    notes_used: list[str] = []
    i = 0
    while i < len(paras):
        p = paras[i]
        if p.kind == "h":
            body.append(f'<h{p.level} id="{p.anchor}">{runs_html(p.runs, ctx, notes_used)}</h{p.level}>')
        elif p.kind == "mark":
            body.append(f'<div id="{p.anchor}"></div>')
        elif p.kind == "li":
            items = []
            while i < len(paras) and paras[i].kind == "li":
                items.append(f"<li>{runs_html(paras[i].runs, ctx, notes_used)}</li>")
                i += 1
            body.append("<ul>" + "".join(items) + "</ul>")
            continue
        elif p.kind == "img":
            alt = esc(re.sub(r"\s+", " ", "".join(r.text for r in (p.caption or []))).strip()) or "imagem"
            cap = f"<figcaption>{runs_html(p.caption, ctx, notes_used)}</figcaption>" if p.caption else ""
            body.append(f'<figure><img src="../images/{p.image}" alt="{alt}"/>{cap}</figure>')
        elif p.kind == "table":
            rows = []
            for r in p.rows or []:
                cells = "".join(f"<td>{esc(c or '').replace(chr(10), '<br/>')}</td>" for c in r)
                rows.append(f"<tr>{cells}</tr>")
            body.append("<table>" + "".join(rows) + "</table>")
        else:
            content = runs_html(p.runs, ctx, notes_used)
            if p.cls == "quote":
                quotes = [content]
                while i + 1 < len(paras) and paras[i + 1].kind == "p" and paras[i + 1].cls == "quote":
                    i += 1
                    quotes.append(runs_html(paras[i].runs, ctx, notes_used))
                body.append("<blockquote>" + "".join(f"<p>{q}</p>" for q in quotes) + "</blockquote>")
            elif p.cls:
                body.append(f'<p class="{p.cls}">{content}</p>')
            else:
                body.append(f"<p>{content}</p>")
        i += 1
    return body, notes_used


def notes_html(uids, ctx):
    if not uids:
        return ""
    out = ['<section class="footnotes" epub:type="footnotes"><hr/>']
    for uid in uids:
        n = ctx.notes[uid]
        back = f'<a href="#r{uid}">{esc(n["marker"])}</a> ' if n["ref"] else f'{esc(n["marker"])} '
        out.append(f'<aside epub:type="footnote" class="fn" id="{uid}"><p>{back}{runs_html(n["runs"])}</p></aside>')
    out.append("</section>")
    return "\n".join(out)


# --------------------------------------------------------------------------- EPUB
def write_epub(path: Path, sections, nav_entries, ctx, meta, cover_bytes):
    lang = ctx.lang
    book_id = f"urn:uuid:{uuid.uuid4()}"
    files = {}           # arcname -> (bytes, media-type, props)
    spine = []
    if cover_bytes:
        files["OEBPS/images/cover.jpg"] = (cover_bytes, "image/jpeg", "cover-image")
        cov = XHTML_HEAD.format(lang=lang, title=esc(meta["title"])) + \
            '<section epub:type="cover"><img class="cover" src="../images/cover.jpg" alt="Capa"/></section>\n</body></html>'
        files["OEBPS/text/cover.xhtml"] = (cov.encode("utf-8"), "application/xhtml+xml", "")
        spine.append("text/cover.xhtml")
    for fname, html_body in sections:
        files[f"OEBPS/text/{fname}"] = (html_body.encode("utf-8"), "application/xhtml+xml", "")
        spine.append(f"text/{fname}")
    for name, (data, mt) in ctx.images.items():
        files[f"OEBPS/images/{name}"] = (data, mt, "")
    files["OEBPS/style.css"] = (CSS.encode("utf-8"), "text/css", "")

    # nav.xhtml (aninhado)
    def nav_ol(entries):
        out, stack = [], []
        base = min((e[0] for e in entries), default=1)
        prev = base - 1
        for lvl, title, href in entries:
            lvl = max(base, min(lvl, prev + 1))
            if lvl > prev:
                out.append("<ol>")
                stack.append(lvl)
            else:
                out.append("</li>")
                while stack and stack[-1] > lvl:
                    out.append("</ol></li>")
                    stack.pop()
            out.append(f'<li><a href="{href}">{esc(title)}</a>')
            prev = lvl
        while stack:
            out.append("</li></ol>")
            stack.pop()
        return "".join(out)

    toc_label = {"pt": "Sumário", "es": "Índice", "fr": "Table des matières", "it": "Indice", "de": "Inhalt"}.get(lang, "Contents")
    nav = (f'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml" '
           f'xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}"><head><meta charset="utf-8"/>'
           f'<title>{toc_label}</title><link rel="stylesheet" type="text/css" href="style.css"/></head><body>'
           f'<nav epub:type="toc" id="toc"><h1>{toc_label}</h1>{nav_ol(nav_entries) or "<ol><li><a href=\"" + spine[-1] + "\">Início</a></li></ol>"}</nav>')
    if cover_bytes:
        nav += f'<nav epub:type="landmarks" hidden=""><ol><li><a epub:type="cover" href="text/cover.xhtml">Capa</a></li>' \
               f'<li><a epub:type="bodymatter" href="{spine[1] if len(spine) > 1 else spine[0]}">Início</a></li></ol></nav>'
    nav += "</body></html>"
    files["OEBPS/nav.xhtml"] = (nav.encode("utf-8"), "application/xhtml+xml", "nav")

    # toc.ncx (compatibilidade EPUB2 / Kindle antigo)
    pts, depth = [], 1
    for n, (lvl, title, href) in enumerate(nav_entries, 1):
        pts.append(f'<navPoint id="np{n}" playOrder="{n}"><navLabel><text>{esc(title)}</text></navLabel><content src="{href}"/></navPoint>')
    ncx = (f'<?xml version="1.0" encoding="utf-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
           f'<head><meta name="dtb:uid" content="{book_id}"/><meta name="dtb:depth" content="{depth}"/>'
           f'<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>'
           f'<docTitle><text>{esc(meta["title"])}</text></docTitle><navMap>{"".join(pts)}</navMap></ncx>')
    files["OEBPS/toc.ncx"] = (ncx.encode("utf-8"), "application/x-dtbncx+xml", "")

    ids = {}
    manifest = []
    for n, (arc, (_, mt, props)) in enumerate(files.items()):
        href = arc[len("OEBPS/"):]
        iid = "nav" if props == "nav" else ("ncx" if href == "toc.ncx" else f"i{n}")
        ids[href] = iid
        p = f' properties="{props}"' if props else ""
        manifest.append(f'<item id="{iid}" href="{href}" media-type="{mt}"{p}/>')
    spine_xml = "".join(f'<itemref idref="{ids[h]}"/>' for h in spine)
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    creator = f'<dc:creator>{esc(meta["author"])}</dc:creator>' if meta.get("author") else ""
    cover_meta = '<meta name="cover" content="' + ids.get("images/cover.jpg", "") + '"/>' if cover_bytes else ""
    opf = (f'<?xml version="1.0" encoding="utf-8"?>\n<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
           f'unique-identifier="bookid" xml:lang="{lang}"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:identifier id="bookid">{book_id}</dc:identifier><dc:title>{esc(meta["title"])}</dc:title>{creator}'
           f'<dc:language>{lang}</dc:language><meta property="dcterms:modified">{modified}</meta>{cover_meta}</metadata>'
           f'<manifest>{"".join(manifest)}</manifest><spine toc="ncx">{spine_xml}</spine></package>')
    container = ('<?xml version="1.0" encoding="utf-8"?>\n<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')

    # auto-validação: todo XML precisa ser bem-formado
    for arc, (data, mt, _) in list(files.items()) + [("OEBPS/content.opf", (opf.encode(), "", "")), ("x", (container.encode(), "", ""))]:
        if mt in ("application/xhtml+xml", "application/x-dtbncx+xml", ""):
            try:
                ET.fromstring(data)
            except ET.ParseError as e:
                raise RuntimeError(f"XML inválido em {arc}: {e}")

    tmp = path.with_suffix(".epub.part")
    with zipfile.ZipFile(tmp, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        for arc, (data, mt, _) in files.items():
            ct = zipfile.ZIP_STORED if mt.startswith("image/") and mt != "image/png" else zipfile.ZIP_DEFLATED
            z.writestr(arc, data, compress_type=ct)
    tmp.replace(path)


# --------------------------------------------------------------------------- pipeline
def parse_range(spec, n):
    if not spec:
        return list(range(n))
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        a = int(a) if a else 1
        b = int(b) if b else (n if _ else a)
        out.extend(range(a - 1, min(b, n)))
    return out


def convert_native(pdf: Path, out: Path, opts) -> dict:
    t0 = time.time()
    ctx = Ctx(opts)
    doc = pymupdf.open(pdf)
    if doc.needs_pass and not doc.authenticate(opts.password or ""):
        raise RuntimeError("PDF protegido por senha (use --password)")
    pnos = parse_range(opts.pages, doc.page_count)
    pages = []
    for k, pno in enumerate(pnos):
        pages.append(extract_page(doc, pno, ctx))
        if (k + 1) % 50 == 0:
            log(f"   ... {k+1}/{len(pnos)} páginas lidas")
    # descarta imagens que se repetem em muitas páginas (logos, vinhetas)
    rep = {n for n, c in ctx.img_pages.items() if c >= 3 and c >= 0.1 * len(pages)}
    if rep:
        for pg in pages:
            pg.blocks = [b for b in pg.blocks if not (b.kind == "image" and b.image in rep)]
        for n in rep:
            ctx.images.pop(n, None)
        ctx.stats["repeated_images_removed"] = len(rep)

    body = body_size(pages)
    ctx.lang = opts.lang or detect_lang(pages)
    remove_headers_footers(pages, body, ctx)
    if not opts.keep_toc:
        detect_toc_pages(pages, ctx)
    pages_use = [p for p in pages if not p.skip]
    for pg in pages_use:
        pg.blocks = order_blocks(pg, body)
    extract_footnotes(pages_use, body, ctx)
    build_vocab(pages_use, ctx)
    for pg in pages_use:
        assign_column_margins(pg, body)

    # métricas globais
    gaps, indented, total = [], 0, 0
    for pg in pages_use:
        for b in pg.blocks:
            if b.kind != "text":
                continue
            for a, c in zip(b.lines, b.lines[1:]):
                if abs(a.size - body) < 1 and abs(c.size - body) < 1 and c.y0 >= a.y1 - 3:
                    gaps.append(c.y0 - a.y1)
            for l in b.lines:
                if abs(l.size - body) < 1:
                    total += 1
                    if 0.6 * body < l.x0 - l.col_left < 4 * body:
                        indented += 1
    typical_gap = statistics.median(gaps) if gaps else 0.2 * body
    indent_style = total > 0 and indented / total > 0.03
    level_fn = heading_levels(pages_use, body)

    outline_marks = {}
    toc = doc.get_toc(simple=True)

    bld = Builder(ctx, body, level_fn, typical_gap, indent_style)
    for pg in pages_use:
        bld.page_break()
        for b in pg.blocks:
            if b.kind == "text":
                if bld.try_caption(b):
                    continue
                for l in b.lines:
                    bld.add_line(l)
            elif b.kind in ("image", "figure"):
                fp = Para("img", image=b.image, page=pg.pno)
                fp.bbox = b.bbox
                bld.add_float(fp)
            elif b.kind == "table":
                bld.add_float(Para("table", rows=b.rows, page=pg.pno))
    bld.close()
    paras = bld.paras
    maxlvl = max((p.level for p in paras if p.kind == "h"), default=1)
    paras = post_process(paras, ctx, body, maxlvl)

    # marcadores (bookmarks) do PDF = estrutura autoritativa, quando existem
    if toc and len(toc) >= 3 and not getattr(opts, "ignore_outline", False):
        paras = apply_outline(paras, toc, ctx)
    # sem títulos detectados? usa os marcadores (outline) do PDF
    if not any(p.kind == "h" for p in paras) and toc:
        first_by_page = {}
        for lvl, title, pnum in toc:
            if lvl == 1 and pnum >= 1:
                first_by_page.setdefault(pnum - 1, title)
        new = []
        seen = set()
        for p in paras:
            if p.page in first_by_page and p.page not in seen:
                seen.add(p.page)
                new.append(Para("mark", title=first_by_page[p.page], page=p.page, level=1))
            new.append(p)
        paras = new

    # divide em capítulos
    hl = [p.level for p in paras if p.kind in ("h", "mark")]
    lvl_count = Counter(hl)
    frequent = [lv for lv in sorted(lvl_count) if lvl_count[lv] >= 3]
    split_lvl = frequent[0] if frequent else (min(hl) if hl else 1)
    if hl and hl.count(split_lvl) == 1 and len(set(hl)) > 1 and paras and \
            [p for p in paras if p.kind in ("h", "mark")][0].page == pages_use[0].pno:
        # único título no topo (folha de rosto) -> divide no nível seguinte
        split_lvl = sorted(set(hl))[1]
    chapters: list[list[Para]] = [[]]
    for p in paras:
        if p.kind in ("h", "mark") and p.level <= split_lvl and chapters[-1] and any(q.kind != "mark" for q in chapters[-1]):
            chapters.append([])
        chapters[-1].append(p)
    # quebra capítulos enormes (leitores ficam lentos)
    final = []
    for ch in chapters:
        cur, size = [], 0
        for p in ch:
            cur.append(p)
            size += len(p.text) + 60
            if size > 120_000 and p.kind == "p":
                final.append(cur)
                cur, size = [], 0
        if cur:
            final.append(cur)

    # metadados
    md = doc.metadata or {}
    first_h = next((p.text.strip() for p in paras if p.kind == "h"), "")
    md_title = (md.get("title") or "").strip()
    if re.match(r"(?i)^(microsoft word|untitled|sem título|document\d*)\b", md_title) or md_title.lower().endswith((".doc", ".docx", ".pdf")):
        md_title = ""
    meta = {"title": opts.title or md_title or first_h or pdf.stem, "author": opts.author or (md.get("author") or "").strip()}

    sections, nav_entries = [], []
    note_page_owner = {}
    title_block = set()
    for p in paras:                       # bloco de títulos contíguo no topo da 1ª página (folha de rosto)
        if p.page != pages_use[0].pno or p.kind not in ("h", "mark"):
            break
        title_block.add(id(p))
    for idx, ch in enumerate(final):
        fname = f"ch{idx+1:03d}.xhtml"
        for p in ch:
            note_page_owner[p.page] = idx
            if p.kind in ("h", "mark"):
                bld.anchor_seq += 1
                p.anchor = f"h{bld.anchor_seq}"
                title = p.title or re.sub(r"\s+", " ", re.sub(r"(\s*\n\s*)+", " — ", p.text.strip())).strip()
                if id(p) in title_block and nav_entries:
                    continue
                if (p.level <= 3 or (p.level == 4 and getattr(p, "from_outline", False))) and title:
                    nav_entries.append((p.level, title[:200], f"text/{fname}#{p.anchor}"))
    rendered = []
    all_used = set()
    for idx, ch in enumerate(final):
        bodyparts, used = render_section(ch, ctx, meta["title"])
        all_used.update(used)
        rendered.append((bodyparts, used))
    orphans = defaultdict(list)
    for uid, n in ctx.notes.items():
        if uid not in all_used:
            orphans[note_page_owner.get(n["page"], len(final) - 1)].append(uid)
    for idx, (bodyparts, used) in enumerate(rendered):
        fname = f"ch{idx+1:03d}.xhtml"
        first = final[idx][0] if final[idx] else None
        is_title = idx == 0 and first is not None and first.page == pages_use[0].pno and split_lvl > 1
        cls = ' class="titlepage"' if is_title else ""
        htm = XHTML_HEAD.format(lang=ctx.lang, title=esc(meta["title"])) + f"<section{cls}>\n" + "\n".join(bodyparts) + \
            "\n</section>\n" + notes_html(used + orphans.get(idx, []), ctx) + "\n</body>\n</html>\n"
        sections.append((fname, htm))

    cover = None
    if opts.cover:
        cover = Path(opts.cover).read_bytes()
    elif not opts.no_cover:
        p0 = doc[pnos[0]]
        zoom = 1400 / max(1, p0.rect.width)
        cover = p0.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("jpeg", jpg_quality=85)

    write_epub(out, sections, nav_entries, ctx, meta, cover)
    ctx.stats.update({"pages": len(pnos), "chapters": len(final), "paragraphs": sum(1 for p in paras if p.kind == "p"),
                      "headings": sum(1 for p in paras if p.kind == "h"), "images": len(ctx.images)})
    report = {"input": str(pdf), "output": str(out), "seconds": round(time.time() - t0, 1), "lang": ctx.lang,
              "title": meta["title"], "author": meta["author"], "body_font_size": body, **ctx.stats,
              "warnings": ctx.warnings, "uncertain_dehyphenations": ctx.uncertain_joins}
    if any(p.scanned for p in pages) and not (opts.ocr and tesseract_ok()):
        report["warnings"].append(f"{sum(p.scanned for p in pages)} página(s) escaneada(s) incluída(s) como imagem — "
                                  "instale o Tesseract e use --ocr para extrair texto.")
    return report


def convert_calibre(pdf: Path, out: Path, opts) -> dict:
    exe = shutil.which("ebook-convert") or r"C:\Program Files\Calibre2\ebook-convert.exe"
    cmd = [exe, str(pdf), str(out), "--enable-heuristics", "--unwrap-factor", "0.45", "--epub-version", "3",
           "--chapter", "//*[name()='h1' or name()='h2']"]
    if opts.title:
        cmd += ["--title", opts.title]
    if opts.author:
        cmd += ["--authors", opts.author]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-800:])
    return {"input": str(pdf), "output": str(out), "engine": "calibre"}


def convert(pdf: Path, outdir: Path | None, opts) -> dict:
    out = (outdir or pdf.parent) / (pdf.stem + ".epub")
    out.parent.mkdir(parents=True, exist_ok=True)
    log(f"-> {pdf.name}")
    rep = convert_calibre(pdf, out, opts) if opts.engine == "calibre" else convert_native(pdf, out, opts)
    if opts.report and opts.engine == "native":
        rp = out.with_suffix(".relatorio.txt")
        lines = [f"{k}: {v}" for k, v in rep.items() if k not in ("warnings", "uncertain_dehyphenations")]
        lines += ["", "AVISOS:"] + [f"  {w}" for w in rep["warnings"]]
        lines += ["", "HIFENS DE FIM DE LINHA INCERTOS (decisão sem confirmação no vocabulário do livro — confira):"]
        lines += [f"  {u}" for u in rep["uncertain_dehyphenations"]]
        rp.write_text("\n".join(lines), encoding="utf-8")
    summary = {k: rep[k] for k in ("pages", "chapters", "paragraphs", "headings", "footnotes", "images", "seconds") if k in rep}
    log(f"   OK {out}  {summary}")
    for w in rep.get("warnings", []):
        log(f"   ! {w}")
    return rep


def collect(paths):
    out = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            out.extend(sorted(p.rglob("*.pdf")))
        elif p.suffix.lower() == ".pdf":
            out.append(p)
    return out


def watch(folder: Path, outdir: Path | None, opts):
    log(f"Vigiando {folder} (Ctrl+C para sair)...")
    sizes, failed = {}, {}
    while True:
        for pdf in sorted(folder.glob("*.pdf")):
            target = (outdir or pdf.parent) / (pdf.stem + ".epub")
            if target.exists() and target.stat().st_mtime >= pdf.stat().st_mtime:
                continue
            if failed.get(pdf) == pdf.stat().st_mtime:      # já falhou e não mudou desde então
                continue
            sz = pdf.stat().st_size
            if sizes.get(pdf) != sz:            # espera o arquivo terminar de ser copiado
                sizes[pdf] = sz
                continue
            try:
                convert(pdf, outdir, opts)
            except Exception as e:
                log(f"   ERRO em {pdf.name}: {e}")
                failed[pdf] = pdf.stat().st_mtime
        time.sleep(3)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Converte PDF em EPUB3 reconstruindo o fluxo do texto.")
    ap.add_argument("inputs", nargs="*", help="arquivos PDF ou pastas")
    ap.add_argument("-o", "--outdir", help="pasta de saída (padrão: ao lado do PDF)")
    ap.add_argument("--watch", metavar="PASTA", help="modo automático: converte todo PDF novo que cair na pasta")
    ap.add_argument("--engine", choices=["native", "calibre"], default="native")
    ap.add_argument("--title")
    ap.add_argument("--author")
    ap.add_argument("--lang", help="código do idioma (pt, en, fr...). Padrão: detecta")
    ap.add_argument("--cover", help="imagem de capa (padrão: 1ª página renderizada)")
    ap.add_argument("--no-cover", action="store_true")
    ap.add_argument("--pages", help="intervalo de páginas, ex.: 1-50,60")
    ap.add_argument("--ocr", action="store_true", help="OCR em páginas escaneadas (requer Tesseract)")
    ap.add_argument("--ocr-lang", default="por+eng")
    ap.add_argument("--ignore-outline", action="store_true", help="não usar os marcadores do PDF como estrutura")
    ap.add_argument("--keep-toc", action="store_true", help="não remover páginas de sumário do PDF")
    ap.add_argument("--no-tables", dest="tables", action="store_false")
    ap.add_argument("--no-figures", dest="figures", action="store_false")
    ap.add_argument("--hf-zone", type=float, default=0.12, help="faixa (fração da altura) onde procurar cabeçalho/rodapé")
    ap.add_argument("--password")
    ap.add_argument("--debug", action="store_true", help="mostra o traceback completo em caso de erro")
    ap.add_argument("--report", action="store_true", help="grava <livro>.relatorio.txt com estatísticas e pontos a conferir")
    opts = ap.parse_args(argv)
    outdir = Path(opts.outdir) if opts.outdir else None
    if opts.watch:
        watch(Path(opts.watch), outdir, opts)
        return
    pdfs = collect(opts.inputs)
    if not pdfs:
        ap.error("nenhum PDF informado")
    fails = 0
    for pdf in pdfs:
        try:
            convert(pdf, outdir, opts)
        except Exception as e:
            fails += 1
            log(f"   ERRO em {pdf.name}: {type(e).__name__}: {e}")
            if opts.debug:
                import traceback
                traceback.print_exc()
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
