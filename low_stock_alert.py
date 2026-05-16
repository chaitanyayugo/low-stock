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
MAX_CID_ATTACHMENTS = 490           # Gmail hard-caps at 500; stay safely under

# Quantity fields to try in priority order
QTY_FIELD_CANDIDATES = [
    "x_avl_custom",
    "x_studio_related_field_6v_1jolles4p",
    "qty_available",
]

# 1×1 transparent PNG fallback
FALLBACK_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
    "DUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

# Deterministic colour palette for letter-avatars
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


def letter_avatar_html(name: str) -> str:
    letter = (name or "?")[0].upper()
    color  = avatar_color(name)
    return (
        f'<div style="width:44px; height:44px; border-radius:10px; '
        f'background:{color}; display:inline-block; text-align:center; '
        f'line-height:44px; font-family:Helvetica,Arial,sans-serif; '
        f'font-size:18px; font-weight:700; color:#fff;">'
        f'{letter}</div>'
    )


def stock_badge(qty: float) -> str:
    if qty == 0:
        return (
            '<span style="background:#1a0005; color:#ff4d6d; padding:4px 12px; '
            'border-radius:20px; font-weight:700; font-size:13px; '
            'letter-spacing:0.5px;">OUT</span>'
        )
    elif qty <= 2:
        return (
            f'<span style="background:#1c0a00; color:#ff8c42; padding:4px 12px; '
            f'border-radius:20px; font-weight:700; font-size:13px;">{qty:g}</span>'
        )
    else:
        return (
            f'<span style="background:#0a1a10; color:#52c41a; padding:4px 12px; '
            f'border-radius:20px; font-weight:700; font-size:13px;">{qty:g}</span>'
        )


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
#  2. AUTO-DETECT QUANTITY FIELD & FETCH PRODUCTS
# ─────────────────────────────────────────────
print("🔍 Detecting available quantity field...", flush=True)

products  = None
qty_field = None

for candidate in QTY_FIELD_CANDIDATES:
    try:
        print(f"   ↳ Trying field: {candidate}", flush=True)
        result = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "search_read",
            [[
                ("active",  "=", True),
                (candidate, "<", LOW_STOCK_THRESHOLD),
                (candidate, ">=", 0),
            ]],
            {
                "fields": ["id", "name", "categ_id", "image_128", candidate],
                "limit":  0,
            },
        )
        products  = result
        qty_field = candidate
        print(f"✅ Using quantity field: '{qty_field}'", flush=True)
        break
    except Exception as exc:
        print(f"   ⚠️  Field '{candidate}' unavailable: {exc}", flush=True)

if products is None or qty_field is None:
    print("❌ None of the quantity fields worked. Aborting.", flush=True)
    sys.exit(1)

# Safety filter: drop nulls / False values that slipped through
products = [
    p for p in products
    if p.get(qty_field) not in (None, False)
    and float(p[qty_field]) < LOW_STOCK_THRESHOLD
]

if not products:
    print("✅ No low-stock products found. Exiting gracefully.", flush=True)
    sys.exit(0)

print(f"📦 Found {len(products)} low-stock product(s).", flush=True)


# ─────────────────────────────────────────────
#  3. SORT: category → name → stock qty
# ─────────────────────────────────────────────
products.sort(key=lambda p: (
    (p["categ_id"][1] if p.get("categ_id") else "zzz").lower(),
    (p["name"] or "").lower(),
    float(p.get(qty_field) or 0),
))


# ─────────────────────────────────────────────
#  4. DEDUPLICATE IMAGES  (no extra API call)
#
#  image_128 was already fetched inside search_read (Step 2).
#  We read it straight from the `products` list already in memory.
#
#    • Hash every image (MD5)
#    • Store only UNIQUE images  →  hash_to_bytes
#    • Map product_id → hash    →  product_hash_map
#    • HTML uses  cid:img_<hash>  so N products sharing one image = 1 attachment
#
#  If unique images still exceed MAX_CID_ATTACHMENTS we switch to
#  letter-avatars (zero attachments) to stay inside Gmail's hard limit.
# ─────────────────────────────────────────────
print("🖼️  Deduplicating product images (no extra API call)...", flush=True)

hash_to_bytes:    dict[str, bytes]      = {}   # img_hash  → raw PNG bytes
product_hash_map: dict[int, str | None] = {}   # p_id      → img_hash | None

for item in products:
    p_id    = item["id"]
    raw_img = item.get("image_128")
    try:
        if raw_img and str(raw_img) != "False":
            img_str   = raw_img.decode("utf-8") if isinstance(raw_img, bytes) else str(raw_img)
            img_str   = img_str.replace("\n", "").replace("\r", "").strip()
            img_bytes = base64.b64decode(img_str)
            img_hash  = hashlib.md5(img_bytes).hexdigest()
            hash_to_bytes[img_hash]   = img_bytes
            product_hash_map[p_id]    = img_hash
        else:
            product_hash_map[p_id] = None
    except Exception as exc:
        print(f"   ⚠️  Bad image for product {p_id}: {exc}", flush=True)
        product_hash_map[p_id] = None

unique_count = len(hash_to_bytes)
print(
    f"   ↳ {len(products)} products → {unique_count} unique image(s) after deduplication.",
    flush=True,
)

if unique_count <= MAX_CID_ATTACHMENTS:
    use_cid = True
    print("   ↳ Mode: CID inline attachments ✅", flush=True)
else:
    use_cid = False
    print(
        f"   ↳ {unique_count} unique images exceeds the {MAX_CID_ATTACHMENTS}-attachment limit.\n"
        f"   ↳ Mode: letter-avatar fallback (no image attachments) ✅",
        flush=True,
    )

print("✅ Images ready.", flush=True)


# ─────────────────────────────────────────────
#  5. BUILD ELITE EMAIL HTML  (grouped by category)
# ─────────────────────────────────────────────
print("📧 Building elite email HTML...", flush=True)

total_products   = len(products)
total_categories = len({p["categ_id"][0] for p in products if p.get("categ_id")})
critical_count   = sum(1 for p in products if float(p.get(qty_field) or 0) == 0)
low_count        = total_products - critical_count
generated_at     = utc_now().strftime("%d %b %Y • %H:%M UTC")

# Group by category
categories: dict[str, list] = defaultdict(list)
for p in products:
    cat_name = p["categ_id"][1] if p.get("categ_id") else "Uncategorised"
    categories[cat_name].append(p)

# Build category sections
sections_html = ""
for cat_name, cat_products in categories.items():
    rows_html = ""
    for p in cat_products:
        p_id  = p["id"]
        qty   = float(p.get(qty_field) or 0)
        badge = stock_badge(qty)

        # Image cell
        if use_cid:
            img_hash = product_hash_map.get(p_id)
            if img_hash:
                img_cell = (
                    f'<div style="width:44px; height:44px; border-radius:10px; '
                    f'overflow:hidden; background:#1e2433; display:inline-block;">'
                    f'<img src="cid:img_{img_hash}" width="44" height="44" '
                    f'style="width:44px;height:44px;object-fit:cover;display:block;" alt="">'
                    f'</div>'
                )
            else:
                img_cell = letter_avatar_html(p["name"] or "?")
        else:
            img_cell = letter_avatar_html(p["name"] or "?")

        rows_html += f"""
        <tr>
          <td style="padding:12px 16px; width:56px; text-align:center; vertical-align:middle;">
            {img_cell}
          </td>
          <td style="padding:12px 8px;
                     font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                     font-size:14px; color:#e2e8f0; font-weight:500;
                     vertical-align:middle;">
            {p['name']}
          </td>
          <td style="padding:12px 16px; text-align:center; vertical-align:middle;">
            {badge}
          </td>
        </tr>
        <tr>
          <td colspan="3" style="padding:0; height:1px;
              background:linear-gradient(90deg, transparent,
              #2d3748 20%, #2d3748 80%, transparent);"></td>
        </tr>
        """

    sections_html += f"""
    <tr>
      <td colspan="3" style="padding:24px 20px 8px;">
        <div style="display:inline-block; background:#0f172a;
                    border:1px solid #334155; border-radius:6px; padding:4px 14px;">
          <span style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                       font-size:11px; font-weight:700; letter-spacing:1.5px;
                       text-transform:uppercase; color:#94a3b8;">{cat_name}</span>
          <span style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                       font-size:11px; color:#475569; margin-left:8px;">
            {len(cat_products)} item{'s' if len(cat_products) != 1 else ''}
          </span>
        </div>
      </td>
    </tr>
    {rows_html}
    """

full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Low Stock Alert</title>
</head>
<body style="margin:0; padding:0; background:#060b14;">

<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#060b14; padding:32px 12px;">
  <tr><td align="center">
  <table width="640" cellpadding="0" cellspacing="0" style="max-width:640px; width:100%;">

    <!-- ══ HEADER ══ -->
    <tr>
      <td style="background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);
                 border-radius:16px 16px 0 0; border:1px solid #1e3a5f;
                 padding:36px 32px 28px;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:11px; font-weight:700; letter-spacing:3px;
                        text-transform:uppercase; color:#38bdf8; margin-bottom:10px;">
              ◈ &nbsp;INVENTORY MANAGEMENT SYSTEM
            </div>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:26px; font-weight:700; color:#f1f5f9;
                        line-height:1.2; margin-bottom:6px;">
              Low Stock Alert
            </div>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:13px; color:#64748b;">
              {generated_at}
              &nbsp;·&nbsp; Threshold: &lt;&nbsp;{LOW_STOCK_THRESHOLD} units
              &nbsp;·&nbsp; Field:
              <code style="color:#94a3b8; font-size:12px;">{qty_field}</code>
            </div>
          </td>
          <td align="right" valign="top">
            <div style="background:#ff4d6d22; border:1px solid #ff4d6d55;
                        border-radius:50%; width:52px; height:52px;
                        text-align:center; line-height:52px; font-size:24px;">⚠</div>
          </td>
        </tr></table>
      </td>
    </tr>

    <!-- ══ METRICS STRIP ══ -->
    <tr>
      <td style="background:#0d1929; border-left:1px solid #1e3a5f;
                 border-right:1px solid #1e3a5f;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td width="33%" style="padding:20px 0; text-align:center;
                                  border-right:1px solid #1e293b;">
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:30px; font-weight:800; color:#f1f5f9; line-height:1;">
              {total_products}</div>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:11px; color:#475569; margin-top:4px;
                        text-transform:uppercase; letter-spacing:1px;">Products</div>
          </td>
          <td width="33%" style="padding:20px 0; text-align:center;
                                  border-right:1px solid #1e293b;">
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:30px; font-weight:800; color:#f1f5f9; line-height:1;">
              {total_categories}</div>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:11px; color:#475569; margin-top:4px;
                        text-transform:uppercase; letter-spacing:1px;">Categories</div>
          </td>
          <td width="34%" style="padding:20px 0; text-align:center;">
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:30px; font-weight:800; color:#ff4d6d; line-height:1;">
              {critical_count}</div>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:11px; color:#475569; margin-top:4px;
                        text-transform:uppercase; letter-spacing:1px;">Out of Stock</div>
          </td>
        </tr></table>
      </td>
    </tr>

    <!-- ══ PRODUCT TABLE ══ -->
    <tr>
      <td style="background:#0a1628; border-left:1px solid #1e3a5f;
                 border-right:1px solid #1e3a5f; border-top:1px solid #1e293b;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr style="background:#080f1e;">
            <th width="56" style="padding:12px 16px; text-align:center;
                font-family:'Segoe UI',Helvetica,Arial,sans-serif; font-size:10px;
                font-weight:700; letter-spacing:1.5px; text-transform:uppercase;
                color:#334155;">IMG</th>
            <th style="padding:12px 8px; text-align:left;
                font-family:'Segoe UI',Helvetica,Arial,sans-serif; font-size:10px;
                font-weight:700; letter-spacing:1.5px; text-transform:uppercase;
                color:#334155;">Product</th>
            <th width="110" style="padding:12px 16px; text-align:center;
                font-family:'Segoe UI',Helvetica,Arial,sans-serif; font-size:10px;
                font-weight:700; letter-spacing:1.5px; text-transform:uppercase;
                color:#334155;">In Stock</th>
          </tr>
          {sections_html}
        </table>
      </td>
    </tr>

    <!-- ══ FOOTER ══ -->
    <tr>
      <td style="background:#080f1e; border:1px solid #1e3a5f;
                 border-top:1px solid #1e293b; border-radius:0 0 16px 16px;
                 padding:20px 32px;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                     font-size:11px; color:#334155;">
            Auto-generated by Inventory Alert System
          </td>
          <td align="right"
              style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                     font-size:11px; color:#334155;">
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

print("✅ HTML built.", flush=True)


# ─────────────────────────────────────────────
#  6. ASSEMBLE & SEND EMAIL
# ─────────────────────────────────────────────
print(f"📤 Sending elite email to {SMTP_TO}...", flush=True)

msg_related = MIMEMultipart("related")
msg_related["Subject"] = (
    f"⚠️ Low Stock Alert — {total_products} product{'s' if total_products != 1 else ''} "
    f"· {critical_count} out of stock · {utc_now().strftime('%d %b %Y')}"
)
msg_related["From"] = SMTP_FROM
msg_related["To"]   = SMTP_TO

msg_alt = MIMEMultipart("alternative")
msg_alt.attach(MIMEText(full_html, "html"))
msg_related.attach(msg_alt)

# Attach UNIQUE images only (if CID mode)
if use_cid:
    attached = 0
    for img_hash, img_bytes in hash_to_bytes.items():
        try:
            img_part = MIMEImage(img_bytes, _subtype="png")
            img_part.add_header("Content-ID",          f"<img_{img_hash}>")
            img_part.add_header("Content-Disposition", "inline",
                                filename=f"img_{img_hash}.png")
            msg_related.attach(img_part)
            attached += 1
        except Exception as exc:
            print(f"   ⚠️  Could not attach image {img_hash}: {exc}", flush=True)
    print(f"   ↳ Attached {attached} unique image(s).", flush=True)

try:
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg_related)
    print("✅ Elite email sent successfully!", flush=True)
except Exception as exc:
    print(f"❌ Failed to send email: {exc}", flush=True)
    sys.exit(1)
