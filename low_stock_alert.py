import sys
import os
import re
import base64
import hashlib
import smtplib
import struct
import zlib
import xmlrpc.client
from collections import defaultdict
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

print("🟢 Starting ELITE low-stock alert script...", flush=True)

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────
ODOO_URL      = os.environ.get("ODOO_URL", "https://your-odoo-url.com")
ODOO_DB       = os.environ.get("ODOO_DB")
ODOO_USER     = os.environ.get("ODOO_USER")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD")

SMTP_HOST     = os.environ.get("SMTP_HOST")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER     = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM     = os.environ.get("SMTP_FROM")

# ── Recipients ───────────────────────────────────────────────────────────
# List every address here. All will appear in the To: header and each
# will receive the email. Add/remove lines freely.
SMTP_TO = [
    os.environ.get("SMTP_TO_1", "recipient1@example.com"),
    os.environ.get("SMTP_TO_2", "recipient2@example.com"),
    os.environ.get("SMTP_TO_3", "recipient3@example.com"),
    os.environ.get("SMTP_TO_4", "recipient4@example.com"),
    os.environ.get("SMTP_TO_5", "recipient5@example.com"),
]
# Strip blanks (in case env vars aren't all set)
SMTP_TO = [a.strip() for a in SMTP_TO if a and a.strip()]

LOW_STOCK_THRESHOLD = 5

# Quantity fields to try in priority order
QTY_FIELD_CANDIDATES = [
    "x_avl_custom",
    "x_studio_related_field_6v_1jolles4p",
    "qty_available",
]

# ── Parent-category split ────────────────────
SPLIT_GROUPS = [
    ("Imported", "imported"),
    ("Indian",   "indian"),
]

# Deterministic colour palette for letter avatars (no product image)
AVATAR_COLORS = [
    "#b8860b", "#8b6914", "#a0522d", "#6b4226",
    "#2c4a7c", "#1a3a5c", "#2d6a4f", "#1b4332",
]

# Image display size — 44 → 66px (50% larger)
IMG_SIZE = 66
# Cell width: image + side padding (20 left + 6 right = 26, cell = IMG_SIZE + 26)
IMG_CELL_WIDTH = IMG_SIZE + 26


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def avatar_color(name: str) -> str:
    idx = int(hashlib.md5(name.encode()).hexdigest(), 16) % len(AVATAR_COLORS)
    return AVATAR_COLORS[idx]


# ── 5×7 bitmap glyphs for A-Z and ? (each column is a bitmask, top=LSB) ──
_GLYPHS: dict[str, list[int]] = {
    "A": [0x7E,0x11,0x11,0x11,0x7E], "B": [0x7F,0x49,0x49,0x49,0x36],
    "C": [0x3E,0x41,0x41,0x41,0x22], "D": [0x7F,0x41,0x41,0x22,0x1C],
    "E": [0x7F,0x49,0x49,0x49,0x41], "F": [0x7F,0x09,0x09,0x09,0x01],
    "G": [0x3E,0x41,0x49,0x49,0x7A], "H": [0x7F,0x08,0x08,0x08,0x7F],
    "I": [0x00,0x41,0x7F,0x41,0x00], "J": [0x20,0x40,0x41,0x3F,0x01],
    "K": [0x7F,0x08,0x14,0x22,0x41], "L": [0x7F,0x40,0x40,0x40,0x40],
    "M": [0x7F,0x02,0x0C,0x02,0x7F], "N": [0x7F,0x04,0x08,0x10,0x7F],
    "O": [0x3E,0x41,0x41,0x41,0x3E], "P": [0x7F,0x09,0x09,0x09,0x06],
    "Q": [0x3E,0x41,0x51,0x21,0x5E], "R": [0x7F,0x09,0x19,0x29,0x46],
    "S": [0x46,0x49,0x49,0x49,0x31], "T": [0x01,0x01,0x7F,0x01,0x01],
    "U": [0x3F,0x40,0x40,0x40,0x3F], "V": [0x1F,0x20,0x40,0x20,0x1F],
    "W": [0x3F,0x40,0x38,0x40,0x3F], "X": [0x63,0x14,0x08,0x14,0x63],
    "Y": [0x07,0x08,0x70,0x08,0x07], "Z": [0x61,0x51,0x49,0x45,0x43],
    "?": [0x02,0x01,0x51,0x09,0x06],
}


def _make_png(pixels: list[list[tuple[int,int,int]]]) -> bytes:
    """
    Encode a 2-D list of (R,G,B) tuples as a valid PNG using only
    struct + zlib from the stdlib. No external dependencies.
    """
    h = len(pixels)
    w = len(pixels[0]) if h else 0

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    # IHDR
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB

    # IDAT — one filter byte (0 = None) per row, then raw RGB bytes
    raw = b"".join(
        b"\x00" + bytes(c for px in row for c in px)
        for row in pixels
    )
    idat = zlib.compress(raw, 9)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def letter_avatar_png_bytes(name: str) -> bytes:
    """
    Pure-stdlib PNG avatar — gradient background + bold letter.
    No Pillow, no SVG, 100 % Gmail-safe.
    """
    letter = (_GLYPHS.get((name or "?")[0].upper()) and (name or "?")[0].upper()) or "?"
    glyph  = _GLYPHS.get(letter, _GLYPHS["?"])

    hex_color = avatar_color(name)
    h = hex_color.lstrip("#")
    top = tuple(int(h[i:i+2], 16) for i in (0, 2, 4))   # top-of-gradient
    bot = (10, 10, 15)                                     # #0a0a0f
    gold   = (232, 201, 109)                               # #e8c96d letter
    border = (201, 168, 76)                                # #c9a84c 1-px rim

    S   = IMG_SIZE       # e.g. 66
    RX  = max(8, S // 8) # corner radius in pixels

    # ── Build pixel grid ──────────────────────────────────────────────────
    pixels: list[list[tuple[int,int,int]]] = []

    # Glyph scale: each bit → SCALE×SCALE block; glyph is 5 cols × 7 rows
    SCALE = max(1, S // 14)      # ~4–5 px per bit at 66 px
    GW    = 5 * SCALE            # glyph pixel width
    GH    = 7 * SCALE            # glyph pixel height
    ox    = (S - GW) // 2        # x offset to centre
    oy    = (S - GH) // 2        # y offset to centre

    def in_glyph(x: int, y: int) -> bool:
        """True if pixel (x,y) should be lit gold."""
        gx = (x - ox) // SCALE
        gy = (y - oy) // SCALE
        if 0 <= gx < 5 and 0 <= gy < 7:
            return bool(glyph[gx] & (1 << gy))
        return False

    def in_rounded_rect(x: int, y: int) -> bool:
        """True if (x,y) is inside the rounded rectangle."""
        cx = min(x, S - 1 - x)
        cy = min(y, S - 1 - y)
        if cx >= RX or cy >= RX:
            return True
        dx, dy = RX - cx - 1, RX - cy - 1
        return dx * dx + dy * dy <= (RX - 1) * (RX - 1)

    def on_border(x: int, y: int) -> bool:
        """True if (x,y) is the 1-px border of the rounded rect."""
        if not in_rounded_rect(x, y):
            return False
        for nx, ny in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
            if 0 <= nx < S and 0 <= ny < S and not in_rounded_rect(nx, ny):
                return True
        return False

    for y in range(S):
        row: list[tuple[int,int,int]] = []
        t = y / max(S - 1, 1)
        # gradient colour for this row
        bg = (
            int(top[0] + (bot[0] - top[0]) * t),
            int(top[1] + (bot[1] - top[1]) * t),
            int(top[2] + (bot[2] - top[2]) * t),
        )
        for x in range(S):
            if not in_rounded_rect(x, y):
                row.append(bot)          # outside rounded rect → darkest bg
            elif on_border(x, y):
                row.append(border)       # 1-px gold rim
            elif in_glyph(x, y):
                row.append(gold)         # letter pixel
            else:
                row.append(bg)           # gradient background
        pixels.append(row)

    return _make_png(pixels)


def stock_level(qty: float) -> str:
    if qty == 0:  return "out"
    if qty <= 2:  return "critical"
    return "low"


def stock_badge(qty: float) -> str:
    """Luxury pill badges — gold/amber/crimson palette."""
    if qty == 0:
        return (
            '<span style="background:linear-gradient(135deg,#2a0008,#1a0005);'
            'color:#ff6b8a;padding:4px 13px;border-radius:20px;font-weight:700;'
            'font-size:11px;letter-spacing:1px;white-space:nowrap;'
            'border:1px solid #ff4d6d44;font-family:Georgia,serif;">'
            'OUT OF STOCK</span>'
        )
    elif qty <= 2:
        return (
            f'<span style="background:linear-gradient(135deg,#2a1000,#1a0a00);'
            f'color:#ffaa5e;padding:4px 13px;border-radius:20px;font-weight:700;'
            f'font-size:11px;white-space:nowrap;'
            f'border:1px solid #ff8c4244;font-family:Georgia,serif;">'
            f'{qty:g} LEFT</span>'
        )
    else:
        return (
            f'<span style="background:linear-gradient(135deg,#c9a84c22,#0a0a0f);'
            f'color:#e8c96d;padding:4px 13px;border-radius:20px;font-weight:700;'
            f'font-size:11px;white-space:nowrap;'
            f'border:1px solid #c9a84c55;font-family:Georgia,serif;">'
            f'{qty:g} IN STOCK</span>'
        )


def natural_sort_key(s: str):
    return [
        int(c) if c.isdigit() else c.lower()
        for c in re.split(r'(\d+)', s or "")
    ]


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def categ_path(p: dict) -> str:
    return p["categ_id"][1] if p.get("categ_id") else ""


def subcateg_name(p: dict) -> str:
    full = categ_path(p)
    for _, kw in SPLIT_GROUPS:
        pattern = re.compile(re.escape(kw), re.IGNORECASE)
        m = pattern.search(full)
        if m:
            remainder = full[m.end():]
            remainder = re.sub(r'^\s*/\s*', '', remainder).strip()
            return remainder or full
    return full


def product_url(p: dict) -> str:
    """Build a direct Odoo backend link to this product."""
    return f"{ODOO_URL}/web#id={p['id']}&model=product.product&view_type=form"


# ─────────────────────────────────────────────
#  IMAGE DECODING
# ─────────────────────────────────────────────
def decode_image(raw_img) -> bytes | None:
    try:
        if not raw_img or str(raw_img) == "False":
            return None

        if isinstance(raw_img, xmlrpc.client.Binary):
            return raw_img.data

        if isinstance(raw_img, str):
            cleaned = raw_img.replace("\n", "").replace("\r", "").strip()
            return base64.b64decode(cleaned)

        if isinstance(raw_img, bytes):
            cleaned = raw_img.replace(b"\n", b"").replace(b"\r", b"").strip()
            return base64.b64decode(cleaned)

    except Exception:
        return None

    return None


# ─────────────────────────────────────────────
#  HTML BUILDER
#
#  Changes vs previous version:
#    • IMG_SIZE = 66px (50% larger than 44px)
#    • object-fit: contain + dark bg so no cropping
#    • Images wrapped in <a href> → clickable to Odoo product page
#    • Cell width is adaptive to IMG_SIZE constant
#    • Tightened row/section/level padding (no wasted negative space)
#    • CID references use canonical hash-based IDs to guarantee
#      zero duplication in the MIME message
# ─────────────────────────────────────────────
def build_html(
    products: list,
    qty_field: str,
    group_label: str,
    total_products: int,
    total_subcategories: int,
    out_count: int,
    critical_count: int,
    low_count: int,
    cid_map: dict,        # product_id → cid string (hash-based, shared across dupes)
) -> str:

    generated_at = utc_now().strftime("%d %b %Y  %H:%M UTC")

    by_level: dict[str, dict[str, list]] = {
        "out":      defaultdict(list),
        "critical": defaultdict(list),
        "low":      defaultdict(list),
    }
    for p in products:
        qty = float(p.get(qty_field) or 0)
        sub = subcateg_name(p) or "General"
        by_level[stock_level(qty)][sub].append(p)

    GOLD_LINE = (
        '<tr><td colspan="3" style="padding:0;height:1px;font-size:1px;'
        'background:linear-gradient(90deg,transparent,#c9a84c33 30%,'
        '#c9a84c55 50%,#c9a84c33 70%,transparent);">&nbsp;</td></tr>'
    )

    level_meta = {
        "out": (
            "OUT OF STOCK",
            "#ff6b8a", "#1e0008", "#ff4d6d33",
            "⬛", "#ff4d6d"
        ),
        "critical": (
            "CRITICAL STOCK  ·  1 – 2 UNITS",
            "#ffaa5e", "#1e0e00", "#ff8c4233",
            "◈", "#ff8c42"
        ),
        "low": (
            "LOW STOCK  ·  3 – 4 UNITS",
            "#e8c96d", "#0e0e00", "#c9a84c33",
            "◇", "#c9a84c"
        ),
    }

    sections_html = ""

    for lvl, (lvl_title, text_col, bg_col, border_col, icon, accent) in level_meta.items():
        cats = by_level[lvl]
        if not cats:
            continue

        lvl_count = sum(len(v) for v in cats.values())

        # ── Level banner ─────────────────────────────────────────────────
        sections_html += f"""
        <tr>
          <td colspan="3" style="padding:18px 20px 6px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="background:{bg_col};
                           border-top:1px solid {border_col};
                           border-bottom:1px solid {border_col};
                           border-left:3px solid {accent};
                           border-right:1px solid {border_col};
                           border-radius:0 8px 8px 0;
                           padding:9px 16px;">
                  <table width="100%" cellpadding="0" cellspacing="0"><tr>
                    <td style="font-family:Georgia,'Times New Roman',serif;
                               font-size:11px;font-weight:700;color:{text_col};
                               letter-spacing:2.5px;text-transform:uppercase;">
                      {icon}&nbsp; {lvl_title}
                    </td>
                    <td align="right"
                        style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                               font-size:11px;color:{text_col};opacity:0.7;
                               letter-spacing:0.5px;">
                      {lvl_count:,} product{'s' if lvl_count != 1 else ''}
                    </td>
                  </tr></table>
                </td>
              </tr>
            </table>
          </td>
        </tr>"""

        for sub_name in sorted(cats.keys(), key=natural_sort_key):
            cat_products = sorted(
                cats[sub_name],
                key=lambda p: (
                    natural_sort_key(p["name"] or ""),
                    float(p.get(qty_field) or 0),
                )
            )

            # ── Sub-category header ───────────────────────────────────────
            sections_html += f"""
        <tr>
          <td colspan="3" style="padding:6px 20px 2px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="border-bottom:1px solid #c9a84c22;padding-bottom:4px;">
                  <span style="font-family:Georgia,'Times New Roman',serif;
                               font-size:10px;font-weight:700;letter-spacing:2px;
                               text-transform:uppercase;color:#c9a84c;">
                    {esc(sub_name) if sub_name else "General"}
                  </span>
                  <span style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                               font-size:10px;color:#3a3020;margin-left:10px;">
                    {len(cat_products)} item{'s' if len(cat_products) != 1 else ''}
                  </span>
                </td>
              </tr>
            </table>
          </td>
        </tr>"""

            # ── Product rows ──────────────────────────────────────────────
            for idx, p in enumerate(cat_products):
                qty    = float(p.get(qty_field) or 0)
                badge  = stock_badge(qty)
                name   = esc(p["name"] or "—")
                row_bg = "#08080f" if idx % 2 == 0 else "#060609"
                cid    = cid_map[p["id"]]
                url    = product_url(p)

                # Contain (no crop) — dark bg fills letterbox gaps
                img_tag = (
                    f'<a href="{url}" target="_blank" '
                    f'style="display:block;width:{IMG_SIZE}px;height:{IMG_SIZE}px;'
                    f'border-radius:10px;overflow:hidden;border:1px solid #c9a84c22;'
                    f'background:#0a0a12;text-decoration:none;">'
                    f'<img src="cid:{cid}" width="{IMG_SIZE}" height="{IMG_SIZE}" '
                    f'style="width:{IMG_SIZE}px;height:{IMG_SIZE}px;'
                    f'object-fit:contain;display:block;" alt="">'
                    f'</a>'
                )

                sections_html += f"""
        <tr style="background:{row_bg};">
          <td width="{IMG_CELL_WIDTH}" style="padding:6px 6px 6px 20px;
                                vertical-align:middle;text-align:left;">
            {img_tag}
          </td>
          <td style="padding:6px 10px;
                     font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                     font-size:13px;color:#d4c5a0;font-weight:500;
                     vertical-align:middle;word-break:break-word;
                     line-height:1.4;">
            {name}
          </td>
          <td style="padding:6px 20px 6px 10px;text-align:right;
                     vertical-align:middle;white-space:nowrap;">
            {badge}
          </td>
        </tr>
        {GOLD_LINE}"""

    monogram    = group_label[0].upper() if group_label else "I"
    label_upper = group_label.upper()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Low Stock — {esc(group_label)}</title>
</head>
<body style="margin:0;padding:0;background:#04040a;">

<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#04040a;padding:32px 8px;">
  <tr><td align="center">
  <table width="660" cellpadding="0" cellspacing="0"
         style="max-width:660px;width:100%;">

    <!-- ══════════════════════════════════════════
         LUXURY HEADER
    ══════════════════════════════════════════ -->
    <tr>
      <td style="background:linear-gradient(160deg,#0d0d1a 0%,#080810 60%,#0a0810 100%);
                 border-radius:16px 16px 0 0;
                 border:1px solid #c9a84c33;
                 padding:0;">

        <div style="height:2px;background:linear-gradient(90deg,transparent,#c9a84c,#e8d48b,#c9a84c,transparent);
                    border-radius:16px 16px 0 0;"></div>

        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td width="90" style="padding:28px 0 28px 28px;vertical-align:top;">
              <div style="width:62px;height:62px;border-radius:50%;
                          background:linear-gradient(135deg,#1a1400,#0a0a0f);
                          border:1px solid #c9a84c66;
                          text-align:center;line-height:62px;
                          font-family:Georgia,'Times New Roman',serif;
                          font-size:26px;font-weight:700;color:#c9a84c;">
                {monogram}
              </div>
            </td>

            <td style="padding:28px 0 28px 12px;vertical-align:top;">
              <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                          font-size:9px;font-weight:700;letter-spacing:4px;
                          text-transform:uppercase;color:#c9a84c;
                          margin-bottom:6px;opacity:0.8;">
                INVENTORY&nbsp;&nbsp;·&nbsp;&nbsp;ALERT&nbsp;&nbsp;·&nbsp;&nbsp;{label_upper}
              </div>
              <div style="font-family:Georgia,'Times New Roman',serif;
                          font-size:26px;font-weight:700;color:#f0e6c8;
                          line-height:1.1;margin-bottom:8px;letter-spacing:0.5px;">
                Low Stock Report
              </div>
              <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                          font-size:11px;color:#5a4e35;letter-spacing:0.5px;">
                {generated_at}
                &nbsp;&nbsp;·&nbsp;&nbsp;
                Threshold &lt; {LOW_STOCK_THRESHOLD} units
                &nbsp;&nbsp;·&nbsp;&nbsp;
                <span style="color:#3a3020;font-size:10px;">{qty_field}</span>
              </div>
            </td>

            <td align="right" valign="top" style="padding:28px 28px 28px 0;">
              <div style="width:46px;height:46px;border-radius:50%;
                          background:linear-gradient(135deg,#2a0008,#0a0008);
                          border:1px solid #ff4d6d44;
                          text-align:center;line-height:46px;font-size:18px;">⚠</div>
            </td>
          </tr>
        </table>

        <div style="height:1px;margin:0 28px;
                    background:linear-gradient(90deg,transparent,#c9a84c55,#c9a84c88,#c9a84c55,transparent);">
        </div>

      </td>
    </tr>

    <!-- ══════════════════════════════════════════
         METRICS STRIP
    ══════════════════════════════════════════ -->
    <tr>
      <td style="background:#060610;
                 border-left:1px solid #c9a84c33;
                 border-right:1px solid #c9a84c33;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td width="25%" style="padding:20px 0;text-align:center;
                border-right:1px solid #c9a84c22;">
              <div style="font-family:Georgia,'Times New Roman',serif;
                          font-size:30px;font-weight:700;
                          color:#e8c96d;line-height:1;">
                {total_products:,}
              </div>
              <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                          font-size:8px;color:#3a3020;margin-top:5px;
                          text-transform:uppercase;letter-spacing:2px;">
                Products
              </div>
            </td>
            <td width="25%" style="padding:20px 0;text-align:center;
                border-right:1px solid #c9a84c22;">
              <div style="font-family:Georgia,'Times New Roman',serif;
                          font-size:30px;font-weight:700;
                          color:#e8c96d;line-height:1;">
                {total_subcategories:,}
              </div>
              <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                          font-size:8px;color:#3a3020;margin-top:5px;
                          text-transform:uppercase;letter-spacing:2px;">
                Sub-Categories
              </div>
            </td>
            <td width="25%" style="padding:20px 0;text-align:center;
                border-right:1px solid #c9a84c22;">
              <div style="font-family:Georgia,'Times New Roman',serif;
                          font-size:30px;font-weight:700;
                          color:#ffaa5e;line-height:1;">
                {critical_count:,}
              </div>
              <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                          font-size:8px;color:#3a3020;margin-top:5px;
                          text-transform:uppercase;letter-spacing:2px;">
                Critical
              </div>
            </td>
            <td width="25%" style="padding:20px 0;text-align:center;">
              <div style="font-family:Georgia,'Times New Roman',serif;
                          font-size:30px;font-weight:700;
                          color:#ff6b8a;line-height:1;">
                {out_count:,}
              </div>
              <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                          font-size:8px;color:#3a3020;margin-top:5px;
                          text-transform:uppercase;letter-spacing:2px;">
                Out of Stock
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>

    <!-- ══════════════════════════════════════════
         COLUMN HEADERS
    ══════════════════════════════════════════ -->
    <tr>
      <td style="background:#04040c;
                 border-left:1px solid #c9a84c33;
                 border-right:1px solid #c9a84c33;
                 border-top:1px solid #c9a84c22;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr style="background:#030308;">
            <th width="{IMG_CELL_WIDTH}" style="padding:8px 6px 8px 20px;text-align:left;
                font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:8px;
                font-weight:700;letter-spacing:2.5px;text-transform:uppercase;
                color:#3a3020;">IMG</th>
            <th style="padding:8px 10px;text-align:left;
                font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:8px;
                font-weight:700;letter-spacing:2.5px;text-transform:uppercase;
                color:#3a3020;">Product Name</th>
            <th style="padding:8px 20px 8px 10px;text-align:right;
                font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:8px;
                font-weight:700;letter-spacing:2.5px;text-transform:uppercase;
                color:#3a3020;">In Stock</th>
          </tr>
        </table>
      </td>
    </tr>

    <!-- ══════════════════════════════════════════
         PRODUCT SECTIONS
    ══════════════════════════════════════════ -->
    <tr>
      <td style="background:#04040c;
                 border-left:1px solid #c9a84c33;
                 border-right:1px solid #c9a84c33;">
        <table width="100%" cellpadding="0" cellspacing="0">
          {sections_html}
          <tr><td colspan="3" style="height:12px;"></td></tr>
        </table>
      </td>
    </tr>

    <!-- ══════════════════════════════════════════
         FOOTER
    ══════════════════════════════════════════ -->
    <tr>
      <td style="background:#030308;
                 border:1px solid #c9a84c33;
                 border-top:1px solid #c9a84c22;
                 border-radius:0 0 16px 16px;
                 padding:0;">

        <div style="height:1px;
                    background:linear-gradient(90deg,transparent,#c9a84c44,#c9a84c66,#c9a84c44,transparent);">
        </div>

        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="padding:14px 28px;
                       font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                       font-size:10px;color:#2a2015;letter-spacing:0.5px;">
              Auto-generated · Inventory Alert System
              &nbsp;&nbsp;·&nbsp;&nbsp;
              {label_upper}
            </td>
            <td align="right"
                style="padding:14px 28px;
                       font-family:Georgia,'Times New Roman',serif;
                       font-size:10px;color:#2a2015;">
              {low_count:,}&nbsp;low
              &nbsp;·&nbsp;
              <span style="color:#ffaa5e55;">{critical_count:,}&nbsp;critical</span>
              &nbsp;·&nbsp;
              <span style="color:#ff6b8a77;">{out_count:,}&nbsp;out</span>
            </td>
          </tr>
        </table>

        <div style="height:2px;
                    background:linear-gradient(90deg,transparent,#c9a84c,#e8d48b,#c9a84c,transparent);
                    border-radius:0 0 16px 16px;">
        </div>

      </td>
    </tr>

  </table>
  </td></tr>
</table>

</body>
</html>"""


# ─────────────────────────────────────────────
#  SEND
#
#  Key upgrade: CID is based on the MD5 hash of the image bytes,
#  NOT the product_id. This means identical images share ONE
#  MIME part — zero duplication in the email payload.
#
#  Structure:
#    multipart/related
#    ├── multipart/alternative
#    │   └── text/html
#    └── image/*  cid:<md5hash>   ← one part per UNIQUE image only
# ─────────────────────────────────────────────
def send_email(
    html: str,
    subject: str,
    unique_images: dict,   # { md5_hash (str): raw bytes }
) -> None:

    msg_related = MIMEMultipart("related")
    msg_related["Subject"] = subject
    msg_related["From"]    = SMTP_FROM
    msg_related["To"]      = ", ".join(SMTP_TO)

    # HTML body
    msg_alt = MIMEMultipart("alternative")
    msg_alt.attach(MIMEText(html, "html"))
    msg_related.attach(msg_alt)

    # One inline image part per UNIQUE image hash — no duplicates, no SVG
    # NOTE: Content-Disposition is intentionally OMITTED.
    # Setting it (even as "inline") causes Gmail to show every image as a
    # visible attachment below the email. Without it, Gmail treats them as
    # embedded body resources referenced only by their CID.
    for img_hash, img_bytes in unique_images.items():
        try:
            # Sniff format: JPEG starts FF D8, everything else treat as PNG
            if img_bytes[:2] == b"\xff\xd8":
                subtype = "jpeg"
            else:
                subtype = "png"
            img_part = MIMEImage(img_bytes, _subtype=subtype)
            img_part.add_header("Content-ID", f"<{img_hash}>")
            # No Content-Disposition header — keeps images out of attachment tray
            msg_related.attach(img_part)
        except Exception as e:
            print(f"   ⚠️  Could not attach image {img_hash[:8]}…: {e}", flush=True)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        # Pass SMTP_TO as the explicit rcpttos list so every address
        # in the list receives the message, regardless of the To: header.
        server.send_message(msg_related, from_addr=SMTP_FROM, to_addrs=SMTP_TO)


# ─────────────────────────────────────────────
#  1. CONNECT TO ODOO
# ─────────────────────────────────────────────
print("🔌 Connecting to Odoo...", flush=True)
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid    = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise Exception("❌ Odoo authentication failed")
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
print("✅ Connected to Odoo", flush=True)


# ─────────────────────────────────────────────
#  2. DETECT QTY FIELD + FETCH ALL LOW-STOCK
# ─────────────────────────────────────────────
print("🔍 Detecting quantity field...", flush=True)

all_products = None
qty_field    = None

base_fields = ["id", "name", "categ_id", "image_512"]

for candidate in QTY_FIELD_CANDIDATES:
    try:
        print(f"   ↳ Trying field: {candidate}", flush=True)
        result = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "search_read",
            [[
                ("active",   "=", True),
                (candidate,  "<", LOW_STOCK_THRESHOLD),
                (candidate, ">=", 0),
            ]],
            {"fields": base_fields + [candidate], "limit": 0},
        )
        all_products = result
        qty_field    = candidate
        print(f"✅ Using quantity field: '{qty_field}'", flush=True)
        break
    except Exception as exc:
        print(f"   ⚠️  Field '{candidate}' unavailable: {exc}", flush=True)

if all_products is None or qty_field is None:
    print("❌ None of the quantity fields worked. Aborting.", flush=True)
    sys.exit(1)

all_products = [
    p for p in all_products
    if p.get(qty_field) not in (None, False)
    and float(p[qty_field]) < LOW_STOCK_THRESHOLD
]

if not all_products:
    print("✅ No low-stock products found. Exiting.", flush=True)
    sys.exit(0)

print(f"📦 Found {len(all_products)} low-stock product(s).", flush=True)


# ─────────────────────────────────────────────
#  3. DECODE IMAGES
#
#  hash_to_bytes  : md5 → canonical raw bytes  (dedup store)
#  product_hash   : product_id → md5           (lookup for CID in HTML)
#
#  CID in HTML = md5 hash string  →  one MIME part per unique image.
# ─────────────────────────────────────────────
print("🖼️  Decoding product images...", flush=True)

hash_to_bytes: dict[str, bytes] = {}   # md5 → raw bytes
product_hash:  dict[int, str]   = {}   # product_id → md5 (= CID)

for p in all_products:
    p_id      = p["id"]
    img_bytes = decode_image(p.get("image_512"))

    if not img_bytes:
        # Letter-avatar: one per unique product name (avatars are cheap to dupe in bytes
        # but we still dedup them via hash)
        img_bytes = letter_avatar_png_bytes(p["name"] or "?")

    h = hashlib.md5(img_bytes).hexdigest()
    if h not in hash_to_bytes:
        hash_to_bytes[h] = img_bytes
    product_hash[p_id] = h

real_images = sum(
    1 for p in all_products
    if decode_image(p.get("image_512")) is not None
)
print(
    f"   ↳ {len(all_products)} products → "
    f"{len(hash_to_bytes)} unique MIME image part(s) "
    f"({real_images} real, {len(all_products)-real_images} avatar).",
    flush=True,
)


# ─────────────────────────────────────────────
#  4. SPLIT BY PARENT CATEGORY KEYWORD
# ─────────────────────────────────────────────
print("🗂️  Splitting products by parent category...", flush=True)

groups: list[tuple[str, list]] = []

for label, keyword in SPLIT_GROUPS:
    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    matched = [p for p in all_products if pattern.search(categ_path(p))]
    if matched:
        groups.append((label, matched))
        print(f"   ↳ '{label}' → {len(matched)} product(s).", flush=True)
    else:
        print(f"   ↳ '{label}' → 0 products (skipping email).", flush=True)

if not groups:
    print("⚠️  No products matched any split group. Nothing to send.", flush=True)
    sys.exit(0)


# ─────────────────────────────────────────────
#  5. BUILD & SEND ONE EMAIL PER GROUP
# ─────────────────────────────────────────────
print(f"📤 Sending to {SMTP_TO}...", flush=True)

for group_label, products in groups:

    products_sorted = sorted(
        products,
        key=lambda p: (
            natural_sort_key(subcateg_name(p)),
            natural_sort_key(p["name"] or ""),
            float(p.get(qty_field) or 0),
        )
    )

    total_products      = len(products_sorted)
    out_count           = sum(1 for p in products_sorted if float(p.get(qty_field) or 0) == 0)
    critical_count      = sum(1 for p in products_sorted if 0 < float(p.get(qty_field) or 0) <= 2)
    low_count           = sum(1 for p in products_sorted if 3 <= float(p.get(qty_field) or 0) < LOW_STOCK_THRESHOLD)
    total_subcategories = len({subcateg_name(p) for p in products_sorted})

    # cid_map: product_id → hash string used as CID in both HTML and MIME parts
    group_cid_map = {p["id"]: product_hash[p["id"]] for p in products_sorted}

    # Only attach unique images referenced by THIS group's products
    group_unique_images = {
        h: hash_to_bytes[h]
        for h in set(group_cid_map.values())
    }

    subject = (
        f"⚑ Low Stock · {group_label}"
        f" — {total_products:,} product{'s' if total_products != 1 else ''}"
        f" · {out_count:,} out of stock"
        f" · {utc_now().strftime('%d %b %Y')}"
    )

    html = build_html(
        products            = products_sorted,
        qty_field           = qty_field,
        group_label         = group_label,
        total_products      = total_products,
        total_subcategories = total_subcategories,
        out_count           = out_count,
        critical_count      = critical_count,
        low_count           = low_count,
        cid_map             = group_cid_map,
    )

    try:
        send_email(html, subject, group_unique_images)
        print(
            f"   ✅ '{group_label}' email sent  "
            f"({total_products:,} products  ·  {out_count:,} out  "
            f"·  {critical_count:,} critical  ·  {low_count:,} low  "
            f"·  {len(group_unique_images)} unique image part(s)).",
            flush=True,
        )
    except Exception as exc:
        print(f"   ❌ '{group_label}' email FAILED: {exc}", flush=True)
        sys.exit(1)

print("✅ All emails sent successfully!", flush=True)
