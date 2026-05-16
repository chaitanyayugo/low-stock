import sys
import os
import smtplib
import xmlrpc.client
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

print("🟢 Starting low-stock alert script [TEST MODE - 1 PRODUCT]...", flush=True)

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

LOW_STOCK_THRESHOLD = 5

# Sanity Check for required env variables
if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD, SMTP_HOST, SMTP_TO]):
    raise Exception("❌ Missing required Environment Variables for Odoo or SMTP.")

# ---------- 1. CONNECT TO ODOO ----------
print("🔌 Connecting to Odoo...", flush=True)
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise Exception("❌ Odoo authentication failed")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
print("✅ Connected to Odoo", flush=True)

# ---------- 2. FETCH LOW STOCK QUANTS (LIMITED TO 1 FOR TEST) ----------
print(f"📦 Fetching 1 quant with stock < {LOW_STOCK_THRESHOLD}...", flush=True)
domain = [
    ("location_id.usage", "=", "internal"),
    ("quantity", "<", LOW_STOCK_THRESHOLD)
]
quants = models.execute_kw(
    ODOO_DB,
    uid,
    ODOO_PASSWORD,
    "stock.quant",
    "search_read",
    [domain],
    {
        "fields": ["id", "product_id", "location_id", "quantity", "reserved_quantity"],
        "limit": 1  # 🔴 ENFORCING ONLY 1 PRODUCT FOR THIS TEST SCRIPT
    }
)

if not quants:
    print("✅ No low-stock products found. Exiting gracefully.", flush=True)
    sys.exit(0)

# Build a unique set of product IDs
product_ids = list({q["product_id"][0] for q in quants if q["product_id"]})
total_products = len(product_ids)
print(f"📦 Found {len(quants)} low-stock records across {total_products} unique products.", flush=True)

# ---------- 3. BULK FETCH IMAGES VIA XML-RPC ----------
print("🖼️ Fetching product images natively through XML-RPC...", flush=True)

product_data = models.execute_kw(
    ODOO_DB,
    uid,
    ODOO_PASSWORD,
    "product.product",
    "read",
    [product_ids],
    {"fields": ["id", "image_128"]}
)

product_image_map = {}
fallback_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

for p in product_data:
    p_id = p["id"]
    img_b64 = p.get("image_128") or fallback_b64
    
    if isinstance(img_b64, bytes):
        img_b64 = img_b64.decode("utf-8")
        
    product_image_map[p_id] = f"data:image/png;base64,{img_b64}"

print("✅ All images securely retrieved", flush=True)

# ---------- 4. BUILD EMAIL HTML ----------
print("📧 Building email HTML...", flush=True)
rows = ""
for q in quants:
    product_id = q["product_id"][0]
    product_name = q["product_id"][1]
    location_name = q["location_id"][1] if q["location_id"] else "Unknown"
    
    # Cast to integer formatting so Odoo's standard `2.0` floats look like `2` in the UI
    quantity_fmt = f"{float(q['quantity']):g}" 
    reserved_fmt = f"{float(q['reserved_quantity']):g}" 

    img_src = product_image_map.get(product_id, f"data:image/png;base64,{fallback_b64}")

    rows += f"""
    <tr style="border-bottom:1px solid #eee;">
        <td style="padding:12px 15px; text-align:center;">
            <img src="{img_src}" style="width:40px; height:40px; object-fit:cover; border-radius:6px;" alt="Product Image">
        </td>
        <td style="padding:12px 15px; font-family:Helvetica; font-size:14px; font-weight:500;">
            {product_name}
        </td>
        <td style="padding:12px 15px;">
            <span style="background:#f3f4f6; padding:4px 8px; border-radius:4px; font-family:Helvetica; font-size:12px;">
                {location_name}
            </span>
        </td>
        <td style="padding:12px 15px; text-align:center; font-family:Helvetica; font-size:14px;">
            <span style="background:#fff1f0; color:#cf1322; padding:4px 10px; border-radius:12px; font-weight:bold;">
                {quantity_fmt}
            </span>
        </td>
        <td style="padding:12px 15px; text-align:center; color:#999; font-family:Helvetica; font-size:14px;">
            {reserved_fmt}
        </td>
    </tr>
    """

full_html = f"""
<div style="background:#f9fafb; padding:40px 10px; font-family:Helvetica, sans-serif;">
  <div style="max-width:800px; margin:0 auto; background:#fff; border-radius:8px; border:1px solid #e5e7eb;">
    <div style="background:#111827; padding:25px 30px;">
      <h1 style="color:#fff; margin:0; font-size: 20px;">⚠️ TEST - Inventory Alert: Low Stock Report</h1>
      <p style="color:#9ca3af; font-size:14px;">The following items are below threshold of {LOW_STOCK_THRESHOLD} units</p>
    </div>
    <table style="width:100%; border-collapse:collapse; margin-bottom: 20px;">
      <thead>
        <tr style="background:#f8fafc; border-bottom:2px solid #e5e7eb; color: #4b5563;">
          <th style="padding:15px; text-align:center; width:60px;">Image</th>
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
        Generated Script Run • {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
      </p>
    </div>
  </div>
</div>
"""

# ---------- 5. SEND EMAIL ----------
print(f"📤 Sending email to {SMTP_TO}...", flush=True)
msg = MIMEMultipart("alternative")
msg["Subject"] = f"[TEST] Low Stock Alert – {datetime.utcnow().strftime('%Y-%m-%d')}"
msg["From"] = SMTP_FROM
msg["To"] = SMTP_TO
msg.attach(MIMEText(full_html, "html"))

try:
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        # Handles connection if non-TLS is provided initially before upgrade
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    print("✅ Test Email sent successfully!", flush=True)
except Exception as e:
    print(f"❌ Failed to send email: {e}", flush=True)
    raise
