"""
tldr_card.py — render the brief's TL;DR as a PNG card, for posting to Discord above the text.

Why this exists: the brief lands in Discord as 3-4 walls of text. Most readers want the four
things that matter in five seconds and will scroll for the rest. This makes that top layer a
single image so it's readable on a phone without expanding anything.

Two halves, deliberately:

  THE JUDGEMENT comes from the brief writer. Put a `<!-- CARD ... -->` block in the brief md,
  one point per line as `Lead | supporting clause`. 3 or 4 lines. Nothing here tries to work
  out what mattered today — that's the one thing a script can't do.

  THE NUMBERS come from data/{date}.json, the same block the brief was written from. Never
  retyped, so the card can't disagree with the brief. If a number is missing it is left out
  rather than guessed (project rule: missing data says missing).

If the CARD block is absent the card still renders — it falls back to the bold lead-in of the
first bullet of each section, which is usually serviceable and always obviously worse.

Usage:
    python tldr_card.py briefs/2026-08-03.md            # -> briefs/2026-08-03.png
    python tldr_card.py briefs/2026-08-03.md --post     # render, then post card + brief
    python tldr_card.py briefs/2026-08-03.md -o x.png
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent

# Palette — dark chart surface + reserved status colors. Up/down are the status good/critical
# steps, which clear 3:1 on this surface; they are never used for anything but direction.
BG          = "#1a1a19"
CARD        = "#232322"
INK         = "#ffffff"
INK_2       = "#c3c2b7"
INK_3       = "#807f78"
RULE        = "#383835"
UP          = "#0ca30c"
DOWN        = "#d03b3b"
WARN        = "#fab219"
ACCENT      = "#3987e5"

W = 1100
PAD = 56

# Segoe UI first (this is a Windows box), DejaVu as the portable fallback so the card still
# renders if the pipeline ever runs in CI on Linux.
_FONT_DIRS = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu")]
_FACES = {
    "bold":    ["segoeuib.ttf", "DejaVuSans-Bold.ttf"],
    "semi":    ["seguisb.ttf", "DejaVuSans-Bold.ttf"],
    "regular": ["segoeui.ttf", "DejaVuSans.ttf"],
    "mono":    ["consolab.ttf", "DejaVuSansMono-Bold.ttf"],
}


def font(face: str, size: int) -> ImageFont.FreeTypeFont:
    for name in _FACES[face]:
        for d in _FONT_DIRS:
            p = d / name
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


# ---------------------------------------------------------------- parsing the brief

def parse_brief(md: str) -> dict:
    """Pull the title date, the TL;DR sentence and the card points out of the brief markdown."""
    out: dict = {"date_line": "", "stamp": "", "headline": "", "points": [], "gate": ""}

    m = re.search(r"^#\s*.*?Market Brief\s*[—-]\s*(.+)$", md, re.M)
    if m:
        out["date_line"] = m.group(1).strip()
    m = re.search(r"^\*Data pulled\s+(.+?)\*$", md, re.M)
    if m:
        out["stamp"] = m.group(1).strip()

    m = re.search(r"\*\*TL;DR:\*\*\s*(.+?)(?:\n\n|\n##)", md, re.S)
    if m:
        out["headline"] = _clean(m.group(1))

    m = re.search(r"<!--\s*CARD\s*(.*?)-->", md, re.S)
    out["from_card_block"] = bool(m)
    if m:
        for line in m.group(1).strip().splitlines():
            line = line.strip().lstrip("-").strip()
            if not line:
                continue
            if line.lower().startswith("gate:"):
                out["gate"] = _clean(line.split(":", 1)[1])
                continue
            lead, _, rest = line.partition("|")
            out["points"].append((_clean(lead), _clean(rest)))
    else:
        out["points"] = _fallback_points(md)

    return out


def _fallback_points(md: str) -> list[tuple[str, str]]:
    """No CARD block: take the bold lead-in of the first bullet under each section."""
    pts: list[tuple[str, str]] = []
    for sec in re.split(r"^##\s+", md, flags=re.M)[1:]:
        b = re.search(r"^-\s+(.+?)(?:\n(?!\s)|\Z)", sec, re.M | re.S)
        if not b:
            continue
        body = _clean(b.group(1))
        lead = re.match(r"\*\*(.+?)\*\*", b.group(1))
        if lead:
            head = _clean(lead.group(1)).rstrip(".:—- ")
            rest = body[len(_clean(lead.group(1))):].lstrip(" —-:")
        else:
            head, _, rest = body.partition(" — ")
        pts.append((head, rest))
    return pts[:4]


def _clean(s: str) -> str:
    """Markdown emphasis and code ticks off; whitespace collapsed. The card has one voice."""
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"\*(.+?)\*", r"\1", s)
    s = s.replace("`", "")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- the number strip

def _pct(v) -> tuple[str, str]:
    return (f"{v:+.2f}%", UP if v >= 0 else DOWN)


def build_stats(date: str) -> list[list[tuple]]:
    """Two rows of (label, value, color) read straight from the gather block. Anything the
    block doesn't have is dropped, never defaulted."""
    f = ROOT / "data" / f"{date}.json"
    if not f.exists():
        return []
    d = json.loads(f.read_text(encoding="utf-8"))

    row1: list[tuple] = []
    idx = d.get("index_recap") or {}
    for key, label in (("ES", "S&P"), ("NQ", "NASDAQ"), ("YM", "DOW"), ("RTY", "RUSSELL")):
        v = (idx.get(key) or {}).get("chg_pct")
        if v is not None:
            row1.append((label, *_pct(v)))

    row2: list[tuple] = []
    macro = d.get("macro_pulse") or {}
    vix = macro.get("^VIX") or {}
    if vix.get("last") is not None:
        chg = vix.get("chg")
        col = INK if chg is None else (DOWN if chg > 0 else UP)   # a VIX bid is the risk-off side
        row2.append(("VIX", f"{vix['last']:.2f}", col))
    ten = ((macro.get("yields") or {}).get("tenors") or {}).get("10Y") or {}
    if ten.get("last") is not None:
        bp = ten.get("chg")
        # The move rides in the label, not the value — a monospace value column stays aligned.
        label = "US 10Y" + (f"   {bp * 100:+.0f}BP" if bp is not None else "")
        row2.append((label, f"{ten['last']:.2f}%", UP if (bp or 0) <= 0 else DOWN))
    oil = (idx.get("CL=F") or {}).get("chg_pct")
    if oil is not None:
        row2.append(("WTI CRUDE", *_pct(oil)))

    spy = ((d.get("gex") or {}).get("SPY") or {}).get("near_term") or {}
    # An incomplete chain UNDERSTATES gamma, and an understated GEX looks exactly like a flip
    # to negative — so the tile would be asserting a regime the brief is not allowed to write.
    # Print N/A rather than let the card contradict the text it was rendered from.
    if spy.get("data_warning"):
        row2.append(("SPY GAMMA", "N/A", INK_3))
    elif spy.get("regime"):
        pos = spy["regime"] == "positive"
        row2.append(("SPY GAMMA", "POSITIVE" if pos else "NEGATIVE", UP if pos else WARN))

    return [r for r in (row1, row2) if r]


def build_gate(date: str) -> str:
    """One line for the footer: today's scheduled events, from the event gate.

    ⚠️ The wording here is deliberate. `today_is_t1: False` means "not a prop-firm restricted
    day" — it has NEVER meant "nothing happens today", and the gate genuinely cannot see ISM,
    Fed speakers or one-off prints (see CURRENT-BEHAVIOUR.md §8). A card that said "no events"
    on an ISM morning would be the 2026-07-30 mistake, published as a graphic. So the fallback
    states only what the field actually asserts, and the writer can override it with a
    `Gate: ...` line in the brief's CARD block."""
    f = ROOT / "data" / f"{date}.json"
    if not f.exists():
        return ""
    g = (json.loads(f.read_text(encoding="utf-8")).get("event_gate") or {})
    if g.get("error") or "today_is_t1" not in g:
        return "EVENT GATE BLIND — read the brief's event section"
    names = [e.get("name", "") for e in (g.get("today_events") or [])]
    names += [e.get("name", "") for e in (g.get("today_tier2") or [])]
    names = [n for n in names if n]
    if g.get("today_is_t1"):
        return "T1 DAY — " + ", ".join(names)
    return ("TODAY: " + ", ".join(names)) if names else "Not a T1 day"


# ---------------------------------------------------------------- drawing

def wrap(draw, text: str, f, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = w if not cur else cur + " " + w
        if draw.textlength(t, font=f) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render(brief_path: Path, out: Path | None = None) -> Path:
    md = brief_path.read_text(encoding="utf-8")
    info = parse_brief(md)
    date = brief_path.stem[:10]
    stats = build_stats(date)
    gate = info["gate"] or build_gate(date)
    points = info["points"][:4]

    # A thin card must complain. Rendering 1 point from a bad fallback looks like a success —
    # the file is written, the exit code is 0 — and that's how a weak card gets posted.
    if not info["from_card_block"]:
        print(f"  WARN no <!-- CARD --> block in {brief_path.name} — fell back to section "
              f"lead-ins ({len(points)} point(s)); the card will be generic", flush=True)
    if len(points) < 3:
        print(f"  WARN only {len(points)} point(s) on the card — expected 3 or 4", flush=True)
    if not stats:
        print(f"  WARN no data/{date}.json — card has no number strip", flush=True)

    f_kicker = font("semi", 20)
    f_date   = font("bold", 40)
    f_stamp  = font("regular", 20)
    f_head   = font("semi", 27)
    f_num    = font("bold", 30)
    f_lead   = font("bold", 25)
    f_body   = font("regular", 23)
    f_label  = font("semi", 17)
    f_stat   = font("mono", 31)
    f_foot   = font("regular", 19)

    inner = W - 2 * PAD
    m = Image.new("RGB", (W, 40), BG)
    md_ = ImageDraw.Draw(m)

    # Measure first so the canvas is exactly as tall as the content — a fixed height either
    # clips a long day or leaves a dead band on a short one.
    y = PAD + 34 + 48 + 30
    head_lines = wrap(md_, info["headline"], f_head, inner) if info["headline"] else []
    y += len(head_lines) * 38 + (26 if head_lines else 0)
    y += 26

    laid: list[tuple] = []
    for lead, rest in points:
        tx = PAD + 52
        lw = inner - 52
        lead_lines = wrap(md_, lead, f_lead, lw)
        rest_lines = wrap(md_, rest, f_body, lw) if rest else []
        laid.append((y, lead_lines, rest_lines))
        y += len(lead_lines) * 33 + len(rest_lines) * 31 + 24
    y += 12

    stat_top = y
    y += 26 + len(stats) * 76
    # Footer is one line if the gate text and the disclaimer both fit, two if not — a long
    # gate line must never be truncated or overprinted, it's the part that carries risk.
    disclaimer = "Situational awareness — not financial advice"
    foot_stacked = bool(gate) and (
        md_.textlength(gate, font=f_foot) + md_.textlength(disclaimer, font=f_foot) + 40 > inner
    )
    y += 22 + 30 + (28 if foot_stacked else 0) + PAD - 10
    H = int(y)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([14, 14, W - 15, H - 15], radius=18, fill=CARD)
    d.rectangle([14, 14, 14 + 6, H - 15], fill=ACCENT)          # spine

    # --- header
    yy = PAD
    d.text((PAD, yy), "MARKET BRIEF", font=f_kicker, fill=ACCENT)
    yy += 34
    d.text((PAD, yy), info["date_line"] or date, font=f_date, fill=INK)
    if info["stamp"]:
        d.text((W - PAD, yy + 16), info["stamp"], font=f_stamp, fill=INK_3, anchor="ra")
    yy += 48 + 30

    # --- the one-sentence take
    for ln in head_lines:
        d.text((PAD, yy), ln, font=f_head, fill=INK_2)
        yy += 38
    if head_lines:
        yy += 26
    d.line([PAD, yy - 14, W - PAD, yy - 14], fill=RULE, width=1)

    # --- the points
    for i, (top, lead_lines, rest_lines) in enumerate(laid, 1):
        d.text((PAD + 4, top - 2), str(i), font=f_num, fill=ACCENT)
        ty = top
        for ln in lead_lines:
            d.text((PAD + 52, ty), ln, font=f_lead, fill=INK)
            ty += 33
        for ln in rest_lines:
            d.text((PAD + 52, ty), ln, font=f_body, fill=INK_2)
            ty += 31

    # --- number strip
    yy = stat_top
    d.line([PAD, yy, W - PAD, yy], fill=RULE, width=1)
    yy += 26
    for row in stats:
        cw = inner / len(row)
        for i, (label, val, col) in enumerate(row):
            x = PAD + i * cw
            d.text((x, yy), label, font=f_label, fill=INK_3)
            d.text((x, yy + 24), val, font=f_stat, fill=col)
        yy += 76

    # --- footer
    yy += 22
    d.line([PAD, yy - 12, W - PAD, yy - 12], fill=RULE, width=1)
    if gate:
        hot = "T1 DAY" in gate or "BLIND" in gate
        d.text((PAD, yy), gate, font=f_foot, fill=WARN if hot else INK_3)
    d.text((W - PAD, yy + (28 if foot_stacked else 0)), disclaimer,
           font=f_foot, fill=INK_3, anchor="ra")

    out = out or brief_path.with_suffix(".png")
    img.save(out)
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print(__doc__)
        sys.exit(1)
    src = Path(args[0])
    dst = Path(args[1]) if len(args) > 1 else None
    if "-o" in sys.argv:
        dst = Path(sys.argv[sys.argv.index("-o") + 1])
    p = render(src, dst)
    print(f"wrote {p}  ({p.stat().st_size // 1024} KB)")

    if "--post" in sys.argv:
        # Card first, then the brief text under it — the image is the lede, not an appendix.
        import post_discord
        print(post_discord.post_image(p))
        print(post_discord.post(src.read_text(encoding="utf-8")))
