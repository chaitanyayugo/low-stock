import xmlrpc.client
import requests
import base64
import smtplib
import os
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

print("🟢 Starting low‑stock alert script (TEST MODE - 10 products, real images)...", flush=True)

# ---------- CONFIGURATION ----------
ODOO_URL = os.environ.get("ODOO_URL")
ODOO_DB = os.environ.get("ODOO_DB")
ODOO_USER = os.environ.get("ODOO_USER")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD")

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM = os.environ.get("SMTP_FROM")
SMTP_TO = os.environ.get("SMTP_TO")

# ---------- 1. CONNECT TO ODOO ----------
print("🔌 Connecting to Odoo...", flush=True)
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise Exception("❌ Odoo authentication failed")
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
print("✅ Connected to Odoo", flush=True)

# ---------- 2. FETCH LOW STOCK QUANTS ----------
print("📦 Fetching low‑stock quants...", flush=True)
domain = [
    ["location_id.usage", "=", "internal"],
    ["quantity", "<", 5]
]
quants = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
    [domain],
    {"fields": ["id", "product_id", "location_id", "quantity", "reserved_quantity"]}
)

if not quants:
    print("✅ No low‑stock products found. Exiting.", flush=True)
    exit(0)

print(f"📦 Found {len(quants)} low‑stock records.", flush=True)

# ---------- 3. SORT AND TAKE TOP 10 ----------
quants_sorted = sorted(quants, key=lambda q: q['quantity'])
TOP_LIMIT = 10
quants_limited = quants_sorted[:TOP_LIMIT]

# Get unique product IDs from limited list
product_ids = list({q["product_id"][0] for q in quants_limited if q["product_id"]})
print(f"🖼️ Will download real images for {len(product_ids)} products.", flush=True)

# ---------- 4. FETCH REAL IMAGES (Base64) ----------
def fetch_product_image(product_id):
    """Return Base64 image data (real if exists, else empty string)"""
    url = f"{ODOO_URL}/web/image/product.product/{product_id}/image_128"
    session = requests.Session()
    session.auth = (ODOO_USER, ODOO_PASSWORD)
    try:
        resp = session.get(url, timeout=10)
        if resp.status_code == 200 and len(resp.content) > 100:  # valid image
            b64 = base64.b64encode(resp.content).decode('utf-8')
            return f"data:image/png;base64,{b64}"
        else:
            # No image in Odoo – return empty string (will show broken icon)
            return ""
    except Exception as e:
        print(f"   ⚠️ Error fetching image for product {product_id}: {e}", flush=True)
        return ""

# Build a dictionary of product_id -> image_src (real or empty)
product_image_map = {}
for idx, pid in enumerate(product_ids, 1):
    product_image_map[pid] = fetch_product_image(pid)
    print(f"   Processed image {idx}/{len(product_ids)}", flush=True)
print("✅ Image fetching completed", flush=True)

# ---------- 5. BUILD EMAIL HTML ----------
print("📧 Building email HTML...", flush=True)
rows = ""
for q in quants_limited:
    product_id = q["product_id"][0]
    product_name = q["product_id"][1]
    location_name = q["location_id"][1] if q["location_id"] else "Unknown"
    quantity = q["quantity"]
    reserved = q["reserved_quantity"]
    img_src = product_image_map.get(product_id, "")
    
    # If img_src is empty, show a placeholder text instead of broken icon
    img_tag = f'<img src="{img_src}" style="width:40px; height:40px; object-fit:cover; border-radius:6px;">' if img_src else '<span style="display:inline-block; width:40px; height:40px; background:#f0f0f0; border-radius:6px; text-align:center; line-height:40px; font-size:10px;">📷</span>'
    
    rows += f"""
    <tr style="border-bottom:1px solid #eee;">
        <td style="padding:12px 15px; text-align:center;">
            {img_tag}
        </td>
        <td style="padding:12px 15px; font-family:Helvetica; font-size:14px; font-weight:500;">
            {product_name}
        </td>
        <td style="padding:12px 15px;">
            <span style="background:#f3f4f6; padding:4px 8px; border-radius:4px;">{location_name}</span>
        </td>
        <td style="padding:12px 15px; text-align:center;">
            <span style="background:#fff1f0; color:#cf1322; padding:4px 10px; border-radius:12px; font-weight:bold;">
                {quantity}
            </span>
        </td>
        <td style="padding:12px 15px; text-align:center; color:#999;">
            {reserved}
        </td>
    </tr>
    """

full_html = f"""
<div style="background:#f9fafb; padding:40px 10px; font-family:Helvetica;">
  <div style="max-width:800px; margin:0 auto; background:#fff; border-radius:8px; border:1px solid #e5e7eb;">
    <div style="background:#111827; padding:25px 30px;">
      <h1 style="color:#fff; margin:0;">⚠️ Low Stock Alert (Top 10)</h1>
      <p style="color:#9ca3af;">Showing {len(quants_limited)} lowest‑stock items out of {len(quants)} total.</p>
    </div>
    <table style="width:100%; border-collapse:collapse;">
      <thead>
        <tr style="background:#f8fafc; border-bottom:2px solid #e5e7eb;">
          <th style="padding:15px; text-align:center;">Image</th>
          <th style="padding:15px; text-align:left;">Product Name</th>
          <th style="padding:15px; text-align:left;">Location</th>
          <th style="padding:15px; text-align:center;">In Stock</th>
          <th style="padding:15px; text-align:center;">Reserved</th>
        </tr>
      </thead>
      <tbody>
        {rows}
      </tbody>
    </table>
    <div style="padding:20px 30px; background:#fefefe; border-top:1px solid #eee; text-align:right;">
      <p style="margin:0; font-size:12px; color:#9ca3af;">Generated by GitHub Actions • {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M UTC')}</p>
    </div>
  </div>
</div>
"""

# ---------- 6. SEND EMAIL WITH RETRY ----------
print(f"📤 Sending email to {SMTP_TO}...", flush=True)
msg = MIMEMultipart("alternative")
msg["Subject"] = f"Low Stock Alert (Top 10) – {__import__('datetime').datetime.now().strftime('%Y-%m-%d')}"
msg["From"] = SMTP_FROM
msg["To"] = SMTP_TO
msg.attach(MIMEText(full_html, "html"))

for attempt in range(3):
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        print("✅ Email sent successfully", flush=True)
        break
    except Exception as e:
        print(f"   Attempt {attempt+1} failed: {e}", flush=True)
        if attempt < 2:
            print("   Waiting 10 seconds...", flush=True)
            time.sleep(10)
        else:
            raise        <td style="padding:12px 15px; text-align:center;">
            <span style="background:#fff1f0; color:#cf1322; padding:4px 10px; border-radius:12px; font-weight:bold;">
                {quantity}
            </span>
        </td>
        <td style="padding:12px 15px; text-align:center; color:#999;">
            {reserved}
        </td>
    </tr>
    """

full_html = f"""
<div style="background:#f9fafb; padding:40px 10px; font-family:Helvetica;">
  <div style="max-width:800px; margin:0 auto; background:#fff; border-radius:8px; border:1px solid #e5e7eb;">
    <div style="background:#111827; padding:25px 30px;">
      <h1 style="color:#fff; margin:0;">⚠️ TEST: Low Stock Alert (Top 10 - All Products)</h1>
      <p style="color:#9ca3af;">Showing only the 10 lowest‑stock items out of {len(quants)} total.</p>
    </div>
    <table style="width:100%; border-collapse:collapse;">
      <thead>
        <tr style="background:#f8fafc; border-bottom:2px solid #e5e7eb;">
          <th style="padding:15px; text-align:center;">Image</th>
          <th style="padding:15px; text-align:left;">Product Name</th>
          <th style="padding:15px; text-align:left;">Location</th>
          <th style="padding:15px; text-align:center;">In Stock</th>
          <th style="padding:15px; text-align:center;">Reserved</th>
        </tr>
      </thead>
      <tbody>
        {rows}
      </tbody>
    </table>
    <div style="padding:20px 30px; background:#fefefe; border-top:1px solid #eee; text-align:right;">
      <p style="margin:0; font-size:12px; color:#9ca3af;">Generated by GitHub Actions (TEST - no archived filter) • {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M UTC')}</p>
    </div>
  </div>
</div>
"""

# ---------- 6. SEND EMAIL WITH RETRY ----------
print(f"📤 Sending email to {SMTP_TO}...", flush=True)
msg = MIMEMultipart("alternative")
msg["Subject"] = f"TEST: Low Stock Alert (Top 10) – {__import__('datetime').datetime.now().strftime('%Y-%m-%d')}"
msg["From"] = SMTP_FROM
msg["To"] = SMTP_TO
msg.attach(MIMEText(full_html, "html"))

for attempt in range(3):
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        print("✅ Email sent successfully", flush=True)
        break
    except Exception as e:
        print(f"   Attempt {attempt+1} failed: {e}", flush=True)
        if attempt < 2:
            print("   Waiting 10 seconds before retry...", flush=True)
            time.sleep(10)
        else:
            raise
