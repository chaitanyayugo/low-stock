import sys
import os
import base64
import hashlib
import smtplib
import xmlrpc.client
from collections import defaultdict
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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

# Set to your Odoo boolean field name to send two separate emails,
# or leave as None for a single combined email.
IMPORTED_FIELD = None

# Deterministic colour palette for letter avatars
AVATAR_COLORS = [
    "#1d4ed8", "#0369a1", "#047857", "#7c3aed",
    "#b45309", "#be123c", "#0e7490", "#15803d",
]


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def avatar_color(name: str) -> str:
    idx = int(hashlib.md5(name.encode()).hexdigest(), 16) % len(AVATAR_COLORS)
    return AVATAR_COLORS[idx]


def letter_avatar_uri(name: str) -> str:
    """Inline SVG data URI — zero email attachments."""
    letter = (name or "?")[0].upper()
    color  = avatar_color(name)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="44" height="44">'
        f'<rect width="44" height="44" rx="10" fill="{color}"/>'
        f'<text x="22" y="31" text-anchor="middle" '
        f'font-family="Helvetica,Arial,sans-serif" font-size="20" '
        f'font-weight="700" fill="#fff">{letter}</text>'
        f'</svg>'
    )
    b64 = base64.b64encode(svg.encode()).decode()
    return f"data:image/svg+xml;base64,{b64}"


def img_to_data_uri(img_bytes: bytes) -> str:
    b64 = base64.b64encode(img_bytes).decode()
    return f"data:image/png;base64,{b64}"


def stock_level(qty: float) -> str:
    if qty == 0:  return "out"
    if qty <= 2:  return "critical"
    return "low"


def stock_badge(qty: float) -> str:
    if qty == 0:
        return (
            '<span style="background:#3d0010;color:#ff4d6d;padding:3px 11px;'
            'border-radius:20px;font-weight:700;font-size:12px;'
            'letter-spacing:0.5px;white-space:nowrap;">OUT OF STOCK</span>'
        )
    elif qty <= 2:
        return (
            f'<span style="background:#3d1a00;color:#ff8c42;padding:3px 11px;'
            f'border-radius:20px;font-weight:700;font-size:12px;'
            f'white-space:nowrap;">{qty:g} left</span>'
        )
    else:
        return (
            f'<span style="background:#0a2410;color:#52c41a;padding:3px 11px;'
            f'border-radius:20px;font-weight:700;font-size:12px;'
            f'white-space:nowrap;">{qty:g} in stock</span>'
        )


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ─────────────────────────────────────────────
#  HTML BUILDER  —  100% Gmail-safe, zero JS
#
#  WHY no dropdowns/JS:
#    Gmail (web + Android + iOS) strips ALL <script> tags and most
#    interactive CSS selectors (:checked, :target, etc.).
#    The only reliable interactivity in Gmail is clicking links.
#
#  What we do instead:
#    • Products pre-grouped into 3 colour-coded sections:
#        🔴 Out of Stock  →  🟠 Critical (1-2)  →  🟡 Low (3-4)
#    • Within each section, products grouped by category with a
#      visible header — readers can scan visually.
#    • A "Product Name Index" at the bottom lists every product name
#      in plain text — fully Ctrl-F / Find-in-page searchable.
#    • A tip banner explains how to search on desktop & mobile.
# ─────────────────────────────────────────────
def build_html(
    products: list,
    qty_field: str,
    product_data_uris: dict,
    label: str,
    total_products: int,
    total_categories: int,
    critical_count: int,
    low_count: int,
) -> str:

    generated_at = utc_now().strftime("%d %b %Y %H:%M UTC")

    # Group by stock level, then category
    by_level: dict[str, dict[str, list]] = {
        "out":      defaultdict(list),
        "critical": defaultdict(list),
        "low":      defaultdict(list),
    }
    for p in products:
        qty = float(p.get(qty_field) or 0)
        cat = p["categ_id"][1] if p.get("categ_id") else "Uncategorised"
        by_level[stock_level(qty)][cat].append(p)

    level_meta = {
        "out":      ("OUT OF STOCK",      "#ff4d6d", "#3d0010", "#ff4d6d44"),
        "critical": ("CRITICAL (1-2 units)", "#ff8c42", "#3d1a00", "#ff8c4244"),
        "low":      ("LOW STOCK (3-4 units)", "#52c41a", "#0a2410", "#52c41a33"),
    }

    sections_html = ""

    for lvl, (lvl_title, text_col, bg_col, border_col) in level_meta.items():
        cats = by_level[lvl]
        if not cats:
            continue

        lvl_count = sum(len(v) for v in cats.values())

        # Level header row
        sections_html += f"""
        <tr>
          <td colspan="3" style="padding:20px 16px 8px 16px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="background:{bg_col};border:1px solid {border_col};
                           border-radius:8px;padding:9px 16px;">
                  <table width="100%" cellpadding="0" cellspacing="0"><tr>
                    <td style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                               font-size:13px;font-weight:700;color:{text_col};
                               letter-spacing:0.5px;">
                      {lvl_title}
                    </td>
                    <td align="right"
                        style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                               font-size:12px;color:{text_col};">
                      {lvl_count} product{'s' if lvl_count != 1 else ''}
                    </td>
                  </tr></table>
                </td>
              </tr>
            </table>
          </td>
        </tr>"""

        for cat_name in sorted(cats.keys()):
            cat_products = sorted(cats[cat_name], key=lambda p: (p["name"] or "").lower())

            # Category sub-header
            sections_html += f"""
        <tr>
          <td colspan="3" style="padding:8px 16px 2px 16px;">
            <div style="background:#0f172a;border:1px solid #1e293b;
                        border-radius:6px;padding:5px 12px;">
              <span style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                           font-size:10px;font-weight:700;letter-spacing:1.5px;
                           text-transform:uppercase;color:#64748b;">
                {esc(cat_name)}
              </span>
              <span style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                           font-size:10px;color:#334155;margin-left:8px;">
                {len(cat_products)} item{'s' if len(cat_products) != 1 else ''}
              </span>
            </div>
          </td>
        </tr>"""

            for p in cat_products:
                qty   = float(p.get(qty_field) or 0)
                uri   = product_data_uris.get(p["id"], "")
                badge = stock_badge(qty)
                name  = esc(p["name"] or "—")

                if uri:
                    img_tag = (
                        f'<img src="{uri}" width="40" height="40" '
                        f'style="width:40px;height:40px;border-radius:8px;'
                        f'object-fit:cover;display:block;border:0;" alt="">'
                    )
                else:
                    av_uri = letter_avatar_uri(p["name"] or "?")
                    img_tag = (
                        f'<img src="{av_uri}" width="40" height="40" '
                        f'style="width:40px;height:40px;border-radius:8px;'
                        f'display:block;border:0;" alt="">'
                    )

                sections_html += f"""
        <tr style="background:#0a1628;">
          <td width="56" style="padding:9px 4px 9px 16px;
                                vertical-align:middle;text-align:center;">
            {img_tag}
          </td>
          <td style="padding:9px 8px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                     font-size:13px;color:#e2e8f0;font-weight:500;
                     vertical-align:middle;word-break:break-word;">
            {name}
          </td>
          <td style="padding:9px 16px 9px 8px;text-align:right;
                     vertical-align:middle;white-space:nowrap;">
            {badge}
          </td>
        </tr>
        <tr>
          <td colspan="3" style="padding:0;height:1px;font-size:1px;
              background:#111827;">&nbsp;</td>
        </tr>"""

    # Product name index for Ctrl-F searching
    all_names = sorted({(p["name"] or "").strip() for p in products}, key=str.lower)
    MAX_INDEX = 100
    shown     = all_names[:MAX_INDEX]
    overflow  = len(all_names) - MAX_INDEX

    index_items = " &nbsp;·&nbsp; ".join(
        f'<span style="font-family:\'Segoe UI\',Helvetica,Arial,sans-serif;'
        f'font-size:11px;color:#64748b;">{esc(n)}</span>'
        for n in shown
    )
    overflow_note = (
        f'<div style="font-family:\'Segoe UI\',Helvetica,Arial,sans-serif;'
        f'font-size:11px;color:#334155;margin-top:6px;">'
        f'… and {overflow} more products not shown here.</div>'
    ) if overflow > 0 else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Low Stock Alert</title>
</head>
<body style="margin:0;padding:0;background:#060b14;">
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#060b14;padding:24px 8px;">
  <tr><td align="center">
  <table width="640" cellpadding="0" cellspacing="0"
         style="max-width:640px;width:100%;">

    <!-- HEADER -->
    <tr>
      <td style="background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);
                 border-radius:16px 16px 0 0;border:1px solid #1e3a5f;
                 padding:28px 24px 20px;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:10px;font-weight:700;letter-spacing:3px;
                        text-transform:uppercase;color:#38bdf8;margin-bottom:8px;">
              ◈ &nbsp;INVENTORY MANAGEMENT SYSTEM
            </div>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:22px;font-weight:700;color:#f1f5f9;
                        line-height:1.2;margin-bottom:6px;">
              Low Stock Alert{"&nbsp;<span style='font-size:14px;color:#475569;font-weight:400;'>" + label + "</span>" if label else ""}
            </div>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:11px;color:#64748b;">
              {generated_at}
              &nbsp;·&nbsp; Threshold &lt; {LOW_STOCK_THRESHOLD}
              &nbsp;·&nbsp; <code style="color:#94a3b8;font-size:10px;">{qty_field}</code>
            </div>
          </td>
          <td align="right" valign="top" width="56">
            <div style="background:#ff4d6d22;border:1px solid #ff4d6d55;
                        border-radius:50%;width:44px;height:44px;
                        text-align:center;line-height:44px;font-size:20px;">⚠</div>
          </td>
        </tr></table>
      </td>
    </tr>

    <!-- METRICS -->
    <tr>
      <td style="background:#0d1929;border-left:1px solid #1e3a5f;
                 border-right:1px solid #1e3a5f;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td width="33%" style="padding:16px 0;text-align:center;
                                  border-right:1px solid #1e293b;">
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:26px;font-weight:800;color:#f1f5f9;line-height:1;">
              {total_products}</div>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:9px;color:#475569;margin-top:4px;
                        text-transform:uppercase;letter-spacing:1px;">Products</div>
          </td>
          <td width="33%" style="padding:16px 0;text-align:center;
                                  border-right:1px solid #1e293b;">
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:26px;font-weight:800;color:#f1f5f9;line-height:1;">
              {total_categories}</div>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:9px;color:#475569;margin-top:4px;
                        text-transform:uppercase;letter-spacing:1px;">Categories</div>
          </td>
          <td width="34%" style="padding:16px 0;text-align:center;">
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:26px;font-weight:800;color:#ff4d6d;line-height:1;">
              {critical_count}</div>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:9px;color:#475569;margin-top:4px;
                        text-transform:uppercase;letter-spacing:1px;">Out of Stock</div>
          </td>
        </tr></table>
      </td>
    </tr>

    <!-- SEARCH TIP -->
    <tr>
      <td style="background:#08111f;border-left:1px solid #1e3a5f;
                 border-right:1px solid #1e3a5f;border-top:1px solid #0f1e35;
                 padding:10px 16px;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                     font-size:11px;color:#475569;line-height:1.6;">
            💡 <strong style="color:#64748b;">Search by name:</strong>
            &nbsp;Desktop — press
            <span style="background:#0f172a;color:#94a3b8;padding:1px 6px;
                         border-radius:4px;font-family:monospace;font-size:10px;">Ctrl+F</span>
            &nbsp;· Mobile — tap
            <span style="background:#0f172a;color:#94a3b8;padding:1px 6px;
                         border-radius:4px;font-size:10px;">⋮ → Find in page</span>
            &nbsp;· All names are listed at the bottom of this email.
          </td>
        </tr></table>
      </td>
    </tr>

    <!-- COLUMN HEADERS -->
    <tr>
      <td style="background:#060e1c;border-left:1px solid #1e3a5f;
                 border-right:1px solid #1e3a5f;border-top:1px solid #0f1e35;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr style="background:#04080f;">
            <th width="56" style="padding:8px 4px 8px 16px;text-align:center;
                font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:9px;
                font-weight:700;letter-spacing:1.5px;text-transform:uppercase;
                color:#1e3a5f;">IMG</th>
            <th style="padding:8px;text-align:left;
                font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:9px;
                font-weight:700;letter-spacing:1.5px;text-transform:uppercase;
                color:#1e3a5f;">Product Name</th>
            <th style="padding:8px 16px 8px 8px;text-align:right;
                font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:9px;
                font-weight:700;letter-spacing:1.5px;text-transform:uppercase;
                color:#1e3a5f;">In Stock</th>
          </tr>
        </table>
      </td>
    </tr>

    <!-- PRODUCT ROWS (pre-rendered, grouped by level → category) -->
    <tr>
      <td style="background:#060e1c;border-left:1px solid #1e3a5f;
                 border-right:1px solid #1e3a5f;">
        <table width="100%" cellpadding="0" cellspacing="0">
          {sections_html}
          <!-- bottom padding row -->
          <tr><td colspan="3" style="height:16px;"></td></tr>
        </table>
      </td>
    </tr>

    <!-- PRODUCT NAME INDEX (plain text, Ctrl-F searchable) -->
    <tr>
      <td style="background:#04080f;border-left:1px solid #1e3a5f;
                 border-right:1px solid #1e3a5f;border-top:1px solid #0f1e35;
                 padding:14px 16px;">
        <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:9px;font-weight:700;letter-spacing:1.5px;
                    text-transform:uppercase;color:#1e3a5f;margin-bottom:8px;">
          ▸ Product Name Index — press Ctrl+F to search
        </div>
        <div style="line-height:2.2;">{index_items}</div>
        {overflow_note}
      </td>
    </tr>

    <!-- FOOTER -->
    <tr>
      <td style="background:#04080f;border:1px solid #1e3a5f;
                 border-top:1px solid #0f1e35;border-radius:0 0 16px 16px;
                 padding:14px 24px;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                     font-size:10px;color:#1e3a5f;">
            Auto-generated · Inventory Alert System
          </td>
          <td align="right"
              style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                     font-size:10px;color:#1e3a5f;">
            {low_count} low &nbsp;·&nbsp;
            <span style="color:#ff4d6d;">{critical_count} out</span>
          </td>
        </tr></table>
      </td>
    </tr>

  </table>
  </td></tr>
</table>
</body>
</html>"""


# ─────────────────────────────────────────────
#  SEND — zero attachments
# ─────────────────────────────────────────────
def send_email(html: str, subject: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_FROM
    msg["To"]      = SMTP_TO
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)


# ─────────────────────────────────────────────
#  1. CONNECT
# ─────────────────────────────────────────────
print("🔌 Connecting to Odoo...", flush=True)
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid    = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise Exception("❌ Odoo authentication failed")
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
print("✅ Connected to Odoo", flush=True)


# ─────────────────────────────────────────────
#  2. DETECT QTY FIELD + FETCH
# ─────────────────────────────────────────────
print("🔍 Detecting quantity field...", flush=True)

all_products = None
qty_field    = None

base_fields = ["id", "name", "categ_id", "image_512"]
if IMPORTED_FIELD:
    base_fields.append(IMPORTED_FIELD)

for candidate in QTY_FIELD_CANDIDATES:
    try:
        print(f"   ↳ Trying field: {candidate}", flush=True)
        result = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "search_read",
            [[("active", "=", True), (candidate, "<", LOW_STOCK_THRESHOLD), (candidate, ">=", 0)]],
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
#  3. SORT
# ─────────────────────────────────────────────
all_products.sort(key=lambda p: (
    (p["categ_id"][1] if p.get("categ_id") else "zzz").lower(),
    (p["name"] or "").lower(),
    float(p.get(qty_field) or 0),
))


# ─────────────────────────────────────────────
#  4. IMAGES → DATA URIs
# ─────────────────────────────────────────────
print("🖼️  Converting images to inline data URIs...", flush=True)

hash_to_uri:       dict[str, str] = {}
product_data_uris: dict[int, str] = {}

for p in all_products:
    p_id    = p["id"]
    raw_img = p.get("image_512")
    try:
        if raw_img and str(raw_img) != "False":
            s = raw_img.decode("utf-8") if isinstance(raw_img, bytes) else str(raw_img)
            s = s.replace("\n", "").replace("\r", "").strip()
            b = base64.b64decode(s)
            h = hashlib.md5(b).hexdigest()
            if h not in hash_to_uri:
                hash_to_uri[h] = img_to_data_uri(b)
            product_data_uris[p_id] = hash_to_uri[h]
        else:
            product_data_uris[p_id] = ""
    except Exception as exc:
        print(f"   ⚠️  Bad image for product {p_id}: {exc}", flush=True)
        product_data_uris[p_id] = ""

print(f"   ↳ {len(all_products)} products → {len(hash_to_uri)} unique image(s).", flush=True)


# ─────────────────────────────────────────────
#  5. SPLIT (optional)
# ─────────────────────────────────────────────
if IMPORTED_FIELD:
    groups = []
    imported     = [p for p in all_products if p.get(IMPORTED_FIELD)]
    non_imported = [p for p in all_products if not p.get(IMPORTED_FIELD)]
    if imported:     groups.append(("Imported",     imported))
    if non_imported: groups.append(("Non-Imported", non_imported))
else:
    groups = [("", all_products)]


# ─────────────────────────────────────────────
#  6. BUILD & SEND
# ─────────────────────────────────────────────
print(f"📤 Sending to {SMTP_TO}...", flush=True)

for group_label, products in groups:
    total_products   = len(products)
    total_categories = len({p["categ_id"][0] for p in products if p.get("categ_id")})
    critical_count   = sum(1 for p in products if float(p.get(qty_field) or 0) == 0)
    low_count        = total_products - critical_count
    label_str        = f"[{group_label}]" if group_label else ""

    subject = (
        f"⚠️ Low Stock Alert {label_str}"
        f" — {total_products} product{'s' if total_products != 1 else ''}"
        f" · {critical_count} out of stock"
        f" · {utc_now().strftime('%d %b %Y')}"
    ).strip()

    html = build_html(
        products=products, qty_field=qty_field,
        product_data_uris=product_data_uris, label=label_str,
        total_products=total_products, total_categories=total_categories,
        critical_count=critical_count, low_count=low_count,
    )

    try:
        send_email(html, subject)
        print(
            f"   ✅ '{group_label or 'All'}' email sent "
            f"({total_products} products · {critical_count} out of stock).",
            flush=True,
        )
    except Exception as exc:
        print(f"   ❌ '{group_label or 'All'}' email failed: {exc}", flush=True)
        sys.exit(1)

print("✅ All emails sent successfully!", flush=True)
