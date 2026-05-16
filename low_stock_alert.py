import sys
import os
import base64
import smtplib
import xmlrpc.client
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from datetime import datetime

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

# Quantity fields to try, in priority order
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

products      = None
qty_field     = None

for candidate in QTY_FIELD_CANDIDATES:
    try:
        print(f"   ↳ Trying field: {candidate}", flush=True)
        result = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "search_read",
            [[
                ("active",    "=", True),
                (candidate,   "<", LOW_STOCK_THRESHOLD),
                (candidate,   ">=", 0),          # exclude negative / undefined
            ]],
            {
                "fields": [
                    "id",
                    "name",
                    "categ_id",
                    "image_128",
                    candidate,
                ],
                "limit": 0,                      # fetch ALL matching records
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

# Safety filter in Python (handles nulls / False values from Odoo)
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
#  3. SORT: category name → product name → stock qty
# ─────────────────────────────────────────────
products.sort(key=lambda p: (
    (p["categ_id"][1] if p.get("categ_id") else "zzz_no_category").lower(),
    (p["name"] or "").lower(),
    float(p.get(qty_field) or 0),
))


# ─────────────────────────────────────────────
#  4. FETCH PRODUCT IMAGES
# ─────────────────────────────────────────────
print("🖼️  Fetching product images...", flush=True)

product_ids  = [p["id"] for p in products]
image_data   = models.execute_kw(
    ODOO_DB, uid, ODOO_PASSWORD,
    "product.product", "read",
    [product_ids],
    {"fields": ["id", "image_128"]},
)

product_image_map = {}
for item in image_data:
    p_id    = item["id"]
    raw_img = item.get("image_128")
    try:
        if raw_img and str(raw_img) != "False":
            img_str = raw_img.decode("utf-8") if isinstance(raw_img, bytes) else str(raw_img)
            img_str = img_str.replace("\n", "").replace("\r", "").strip()
            base64.b64decode(img_str)            # validate
        else:
            img_str = FALLBACK_B64
    except Exception as exc:
        print(f"   ⚠️  Bad image for product {p_id}: {exc}", flush=True)
        img_str = FALLBACK_B64
    product_image_map[p_id] = img_str

print("✅ Images ready.", flush=True)


# ─────────────────────────────────────────────
#  5. BUILD ELITE EMAIL HTML  (grouped by category)
# ─────────────────────────────────────────────
print("📧 Building elite email HTML...", flush=True)

# ── summary metrics ──────────────────────────
total_products   = len(products)
total_categories = len({p["categ_id"][0] for p in products if p.get("categ_id")})
critical_count   = sum(1 for p in products if float(p.get(qty_field) or 0) == 0)
low_count        = total_products - critical_count
generated_at     = datetime.utcnow().strftime("%d %b %Y • %H:%M UTC")

# ── stock-level badge helper ─────────────────
def stock_badge(qty):
    q = float(qty)
    if q == 0:
        return (
            '<span style="background:#1a0005; color:#ff4d6d; padding:4px 12px; '
            'border-radius:20px; font-weight:700; font-size:13px; '
            'letter-spacing:0.5px;">OUT</span>'
        )
    elif q <= 2:
        return (
            f'<span style="background:#1c0a00; color:#ff8c42; padding:4px 12px; '
            f'border-radius:20px; font-weight:700; font-size:13px;">{q:g}</span>'
        )
    else:
        return (
            f'<span style="background:#0a1a10; color:#52c41a; padding:4px 12px; '
            f'border-radius:20px; font-weight:700; font-size:13px;">{q:g}</span>'
        )

# ── group products by category ───────────────
from collections import defaultdict
categories = defaultdict(list)
for p in products:
    cat_name = p["categ_id"][1] if p.get("categ_id") else "Uncategorised"
    categories[cat_name].append(p)

# ── build category sections ──────────────────
sections_html = ""
for cat_name, cat_products in categories.items():
    rows_html = ""
    for p in cat_products:
        p_id   = p["id"]
        qty    = p.get(qty_field, 0)
        badge  = stock_badge(qty)

        rows_html += f"""
        <tr>
          <td style="padding:14px 16px; width:56px; text-align:center; vertical-align:middle;">
            <div style="width:44px; height:44px; border-radius:10px; overflow:hidden;
                        background:#1e2433; display:inline-block; vertical-align:middle;">
              <img src="cid:product_{p_id}"
                   width="44" height="44"
                   style="width:44px; height:44px; object-fit:cover; display:block;"
                   alt="">
            </div>
          </td>
          <td style="padding:14px 8px; font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                     font-size:14px; color:#e2e8f0; font-weight:500;
                     vertical-align:middle;">
            {p['name']}
          </td>
          <td style="padding:14px 16px; text-align:center; vertical-align:middle;">
            {badge}
          </td>
        </tr>
        <tr>
          <td colspan="3"
              style="padding:0; height:1px; background:linear-gradient(90deg,
                     transparent, #2d3748 20%, #2d3748 80%, transparent);"></td>
        </tr>
        """

    sections_html += f"""
    <!-- CATEGORY: {cat_name} -->
    <tr>
      <td colspan="3" style="padding:24px 20px 8px;">
        <div style="display:inline-block; background:#0f172a; border:1px solid #334155;
                    border-radius:6px; padding:4px 14px;">
          <span style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                       font-size:11px; font-weight:700; letter-spacing:1.5px;
                       text-transform:uppercase; color:#94a3b8;">
            {cat_name}
          </span>
          <span style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                       font-size:11px; color:#475569; margin-left:8px;">
            {len(cat_products)} item{'s' if len(cat_products) != 1 else ''}
          </span>
        </div>
      </td>
    </tr>
    {rows_html}
    """

# ── assemble full email ──────────────────────
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
  <tr>
    <td align="center">
      <table width="640" cellpadding="0" cellspacing="0"
             style="max-width:640px; width:100%;">

        <!-- ══ HEADER ══ -->
        <tr>
          <td style="background:linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
                     border-radius:16px 16px 0 0;
                     border:1px solid #1e3a5f;
                     padding:36px 32px 28px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td>
                  <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                              font-size:11px; font-weight:700; letter-spacing:3px;
                              text-transform:uppercase; color:#38bdf8;
                              margin-bottom:10px;">
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
                    &nbsp;·&nbsp;
                    Threshold: &lt; {LOW_STOCK_THRESHOLD} units
                    &nbsp;·&nbsp;
                    Field: <code style="color:#94a3b8; font-size:12px;">{qty_field}</code>
                  </div>
                </td>
                <td align="right" valign="top">
                  <div style="background:#ff4d6d22; border:1px solid #ff4d6d55;
                              border-radius:50%; width:52px; height:52px;
                              text-align:center; line-height:52px; font-size:24px;">
                    ⚠
                  </div>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- ══ METRICS STRIP ══ -->
        <tr>
          <td style="background:#0d1929; border-left:1px solid #1e3a5f;
                     border-right:1px solid #1e3a5f; padding:0;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td width="33%" style="padding:20px 0; text-align:center;
                                        border-right:1px solid #1e293b;">
                  <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                              font-size:30px; font-weight:800; color:#f1f5f9;
                              line-height:1;">{total_products}</div>
                  <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                              font-size:11px; color:#475569; margin-top:4px;
                              text-transform:uppercase; letter-spacing:1px;">
                    Products
                  </div>
                </td>
                <td width="33%" style="padding:20px 0; text-align:center;
                                        border-right:1px solid #1e293b;">
                  <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                              font-size:30px; font-weight:800; color:#f1f5f9;
                              line-height:1;">{total_categories}</div>
                  <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                              font-size:11px; color:#475569; margin-top:4px;
                              text-transform:uppercase; letter-spacing:1px;">
                    Categories
                  </div>
                </td>
                <td width="34%" style="padding:20px 0; text-align:center;">
                  <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                              font-size:30px; font-weight:800; color:#ff4d6d;
                              line-height:1;">{critical_count}</div>
                  <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                              font-size:11px; color:#475569; margin-top:4px;
                              text-transform:uppercase; letter-spacing:1px;">
                    Out of Stock
                  </div>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- ══ TABLE HEADER ══ -->
        <tr>
          <td style="background:#0a1628; border-left:1px solid #1e3a5f;
                     border-right:1px solid #1e3a5f;
                     border-top:1px solid #1e293b;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr style="background:#080f1e;">
                <th width="56" style="padding:12px 16px; text-align:center;
                    font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:10px; font-weight:700; letter-spacing:1.5px;
                    text-transform:uppercase; color:#334155;">IMG</th>
                <th style="padding:12px 8px; text-align:left;
                    font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:10px; font-weight:700; letter-spacing:1.5px;
                    text-transform:uppercase; color:#334155;">Product</th>
                <th width="110" style="padding:12px 16px; text-align:center;
                    font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:10px; font-weight:700; letter-spacing:1.5px;
                    text-transform:uppercase; color:#334155;">In Stock</th>
              </tr>

              <!-- CATEGORY SECTIONS -->
              {sections_html}

            </table>
          </td>
        </tr>

        <!-- ══ FOOTER ══ -->
        <tr>
          <td style="background:#080f1e; border:1px solid #1e3a5f;
                     border-top:1px solid #1e293b;
                     border-radius:0 0 16px 16px; padding:20px 32px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
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
              </tr>
            </table>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>

</body>
</html>"""

print("✅ HTML built.", flush=True)


# ─────────────────────────────────────────────
#  6. ASSEMBLE & SEND EMAIL  (multipart/related + CID images)
# ─────────────────────────────────────────────
print(f"📤 Sending elite email to {SMTP_TO}...", flush=True)

msg_related = MIMEMultipart("related")
msg_related["Subject"] = (
    f"⚠️ Low Stock Alert — {total_products} product{'s' if total_products != 1 else ''} "
    f"· {critical_count} out of stock · {datetime.utcnow().strftime('%d %b %Y')}"
)
msg_related["From"] = SMTP_FROM
msg_related["To"]   = SMTP_TO

# HTML body
msg_alt = MIMEMultipart("alternative")
msg_alt.attach(MIMEText(full_html, "html"))
msg_related.attach(msg_alt)

# Inline image attachment per product
for p in products:
    p_id    = p["id"]
    b64_str = product_image_map.get(p_id, FALLBACK_B64)
    try:
        img_bytes = base64.b64decode(b64_str)
        img_part  = MIMEImage(img_bytes, _subtype="png")
        img_part.add_header("Content-ID",          f"<product_{p_id}>")
        img_part.add_header("Content-Disposition", "inline",
                            filename=f"product_{p_id}.png")
        msg_related.attach(img_part)
    except Exception as exc:
        print(f"   ⚠️  Could not attach image for product {p_id}: {exc}", flush=True)

# Send
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
# ---------- 3. BULK FETCH IMAGES VIA XML-RPC ----------
print("🖼️  Fetching and sanitising product image...", flush=True)

product_data = models.execute_kw(
    ODOO_DB, uid, ODOO_PASSWORD,
    "product.product", "read",
    [product_ids],
    {"fields": ["id", "image_128"]},
)

# product_image_map  →  { product_id: <plain base64 string, no prefix> }
product_image_map = {}

for p in product_data:
    p_id = p["id"]
    try:
        raw_img = p.get("image_128")

        if raw_img and str(raw_img) != "False":
            # Odoo may return bytes or str depending on the xmlrpc library version
            if isinstance(raw_img, bytes):
                img_str = raw_img.decode("utf-8")
            else:
                img_str = str(raw_img)

            # Strip whitespace / newlines that break base64 decoding
            img_str = img_str.replace("\n", "").replace("\r", "").strip()

            # Validate: attempt a decode so we catch bad data early
            base64.b64decode(img_str)
        else:
            img_str = FALLBACK_B64

    except Exception as e:
        print(f"⚠️  Image issue for product ID {p_id}: {e}. Using fallback.", flush=True)
        img_str = FALLBACK_B64

    product_image_map[p_id] = img_str   # plain base64, NO data-URI prefix

print("✅ Image processed safely", flush=True)

# ---------- 4. BUILD EMAIL HTML ----------
# Images are referenced as  cid:product_<id>  — resolved by the attached MIMEImage parts.
print("📧 Building email HTML...", flush=True)

rows = ""
for q in quants:
    product_id   = q["product_id"][0]
    product_name = q["product_id"][1]
    location_name = q["location_id"][1] if q["location_id"] else "Unknown"
    quantity_fmt  = f"{float(q['quantity']):g}"

    rows += f"""
    <tr style="border-bottom:1px solid #eee;">
        <td style="padding:12px 15px; text-align:center;">
            <img src="cid:product_{product_id}"
                 style="width:40px; height:40px; object-fit:cover;
                        border-radius:6px; background-color:#f9fafb;"
                 alt="Product image">
        </td>
        <td style="padding:12px 15px; font-family:Helvetica; font-size:14px; font-weight:500;">
            {product_name}
        </td>
        <td style="padding:12px 15px;">
            <span style="background:#f3f4f6; padding:4px 8px; border-radius:4px;
                         font-family:Helvetica; font-size:12px;">
                {location_name}
            </span>
        </td>
        <td style="padding:12px 15px; text-align:center; font-family:Helvetica; font-size:14px;">
            <span style="background:#fff1f0; color:#cf1322; padding:4px 10px;
                         border-radius:12px; font-weight:bold;">
                {quantity_fmt}
            </span>
        </td>
    </tr>
    """

full_html = f"""
<div style="background:#f9fafb; padding:40px 10px; font-family:Helvetica, sans-serif;">
  <div style="max-width:800px; margin:0 auto; background:#fff;
              border-radius:8px; border:1px solid #e5e7eb;">

    <div style="background:#111827; padding:25px 30px;">
      <h1 style="color:#fff; margin:0; font-size:20px;">
        ⚠️ TEST REPORT — Inventory Alert: Low Stock
      </h1>
      <p style="color:#9ca3af; font-size:14px; margin:6px 0 0;">
        The following items are below the threshold of {LOW_STOCK_THRESHOLD} units
      </p>
    </div>

    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
      <thead>
        <tr style="background:#f8fafc; border-bottom:2px solid #e5e7eb; color:#4b5563;">
          <th style="padding:15px; text-align:center; width:60px;">Image</th>
          <th style="padding:15px; text-align:left;">Product Name</th>
          <th style="padding:15px; text-align:left;">Location</th>
          <th style="padding:15px; text-align:center;">In Stock</th>
        </tr>
      </thead>
      <tbody>
        {rows}
      </tbody>
    </table>

    <div style="padding:20px 30px; background:#fefefe;
                border-top:1px solid #eee; text-align:right;">
      <p style="margin:0; font-size:12px; color:#9ca3af;">
        Generated by Test Script •
        {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
      </p>
    </div>

  </div>
</div>
"""

# ---------- 5. ASSEMBLE & SEND EMAIL ----------
# Structure:
#   multipart/related          ← ties HTML to its inline images
#   ├── multipart/alternative  ← best practice wrapper for the HTML body
#   │   └── text/html
#   └── image/png (cid:product_<id>)  ← one part per product
print(f"📤 Sending test email to {SMTP_TO}...", flush=True)

msg_related = MIMEMultipart("related")
msg_related["Subject"] = f"[TEST] Low Stock Alert – {datetime.utcnow().strftime('%Y-%m-%d')}"
msg_related["From"]    = SMTP_FROM
msg_related["To"]      = SMTP_TO

# HTML body wrapped in alternative
msg_alt = MIMEMultipart("alternative")
msg_alt.attach(MIMEText(full_html, "html"))
msg_related.attach(msg_alt)

# Attach one inline image per product
for q in quants:
    product_id = q["product_id"][0]
    b64_str    = product_image_map.get(product_id, FALLBACK_B64)

    try:
        img_bytes = base64.b64decode(b64_str)
        img_part  = MIMEImage(img_bytes, _subtype="png")
        img_part.add_header("Content-ID",          f"<product_{product_id}>")
        img_part.add_header("Content-Disposition", "inline",
                            filename=f"product_{product_id}.png")
        msg_related.attach(img_part)
    except Exception as e:
        print(f"⚠️  Could not attach image for product {product_id}: {e}", flush=True)

# Send
try:
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg_related)
    print("✅ Test email sent successfully!", flush=True)
except Exception as e:
    print(f"❌ Failed to send email: {e}", flush=True)
    sys.exit(1)
