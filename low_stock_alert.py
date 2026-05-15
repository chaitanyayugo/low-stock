import xmlrpc.client
import requests
import base64
import smtplib
import os
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

print("TEST: Fetching CH-625 dining chair image", flush=True)

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

# Connect
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise Exception("Auth failed")
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# Find product by name
product_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search',
                                [[['name', 'ilike', 'CH-625 dining chair']]], {'limit': 1})
if not product_ids:
    print("Product not found")
    exit(1)

product_id = product_ids[0]
# Get product details
prod = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'read',
                         [product_id], ['name', 'qty_available'])
product_name = prod[0]['name']
qty = prod[0].get('qty_available', 0)

print(f"Found: {product_name} (ID: {product_id}), stock: {qty}", flush=True)

# Fetch image (variant then template)
def get_image(product_id):
    session = requests.Session()
    session.auth = (ODOO_USER, ODOO_PASSWORD)
    # Variant
    url = f"{ODOO_URL}/web/image/product.product/{product_id}/image_128"
    resp = session.get(url, timeout=10)
    if resp.status_code == 200 and len(resp.content) > 100:
        return f"data:image/png;base64,{base64.b64encode(resp.content).decode()}"
    # Template fallback
    variant_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'read',
                                     [product_id], ['product_tmpl_id'])
    if variant_data and variant_data[0].get('product_tmpl_id'):
        tmpl_id = variant_data[0]['product_tmpl_id'][0]
        url_tmpl = f"{ODOO_URL}/web/image/product.template/{tmpl_id}/image_128"
        resp = session.get(url_tmpl, timeout=10)
        if resp.status_code == 200 and len(resp.content) > 100:
            return f"data:image/png;base64,{base64.b64encode(resp.content).decode()}"
    return ""

img_src = get_image(product_id)
if img_src:
    print("Image fetched successfully", flush=True)
else:
    print("No image found", flush=True)

# Build email
rows = f"""
<tr style="border-bottom:1px solid #eee;">
    <td style="padding:12px 15px; text-align:center;">
        <img src="{img_src}" style="width:80px; height:80px; object-fit:cover; border-radius:6px;">
    </td>
    <td style="padding:12px 15px; font-family:Helvetica; font-size:14px;">
        {product_name}
    </td>
    <td style="padding:12px 15px; text-align:center;">
        {qty}
    </td>
</tr>
"""

full_html = f"""
<div style="background:#f9fafb; padding:40px 10px;">
  <div style="max-width:600px; margin:0 auto; background:#fff; border-radius:8px;">
    <div style="background:#111827; padding:20px;">
      <h1 style="color:#fff;">Test: Single Product Image</h1>
    </div>
    <table style="width:100%; border-collapse:collapse;">
      <thead>
        <tr><th>Image</th><th>Product</th><th>Stock</th></tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    <div style="padding:20px; text-align:center;">
      <p>If you see an image, the script works.</p>
    </div>
  </div>
</div>
"""

print("Sending email...", flush=True)
msg = MIMEMultipart("alternative")
msg["Subject"] = "Test: CH-625 dining chair image"
msg["From"] = SMTP_FROM
msg["To"] = SMTP_TO
msg.attach(MIMEText(full_html, "html"))

with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASSWORD)
    server.send_message(msg)

print("Email sent", flush=True)      <h1 style="color:#fff; margin:0;">Low Stock Alert (Top 10)</h1>
      <p style="color:#9ca3af;">Showing {len(quants_limited)} lowest-stock items out of {len(quants)} total.</p>
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
      <p style="margin:0; font-size:12px; color:#9ca3af;">Generated by GitHub Actions</p>
    </div>
  </div>
</div>
"""

print(f"Sending email to {SMTP_TO}...", flush=True)
msg = MIMEMultipart("alternative")
msg["Subject"] = f"Low Stock Alert (Top 10) - {__import__('datetime').datetime.now().strftime('%Y-%m-%d')}"
msg["From"] = SMTP_FROM
msg["To"] = SMTP_TO
msg.attach(MIMEText(full_html, "html"))

for attempt in range(3):
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        print("Email sent successfully", flush=True)
        break
    except Exception as e:
        print(f"Attempt {attempt+1} failed: {e}", flush=True)
        if attempt < 2:
            time.sleep(10)
        else:
            raise
