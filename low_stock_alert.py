import sys
import os
import re
import base64
import hashlib
import smtplib
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
# Products whose full category path contains "imported" (case-insensitive)
# go into Email 1. Those containing "indian" go into Email 2.
# Products matching neither are skipped (not emailed).
# Adjust these strings to match your exact Odoo category names.
SPLIT_GROUPS = [
    ("Imported", "imported"),   # (email label,  keyword to match in categ path)
    ("Indian",   "indian"),
]

# Deterministic colour palette for letter avatars (no product image)
AVATAR_COLORS = [
    "#b8860b", "#8b6914", "#a0522d", "#6b4226",
    "#2c4a7c", "#1a3a5c", "#2d6a4f", "#1b4332",
]

# 1×1 transparent PNG fallback (used when a product has no image)
FALLBACK_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
    "DUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def avatar_color(name: str) -> str:
    idx = int(hashlib.md5(name.encode()).hexdigest(), 16) % len(AVATAR_COLORS)
    return AVATAR_COLORS[idx]


def letter_avatar_svg_bytes(name: str) -> bytes:
    """
    Return raw SVG bytes for a gold letter avatar.
    Attached as a MIMEImage just like a real product image.
    """
    letter = (name or "?")[0].upper()
    color  = avatar_color(name)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48">'
        f'<defs>'
        f'<linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0%" stop-color="{color}"/>'
        f'<stop offset="100%" stop-color="#0a0a0f"/>'
        f'</linearGradient>'
        f'</defs>'
        f'<rect width="48" height="48" rx="10" fill="url(#g)"/>'
        f'<rect width="48" height="48" rx="10" fill="none" '
        f'stroke="#c9a84c" stroke-width="1" opacity="0.6"/>'
        f'<text x="24" y="33" text-anchor="middle" '
        f'font-family="Georgia,serif" font-size="22" '
        f'font-weight="700" fill="#e8c96d">{letter}</text>'
        f'</svg>'
    )
    return svg.encode()


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
    """Sort strings naturally: 'Item 2' before 'Item 10'."""
    return [
        int(c) if c.isdigit() else c.lower()
        for c in re.split(r'(\d+)', s or "")
    ]


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def categ_path(p: dict) -> str:
    """Return the full category path string, e.g. 'All / Imported / Sofas'."""
    return p["categ_id"][1] if p.get("categ_id") else ""


def subcateg_name(p: dict) -> str:
    """
    Return the sub-category portion (last segment after the split keyword).
    e.g. 'All / Imported / Living Room / Sofas'  →  'Living Room / Sofas'
    Falls back to the full path if no split keyword found.
    """
    full = categ_path(p)
    for _, kw in SPLIT_GROUPS:
        pattern = re.compile(re.escape(kw), re.IGNORECASE)
        m = pattern.search(full)
        if m:
            remainder = full[m.end():]
            remainder = re.sub(r'^\s*/\s*', '', remainder).strip()
            return remainder or full
    return full


# ─────────────────────────────────────────────
#  IMAGE DECODING
#  Proven approach from the working test script:
#  sanitise the base64 string then decode to raw bytes.
#  xmlrpc.client.Binary objects are handled explicitly.
# ─────────────────────────────────────────────
def decode_image(raw_img) -> bytes | None:
    """
    Return raw image bytes from whatever xmlrpc.client gives us, or None.

    Odoo XML-RPC can return image fields as:
      • xmlrpc.client.Binary  →  .data holds raw bytes already
      • str                   →  base64-encoded string
      • bytes                 →  base64-encoded bytes (rare)
      • False / None          →  no image
    """
    try:
        if not raw_img or str(raw_img) == "False":
            return None

        # Case 1: xmlrpc.client.Binary — .data is already raw bytes
        if isinstance(raw_img, xmlrpc.client.Binary):
            return raw_img.data

        # Case 2: str — base64-encoded string (most common in practice)
        if isinstance(raw_img, str):
            cleaned = raw_img.replace("\n", "").replace("\r", "").strip()
            return base64.b64decode(cleaned)

        # Case 3: bytes — base64-encoded bytes
        if isinstance(raw_img, bytes):
            cleaned = raw_img.replace(b"\n", b"").replace(b"\r", b"").strip()
            return base64.b64decode(cleaned)

    except Exception:
        return None

    return None


# ─────────────────────────────────────────────
#  HTML BUILDER  —  100 % Gmail-safe, zero JS
#
#  Images referenced as  cid:product_<id>
#  resolved by MIMEImage parts added in send_email().
#
#  Layout strategy:
#    • 3 colour-coded stock-level bands: OUT → CRITICAL → LOW
#    • Within each band: sub-categories sorted A-Z / 0-9
#    • Within each sub-category: products sorted A-Z / 0-9 → qty asc
#    • Pure luxury design: deep obsidian + gold dividers + serif accents
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
) -> str:

    generated_at = utc_now().strftime("%d %b %Y  %H:%M UTC")

    # ── Group: stock level → sub-category → [products] ──────────────────
    by_level: dict[str, dict[str, list]] = {
        "out":      defaultdict(list),
        "critical": defaultdict(list),
        "low":      defaultdict(list),
    }
    for p in products:
        qty = float(p.get(qty_field) or 0)
        sub = subcateg_name(p) or "General"
        by_level[stock_level(qty)][sub].append(p)

    # ── Decorative gold divider line ─────────────────────────────────────
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
          <td colspan="3" style="padding:28px 20px 10px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="background:{bg_col};
                           border-top:1px solid {border_col};
                           border-bottom:1px solid {border_col};
                           border-left:3px solid {accent};
                           border-right:1px solid {border_col};
                           border-radius:0 8px 8px 0;
                           padding:11px 20px;">
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

        # Sort sub-categories naturally
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
          <td colspan="3" style="padding:10px 20px 3px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="border-bottom:1px solid #c9a84c22;padding-bottom:6px;">
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
                cid    = f"product_{p['id']}"

                img_tag = (
                    f'<img src="cid:{cid}" width="44" height="44" '
                    f'style="width:44px;height:44px;border-radius:8px;'
                    f'object-fit:cover;display:block;border:1px solid #c9a84c22;" alt="">'
                )

                sections_html += f"""
        <tr style="background:{row_bg};">
          <td width="64" style="padding:8px 6px 8px 20px;
                                vertical-align:middle;text-align:left;">
            {img_tag}
          </td>
          <td style="padding:8px 10px;
                     font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                     font-size:13px;color:#d4c5a0;font-weight:500;
                     vertical-align:middle;word-break:break-word;
                     line-height:1.4;">
            {name}
          </td>
          <td style="padding:8px 20px 8px 10px;text-align:right;
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
            <th width="64" style="padding:10px 6px 10px 20px;text-align:left;
                font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:8px;
                font-weight:700;letter-spacing:2.5px;text-transform:uppercase;
                color:#3a3020;">IMG</th>
            <th style="padding:10px 10px;text-align:left;
                font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:8px;
                font-weight:700;letter-spacing:2.5px;text-transform:uppercase;
                color:#3a3020;">Product Name</th>
            <th style="padding:10px 20px 10px 10px;text-align:right;
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
          <tr><td colspan="3" style="height:20px;"></td></tr>
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
            <td style="padding:16px 28px;
                       font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                       font-size:10px;color:#2a2015;letter-spacing:0.5px;">
              Auto-generated · Inventory Alert System
              &nbsp;&nbsp;·&nbsp;&nbsp;
              {label_upper}
            </td>
            <td align="right"
                style="padding:16px 28px;
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
#  Structure (proven from working test script):
#    multipart/related          ← ties HTML body to its inline images
#    ├── multipart/alternative  ← best-practice HTML wrapper
#    │   └── text/html
#    └── image/png  cid:product_<id>   ← one part per product in this group
# ─────────────────────────────────────────────
def send_email(
    html: str,
    subject: str,
    image_map: dict,   # { product_id (int): raw bytes }
) -> None:

    msg_related = MIMEMultipart("related")
    msg_related["Subject"] = subject
    msg_related["From"]    = SMTP_FROM
    msg_related["To"]      = SMTP_TO

    # HTML body
    msg_alt = MIMEMultipart("alternative")
    msg_alt.attach(MIMEText(html, "html"))
    msg_related.attach(msg_alt)

    # One inline image part per unique product_id in this group
    for product_id, img_bytes in image_map.items():
        try:
            # SVG avatars vs PNG product images
            if img_bytes[:4] == b"<svg":
                img_part = MIMEImage(img_bytes, _subtype="svg+xml")
            else:
                img_part = MIMEImage(img_bytes, _subtype="png")
            img_part.add_header("Content-ID",          f"<product_{product_id}>")
            img_part.add_header("Content-Disposition", "inline",
                                filename=f"product_{product_id}.png")
            msg_related.attach(img_part)
        except Exception as e:
            print(f"   ⚠️  Could not attach image for product {product_id}: {e}", flush=True)

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

# Safety filter — drop nulls / False that slipped through
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
#  Build a map: product_id → raw bytes
#  Identical image data is deduplicated by MD5 hash so the same
#  bytes are never stored twice in memory, but every product_id
#  still gets its own entry (needed for CID lookup in send_email).
# ─────────────────────────────────────────────
print("🖼️  Decoding product images...", flush=True)

hash_to_bytes:   dict[str, bytes] = {}   # md5 → canonical raw bytes
product_img_map: dict[int, bytes] = {}   # product_id → raw bytes

for p in all_products:
    p_id      = p["id"]
    img_bytes = decode_image(p.get("image_512"))

    if img_bytes:
        h = hashlib.md5(img_bytes).hexdigest()
        if h not in hash_to_bytes:
            hash_to_bytes[h] = img_bytes
        product_img_map[p_id] = hash_to_bytes[h]
    else:
        # Letter-avatar SVG as fallback — unique per product name
        product_img_map[p_id] = letter_avatar_svg_bytes(p["name"] or "?")

print(
    f"   ↳ {len(all_products)} products → "
    f"{len(hash_to_bytes)} unique real image(s).",
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

    # Only attach images for products in THIS group — no cross-contamination
    group_image_map = {p["id"]: product_img_map[p["id"]] for p in products_sorted}

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
    )

    try:
        send_email(html, subject, group_image_map)
        print(
            f"   ✅ '{group_label}' email sent  "
            f"({total_products:,} products  ·  {out_count:,} out  "
            f"·  {critical_count:,} critical  ·  {low_count:,} low).",
            flush=True,
        )
    except Exception as exc:
        print(f"   ❌ '{group_label}' email FAILED: {exc}", flush=True)
        sys.exit(1)

print("✅ All emails sent successfully!", flush=True)
