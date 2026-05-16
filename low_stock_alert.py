import sys
import os
import base64
import smtplib
import xmlrpc.client
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from datetime import datetime

print("🟢 Starting low-stock alert script [TEST MODE - 1 SINGLE PRODUCT]...", flush=True)

# ---------- CONFIGURATION ----------
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

# 1x1 transparent PNG used when a product has no image
FALLBACK_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
    "DUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

# ---------- 1. CONNECT TO ODOO ----------
print("🔌 Connecting to Odoo...", flush=True)
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid    = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise Exception("❌ Odoo authentication failed")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
print("✅ Connected to Odoo", flush=True)

# ---------- 2. FETCH LOW STOCK QUANTS ----------
print("📦 Fetching exactly 1 low-stock record for testing...", flush=True)
domain = [
    ("location_id.usage", "=", "internal"),
    ("quantity", "<", LOW_STOCK_THRESHOLD),
]

quants = models.execute_kw(
    ODOO_DB, uid, ODOO_PASSWORD,
    "stock.quant", "search_read",
    [domain],
    {"fields": ["id", "product_id", "location_id", "quantity"], "limit": 1},
)

quants = quants[:1]

if not quants:
    print("✅ No low-stock products found. Exiting gracefully.", flush=True)
    sys.exit(0)

product_ids    = list({q["product_id"][0] for q in quants if q["product_id"]})
total_products = len(product_ids)
print(f"📦 Found {len(quants)} low-stock record. Will process {total_products} unique product.", flush=True)

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
