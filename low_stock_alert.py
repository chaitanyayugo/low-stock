import xmlrpc.client
import requests
import base64
import smtplib
import os
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

print("🟢 EFFICIENT – Only downloads images for top N products")

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

TOP_LIMIT = 10   # Change to 100 later

# Connect
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise Exception("Odoo auth failed")
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# Fetch quants
domain = [["location_id.usage", "=", "internal"], ["quantity", "<", 5]]
quants = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
    [domain],
    {"fields": ["id", "product_id", "location_id", "quantity", "reserved_quantity"]})
if not quants:
    print("No low stock")
    exit(0)

# Take top N
quants_sorted = sorted(quants, key=lambda q: q['quantity'])
quants_limited = quants_sorted[:TOP_LIMIT]
product_ids = list({q["product_id"][0] for q in quants_limited if q["product_id"]})
print(f"Downloading images for {len(product_ids)} products (limit {TOP_LIMIT})")

# Download images only for those
def get_image(pid):
    try:
        resp = requests.get(f"{ODOO_URL}/web/image/product.product/{pid}/image_128",
                            auth=(ODOO_USER, ODOO_PASSWORD), timeout=10)
        if resp.status_code == 200 and len(resp.content) > 100:
            b64 = base64.b64encode(resp.content).decode()
            return f"data:image/png;base64,{b64}"
    except:
        pass
    return "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

img_map = {}
for i, pid in enumerate(product_ids, 1):
    img_map[pid] = get_image(pid)
    print(f"  Downloaded {i}/{len(product_ids)}")

# Build HTML
rows = ""
for q in quants_limited:
    pid = q["product_id"][0]
    pname = q["product_id"][1]
    loc = q["location_id"][1] if q["location_id"] else "Unknown"
    qty = q["quantity"]
    reserved = q["reserved_quantity"]
    img = img_map.get(pid, "")
    rows += f"""
    <tr>
        <td><img src="{img}" width="40"></td>
        <td>{pname}</td>
        <td>{loc}</td>
        <td align="center"><b>{qty}</b></td>
        <td align="center">{reserved}</td>
    </tr>
html = f"""
<html><body>
<h2>Low Stock Alert (Top {TOP_LIMIT} of {len(quants)})</h2>
<table border="1">
<tr><th>Image</th><th>Product</th><th>Location</th><th>Stock</th><th>Reserved</th></tr>
{rows}
</table>
<p>{time.strftime('%Y-%m-%d %H:%M UTC')}</p>
</body></html>
"""

# Send email
msg = MIMEMultipart("alternative")
msg["Subject"] = f"Low Stock Alert – Top {TOP_LIMIT} (Test)"
msg["From"] = SMTP_FROM
msg["To"] = SMTP_TO
msg.attach(MIMEText(html, "html"))

with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASSWORD)
    server.send_message(msg)
print("Email sent")
