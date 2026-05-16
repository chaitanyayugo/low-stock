import xmlrpc.client
import requests
import base64
import smtplib
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

print("🟢 Starting low‑stock alert script...", flush=True)

# ---------- CONFIGURATION ----------
ODOO_URL = os.environ.get("ODOO_URL", "https://your-odoo-url.com")
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
domain = [["location_id.usage", "=", "internal"], ["quantity", "<", 1]]
quants = models.execute_kw(
    ODOO_DB,
    uid,
    ODOO_PASSWORD,
    "stock.quant",
    "search_read",
    [domain],
    {"fields": ["id", "product_id", "location_id", "quantity", "reserved_quantity"]}
)

if not quants:
    print("✅ No low‑stock products found. Exiting.", flush=True)
    exit(0)

print(
    f"📦 Found {len(quants)} low‑stock records. Unique products to process: "
    f"{len(set(q['product_id'][0] for q in quants if q['product_id']))}",
    flush=True,
)

# ---------- 3. UNIQUE PRODUCT IDs (LIMIT TO 2) ----------
unique_product_ids = {q["product_id"][0] for q in quants if q["product_id"]}
product_ids = list(unique_product_ids)[:2]  # Only 2 products
total_products = len(product_ids)
print(f"🖼️ Will download images for {total_products} products (limited to top 2).", flush=True)

# ---------- 4. FETCH IMAGES WITH PROGRESS ----------
def get_product_image_base64(product_id):
    url = f"{ODOO_URL}/web/image/product.product/{product_id}/image_128"
    session = requests.Session()
    # Use Odoo basic auth (works with SaaS if auth is allowed to robots / API)
    session.auth = (ODOO_USER, ODOO_PASSWORD)
    try:
        resp = session.get(url, timeout=10)
        if resp.status_code == 200:
            b64 = base64.b64encode(resp.content).decode("utf-8")
            return f"data:image/png;base64,{b64}"
    except Exception as e:
        print(f"⚠️ Failed to fetch image for product {product_id}: {e}")
        pass
    # transparent placeholder
    return (
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

product_image_map = {}
for idx, pid in enumerate(product_ids, start=1):
    product_image_map[pid] = get_product_image_base64(pid)
    if idx % 10 == 0 or idx == total_products:
        print(f"   Progress: {idx}/{total_products} product images downloaded", flush=True)
print("✅ All images downloaded", flush=True)

# ---------- 5. BUILD EMAIL HTML (ONLY 2 PRODUCTS) ----------
print("📧 Building email HTML...", flush=True)
rows = ""
products_shown = 0
for q in quants:
    product_id = q["product_id"][0]
    if product_id not in product_image_map:
        continue  # skip products not in our 2‑product list
    if products_shown >= 2:
        continue  # only 2 rows

    product_name = q["product_id"][1]
    location_name = q["location_id"][1] if q["location_id"] else "Unknown"
    quantity = q["quantity"]
    reserved = q["reserved_quantity"]
    img_src = product_image_map.get(product_id, "")
    rows += f"""
    <tr style="border-bottom:1px solid #eee;">
        <td style="padding:12px 15px; text-align:center;">
            <img src="{img_src}" style="width:40px; height:40px; object-fit:cover; border-radius:6px;">
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
    products_shown += 1

full_html = f"""
<div style="background:#f9fafb; padding:40px 10px; font-family:Helvetica;">
  <div style="max-width:800px; margin:0 auto; background:#fff; border-radius:8px; border:1px solid #e5e7eb;">
    <div style="background:#111827; padding:25px 30px;">
      <h1 style="color:#fff; margin:0;">⚠️ Inventory Alert: Low Stock Report</h1>
      <p style="color:#9ca3af;">The following items are below threshold of 5 units</p>
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
      <p style="margin:0; font-size:12px; color:#9ca3af;">
        Generated by GitHub Actions • {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
      </p>
    </div>
  </div>
</div>
"""

# ---------- 6. SEND EMAIL ----------
print(f"📤 Sending email to {SMTP_TO}...", flush=True)
msg = MIMEMultipart("alternative")
msg["Subject"] = f"Low Stock Alert – {datetime.utcnow().strftime('%Y-%m-%d')}"
msg["From"] = SMTP_FROM
msg["To"] = SMTP_TO
msg.attach(MIMEText(full_html, "html"))

try:
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    print("✅ Email sent successfully", flush=True)
except Exception as e:
    print(f"❌ Failed to send email: {e}")
    raise
