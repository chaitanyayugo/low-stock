import xmlrpc.client
import requests
import base64
import smtplib
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

print("Start")

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

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# Find product
pids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search',
    [[['name', 'ilike', 'CH-625']]], {'limit': 1})
if not pids:
    print("Product not found")
    exit(1)
pid = pids[0]

# Fetch image
url = f"{ODOO_URL}/web/image/product.product/{pid}/image_128"
s = requests.Session()
s.auth = (ODOO_USER, ODOO_PASSWORD)
r = s.get(url, timeout=10)
img = ""
if r.status_code == 200 and len(r.content) > 100:
    b64 = base64.b64encode(r.content).decode('utf-8')
    img = "data:image/png;base64," + b64
    print("Image OK")
else:
    print("No image")

# Build HTML (single line, no triple quotes)
html = '<html><body><h2>Test</h2><img src="' + img + '" width="100"/></body></html>'

# Send email
msg = MIMEMultipart("alternative")
msg["Subject"] = "CH-625 Test"
msg["From"] = SMTP_FROM
msg["To"] = SMTP_TO
msg.attach(MIMEText(html, "html"))

with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASSWORD)
    server.send_message(msg)

print("Done")
