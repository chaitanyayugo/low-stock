import xmlrpc.client
import requests
import smtplib
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

print("CID attachment image test")

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

# Fetch image
url = f"{ODOO_URL}/web/image/product.product/{pid}/image_128"
session = requests.Session()
session.auth = (ODOO_USER, ODOO_PASSWORD)
resp = session.get(url, timeout=10)
if resp.status_code != 200 or len(resp.content) < 100:
    print("Image not fetched")
    exit(1)

# Create email with attachment
msg = MIMEMultipart("related")  # "related" allows referencing attachments
msg["Subject"] = "CH-625 Image (CID attachment)"
msg["From"] = SMTP_FROM
msg["To"] = SMTP_TO

# HTML that references the image by CID
html = """
<html>
<body>
<h2>CH-625 Dining Chair</h2>
<img src="cid:product_image" width="200">
<p>This image is attached as a CID (works in all email clients).</p>
</body>
</html>
"""
html_part = MIMEText(html, "html")
msg.attach(html_part)

# Attach the image with a Content-ID
image_part = MIMEImage(resp.content, _subtype="png")   # or "jpeg"
image_part.add_header("Content-ID", "<product_image>")
image_part.add_header("Content-Disposition", "inline", filename="product.png")
msg.attach(image_part)

# Send
with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASSWORD)
    server.send_message(msg)

print("Email sent with CID attachment – image should appear.")
