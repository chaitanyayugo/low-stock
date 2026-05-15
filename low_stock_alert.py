import xmlrpc.client
import requests
import base64
import os

print("Diagnostic: Fetching one product image from Odoo", flush=True)

ODOO_URL = os.environ.get("ODOO_URL")
ODOO_DB = os.environ.get("ODOO_DB")
ODOO_USER = os.environ.get("ODOO_USER")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD")

# Connect to Odoo
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise Exception("Authentication failed")
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# Get first low-stock quant
domain = [["location_id.usage", "=", "internal"], ["quantity", "<", 5]]
quants = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
    [domain], {"fields": ["product_id"], "limit": 1})
if not quants:
    print("No low-stock products")
    exit(0)

product_id = quants[0]['product_id'][0]
product_name = quants[0]['product_id'][1]
print(f"Testing product: {product_name} (ID: {product_id})", flush=True)

# Try to fetch image
url = f"{ODOO_URL}/web/image/product.product/{product_id}/image_128"
print(f"URL: {url}", flush=True)

session = requests.Session()
session.auth = (ODOO_USER, ODOO_PASSWORD)
resp = session.get(url, timeout=10)

print(f"Status code: {resp.status_code}", flush=True)
print(f"Content length: {len(resp.content)} bytes", flush=True)
print(f"First 50 bytes: {resp.content[:50]}", flush=True)

if resp.status_code == 200 and len(resp.content) > 100:
    b64 = base64.b64encode(resp.content).decode('utf-8')[:100]
    print(f"Base64 (first 100 chars): {b64}", flush=True)
else:
    print("Image not found or too small", flush=True)
    # Also try template
    variant_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'read',
                                     [product_id], ['product_tmpl_id'])
    if variant_data and variant_data[0].get('product_tmpl_id'):
        tmpl_id = variant_data[0]['product_tmpl_id'][0]
        url_tmpl = f"{ODOO_URL}/web/image/product.template/{tmpl_id}/image_128"
        print(f"Trying template URL: {url_tmpl}", flush=True)
        resp2 = session.get(url_tmpl, timeout=10)
        print(f"Template status: {resp2.status_code}, length: {len(resp2.content)}", flush=True)# ------------------------------------------------------------------
print("Building HTML...", flush=True)
rows = ""
for q in quants_limited:
    product_id = q["product_id"][0]
    product_name = q["product_id"][1]
    location_name = q["location_id"][1] if q["location_id"] else "Unknown"
    quantity = q["quantity"]
    reserved = q["reserved_quantity"]
    img_src = product_image_map.get(product_id, "")
    if img_src:
        img_tag = f'<img src="{img_src}" style="width:40px; height:40px; object-fit:cover; border-radius:6px;">'
    else:
        img_tag = '<span style="display:inline-block; width:40px; height:40px; background:#f0f0f0; border-radius:6px; text-align:center; line-height:40px; font-size:10px;">no img</span>'
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
      <h1 style="color:#fff; margin:0;">Low Stock Alert (Top 10)</h1>
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
