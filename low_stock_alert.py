import xmlrpc.client
import requests
import base64
import smtplib
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

print("Start CH-625 test with MIME detection")

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

# Connect to Odoo
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise Exception("Odoo auth failed")
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# Find product
pids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search',
    [[['name', 'ilike', 'CH-625']]], {'limit': 1})
if not pids:
    print("Product not found")
    exit(1)
pid = pids[0]
print("Product ID:", pid)

# Fetch image
url = f"{ODOO_URL}/web/image/product.product/{pid}/image_128"
session = requests.Session()
session.auth = (ODOO_USER, ODOO_PASSWORD)
resp = session.get(url, timeout=10)

img_src = ""
if resp.status_code == 200 and len(resp.content) > 100:
    # Detect content type from response headers
    content_type = resp.headers.get('Content-Type', 'image/jpeg')
    if content_type.startswith('image/'):
        mime = content_type
    else:
        # fallback: guess from magic bytes
        if resp.content[:4] == b'\x89PNG':
            mime = 'image/png'
        else:
            mime = 'image/jpeg'
    
    b64 = base64.b64encode(resp.content).decode('utf-8')
    img_src = f"data:{mime};base64,{b64}"
    print(f"Image fetched: {len(resp.content)} bytes, MIME: {mime}")
else:
    print("No image or too small")

# Build HTML (simple, no extra spaces)
html = f'<html><body><h2>CH-625 Dining Chair</h2><img src="{img_src}" width="150"/><p>If you see the product image, it works.</p></body></html>'

# Send email
msg = MIMEMultipart("alternative")
msg["Subject"] = "CH-625 Image Test (MIME fixed)"
msg["From"] = SMTP_FROM
msg["To"] = SMTP_TO
msg.attach(MIMEText(html, "html"))

with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASSWORD)
    server.send_message(msg)

print("Done")
