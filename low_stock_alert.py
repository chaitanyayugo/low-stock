import xmlrpc.client
import requests
import base64
import smtplib
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import defaultdict

# ---------- CONFIGURATION (from environment variables) ----------
ODOO_URL = os.environ.get("ODOO_URL")
ODOO_DB = os.environ.get("ODOO_DB")
ODOO_USER = os.environ.get("ODOO_USER")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM = os.environ.get("SMTP_FROM")
SMTP_TO = os.environ.get("SMTP_TO")   # can be comma-separated

# ---------- 1. CONNECT TO ODOO ----------
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# ---------- 2. FETCH LOW STOCK QUANTS ----------
domain = [
    ["location_id.usage", "=", "internal"],
    ["quantity", "<", 5]
]
quants = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
    [domain],
    {"fields": ["id", "product_id", "location_id", "quantity", "reserved_quantity"]}
)

if not quants:
    print("✅ No low-stock products found. Exiting.")
    exit(0)

print(f"📦 Found {len(quants)} low-stock records.")

# ---------- 3. UNIQUE PRODUCT IDs ----------
product_ids = list({q["product_id"][0] for q in quants if q["product_id"]})

# ---------- 4. FETCH IMAGES AS BASE64 ----------
def get_product_image_base64(product_id):
    """Return data:image/png;base64,.... for a given product.id"""
    url = f"{ODOO_URL}/web/image/product.product/{product_id}/image_128"
    session = requests.Session()
    # Basic auth works for Odoo's /web/image endpoint
    session.auth = (ODOO_USER, ODOO_PASSWORD)
    try:
        resp = session.get(url, timeout=10)
        if resp.status_code == 200:
            b64 = base64.b64encode(resp.content).decode('utf-8')
            return f"data:image/png;base64,{b64}"
        else:
            print(f"⚠️ No image for product {product_id}, using placeholder.")
    except Exception as e:
        print(f"⚠️ Error fetching image {product_id}: {e}")
    # Transparent 1x1 pixel placeholder
    return "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

print("🖼️ Downloading product images...")
product_image_map = {}
for pid in product_ids:
    product_image_map[pid] = get_product_image_base64(pid)

# ---------- 5. BUILD EMAIL HTML TABLE ----------
rows = ""
for q in quants:
    product_id = q["product_id"][0]
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

full_html = f"""
<div style="background:#f9fafb; padding:40px 10px; font-family:Helvetica;">
  <div style="max-width:800px; margin:0 auto; background:#fff; border-radius:8px; border:1px solid #e5e7eb;">
    <div style="background:#111827; padding:25px 30px;">
      <h1 style="color:#fff; margin:0;">⚠️ Inventory Alert: Low Stock Report</h1>
      <p style="color:#9ca3af;">The following items are below the threshold of 5 units</p>
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
    追赶
    <div style="padding:20px 30px; background:#fefefe; border-top:1px solid #eee; text-align:right;">
      <p style="margin:0; font-size:12px; color:#9ca3af;">Generated by GitHub Actions • {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M UTC')}</p>
    </div>
  </div>
</div>
"""

# ---------- 6. SEND EMAIL ----------
msg = MIMEMultipart("alternative")
msg["Subject"] = f"Low Stock Alert – {__import__('datetime').datetime.now().strftime('%Y-%m-%d')}"
msg["From"] = SMTP_FROM
msg["To"] = SMTP_TO

html_part = MIMEText(full_html, "html")
msg.attach(html_part)

with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASSWORD)
    server.send_message(msg)

print(f"✅ Email sent successfully to {SMTP_TO}")
