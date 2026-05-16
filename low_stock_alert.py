import xmlrpc.client
import requests
import smtplib
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

print("Minimal image test – using direct Odoo image URL")

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

# Find product CH-625
pids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search',
    [[['name', 'ilike', 'CH-625']]], {'limit': 1})
if not pids:
    print("Product not found")
    exit(1)
pid = pids[0]
print(f"Product ID: {pid}")

# Build image URL (direct, not base64)
image_url = f"{ODOO_URL}/web/image/product.product/{pid}/image_128"

# Simple HTML with direct image URL
html = f"""
<html>
<body>
<h2>CH-625 Dining Chair</h2>
<img src="{image_url}" width="200" />
<p>If you see the image above (maybe after clicking "Load images"), the direct URL works.</p>
</body>
</html>
"""

# Send email
msg = MIMEMultipart("alternative")
msg["Subject"] = "Minimal image test – direct URL"
msg["From"] = SMTP_FROM
msg["To"] = SMTP_TO
msg.attach(MIMEText(html, "html"))

print(f"Connecting to SMTP {SMTP_HOST}:{SMTP_PORT}...")
with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASSWORD)
    server.send_message(msg)

print("Email sent. Check inbox.")
