import sys
import os
import re
import io
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
SMTP_TO       = os.environ.get("SMTP_TO")

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


def letter_avatar_png_bytes(name: str) -> bytes:
    """
    Return PNG bytes for a gold letter avatar.
    Rendered with Pillow — 100% Gmail-safe, no SVG.
    Falls back to a solid-colour PNG if font rendering fails.
    """
    from PIL import Image, ImageDraw, ImageFont

    letter = (name or "?")[0].upper()
    hex_color = avatar_color(name)

    # Parse hex colour → RGB tuple
    h = hex_color.lstrip("#")
    top_rgb    = tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    bottom_rgb = (10, 10, 15)   # #0a0a0f
    gold_rgb   = (232, 201, 109) # #e8c96d
    border_rgb = (201, 168, 76)  # #c9a84c

    size = IMG_SIZE

    # ── Draw gradient background ──────────────────────────────────────────
    img  = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(img)

    for y in range(size):
        t = y / (size - 1)
        r = int(top_rgb[0] + (bottom_rgb[0] - top_rgb[0]) * t)
        g = int(top_rgb[1] + (bottom_rgb[1] - top_rgb[1]) * t)
        b = int(top_rgb[2] + (bottom_rgb[2] - top_rgb[2]) * t)
        draw.line([(0, y), (size, y)], fill=(r, g, b))

    # ── Rounded-rect mask (rx=12) ────────────────────────────────────────
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1],
                                           radius=12, fill=255)
    img.putalpha(mask)

    # ── Gold border ───────────────────────────────────────────────────────
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, size - 1, size - 1],
                           radius=12, outline=border_rgb + (153,), width=1)

    # ── Letter ────────────────────────────────────────────────────────────
    font_size = int(size * 0.50)
    font = None
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
        try:
            font = ImageFont.truetype(path, font_size)
            break
        except Exception:
            continue

    if font is None:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), letter, font=font)
    tw   = bbox[2] - bbox[0]
    th   = bbox[3] - bbox[1]
    tx   = (size - tw) // 2 - bbox[0]
    ty   = (size - th) // 2 - bbox[1]
    draw.text((tx, ty), letter, font=font, fill=gold_rgb)

    # ── Export as PNG ─────────────────────────────────────────────────────
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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
    msg_related["To"]      = SMTP_TO

    # HTML body
    msg_alt = MIMEMultipart("alternative")
    msg_alt.attach(MIMEText(html, "html"))
    msg_related.attach(msg_alt)

    # One inline image part per UNIQUE image hash — no duplicates, no SVG
    for img_hash, img_bytes in unique_images.items():
        try:
            # Sniff format: JPEG starts FF D8, PNG starts 89 50 4E 47
            if img_bytes[:2] == b"\xff\xd8":
                subtype = "jpeg"
            else:
                subtype = "png"
            img_part = MIMEImage(img_bytes, _subtype=subtype)
            img_part.add_header("Content-ID",          f"<{img_hash}>")
            img_part.add_header("Content-Disposition", "inline",
                                filename=f"{img_hash}.{subtype}")
            msg_related.attach(img_part)
        except Exception as e:
            print(f"   ⚠️  Could not attach image {img_hash[:8]}…: {e}", flush=True)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg_related)


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
