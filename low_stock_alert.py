import sys
import os
import base64
import hashlib
import smtplib
import xmlrpc.client
from collections import defaultdict
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

print("🟢 Starting ELITE low-stock alert script...", flush=True)

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────
ODOO_URL      = os.environ.get("ODOO_URL", "https://your-odoo-url.com")
ODOO_DB       = os.environ.get("ODOO_DB")
ODOO_USER     = os.environ.get("ODOO_USER")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD")

SMTP_HOST     = os.environ.get("SMTP_HOST")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER     = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM     = os.environ.get("SMTP_FROM")
SMTP_TO       = os.environ.get("SMTP_TO")

LOW_STOCK_THRESHOLD = 5

# Quantity fields to try in priority order
QTY_FIELD_CANDIDATES = [
    "x_avl_custom",
    "x_studio_related_field_6v_1jolles4p",
    "qty_available",
]

# Field that marks a product as imported (boolean True/False in Odoo)
# Adjust this to your actual custom field name
IMPORTED_FIELD = None   # set to e.g. "x_is_imported" to send two separate emails

# Deterministic colour palette — used when a product has NO image in Odoo
AVATAR_COLORS = [
    "#1d4ed8", "#0369a1", "#047857", "#7c3aed",
    "#b45309", "#be123c", "#0e7490", "#15803d",
]


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def avatar_color(name: str) -> str:
    idx = int(hashlib.md5(name.encode()).hexdigest(), 16) % len(AVATAR_COLORS)
    return AVATAR_COLORS[idx]


def letter_avatar_b64_uri(name: str) -> str:
    """
    Returns a tiny inline SVG as a data URI — no attachment needed.
    This replaces the old letter_avatar_html() approach.
    """
    letter = (name or "?")[0].upper()
    color  = avatar_color(name)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="44" height="44">'
        f'<rect width="44" height="44" rx="10" fill="{color}"/>'
        f'<text x="22" y="31" text-anchor="middle" '
        f'font-family="Helvetica,Arial,sans-serif" font-size="20" '
        f'font-weight="700" fill="#fff">{letter}</text>'
        f'</svg>'
    )
    b64 = base64.b64encode(svg.encode()).decode()
    return f"data:image/svg+xml;base64,{b64}"


def img_to_data_uri(img_bytes: bytes) -> str:
    """Convert raw image bytes to an inline base64 data URI (PNG assumed)."""
    b64 = base64.b64encode(img_bytes).decode()
    return f"data:image/png;base64,{b64}"


def stock_badge(qty: float) -> str:
    if qty == 0:
        return (
            '<span style="background:#1a0005;color:#ff4d6d;padding:4px 12px;'
            'border-radius:20px;font-weight:700;font-size:13px;'
            'letter-spacing:0.5px;">OUT</span>'
        )
    elif qty <= 2:
        return (
            f'<span style="background:#1c0a00;color:#ff8c42;padding:4px 12px;'
            f'border-radius:20px;font-weight:700;font-size:13px;">{qty:g}</span>'
        )
    else:
        return (
            f'<span style="background:#0a1a10;color:#52c41a;padding:4px 12px;'
            f'border-radius:20px;font-weight:700;font-size:13px;">{qty:g}</span>'
        )


# ─────────────────────────────────────────────
#  HTML BUILDER
#  ✅ Images are now inline data URIs — ZERO attachments, zero gallery mess.
#  ✅ Sortable/filterable dropdowns on Name, Category, Stock columns.
# ─────────────────────────────────────────────
def build_html(
    products: list,
    qty_field: str,
    product_data_uris: dict,   # p_id → data URI string
    label: str,
    total_products: int,
    total_categories: int,
    critical_count: int,
    low_count: int,
) -> str:
    generated_at = utc_now().strftime("%d %b %Y • %H:%M UTC")

    # Collect unique category names for the dropdown
    cat_names = sorted({
        (p["categ_id"][1] if p.get("categ_id") else "Uncategorised")
        for p in products
    })

    # Build the JS data array — each product as a JS object literal
    js_rows = []
    for p in products:
        p_id     = p["id"]
        qty      = float(p.get(qty_field) or 0)
        cat_name = p["categ_id"][1] if p.get("categ_id") else "Uncategorised"
        name     = (p["name"] or "").replace("\\", "\\\\").replace("`", "\\`").replace("'", "\\'")
        cat_esc  = cat_name.replace("\\", "\\\\").replace("'", "\\'")
        uri      = product_data_uris.get(p_id, "")
        js_rows.append(
            f"{{id:{p_id},name:'{name}',cat:'{cat_esc}',qty:{qty},uri:`{uri}`}}"
        )

    js_data = ",\n    ".join(js_rows)

    # Category <option> tags
    cat_options = "\n".join(
        f'<option value="{c}">{c}</option>' for c in cat_names
    )

    # Stock level options
    stock_options = """
      <option value="all">All</option>
      <option value="out">Out of Stock (0)</option>
      <option value="critical">Critical (1–2)</option>
      <option value="low">Low (3–4)</option>
    """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Low Stock Alert</title>
</head>
<body style="margin:0;padding:0;background:#060b14;">

<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#060b14;padding:32px 12px;">
  <tr><td align="center">
  <table width="680" cellpadding="0" cellspacing="0"
         style="max-width:680px;width:100%;">

    <!-- HEADER -->
    <tr>
      <td style="background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);
                 border-radius:16px 16px 0 0;border:1px solid #1e3a5f;
                 padding:36px 32px 28px;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:11px;font-weight:700;letter-spacing:3px;
                        text-transform:uppercase;color:#38bdf8;margin-bottom:10px;">
              ◈ &nbsp;INVENTORY MANAGEMENT SYSTEM
            </div>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:26px;font-weight:700;color:#f1f5f9;
                        line-height:1.2;margin-bottom:6px;">
              Low Stock Alert &nbsp;
              <span style="font-size:16px;color:#475569;font-weight:400;">
                {label}
              </span>
            </div>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:13px;color:#64748b;">
              {generated_at}
              &nbsp;·&nbsp; Threshold: &lt;&nbsp;{LOW_STOCK_THRESHOLD} units
              &nbsp;·&nbsp; Field:
              <code style="color:#94a3b8;font-size:12px;">{qty_field}</code>
            </div>
          </td>
          <td align="right" valign="top">
            <div style="background:#ff4d6d22;border:1px solid #ff4d6d55;
                        border-radius:50%;width:52px;height:52px;
                        text-align:center;line-height:52px;font-size:24px;">⚠</div>
          </td>
        </tr></table>
      </td>
    </tr>

    <!-- METRICS STRIP -->
    <tr>
      <td style="background:#0d1929;border-left:1px solid #1e3a5f;
                 border-right:1px solid #1e3a5f;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td width="33%" style="padding:20px 0;text-align:center;
                                  border-right:1px solid #1e293b;">
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:30px;font-weight:800;color:#f1f5f9;line-height:1;">
              {total_products}</div>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:11px;color:#475569;margin-top:4px;
                        text-transform:uppercase;letter-spacing:1px;">Total Products</div>
          </td>
          <td width="33%" style="padding:20px 0;text-align:center;
                                  border-right:1px solid #1e293b;">
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:30px;font-weight:800;color:#f1f5f9;line-height:1;">
              {total_categories}</div>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:11px;color:#475569;margin-top:4px;
                        text-transform:uppercase;letter-spacing:1px;">Categories</div>
          </td>
          <td width="34%" style="padding:20px 0;text-align:center;">
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:30px;font-weight:800;color:#ff4d6d;line-height:1;">
              {critical_count}</div>
            <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                        font-size:11px;color:#475569;margin-top:4px;
                        text-transform:uppercase;letter-spacing:1px;">Out of Stock</div>
          </td>
        </tr></table>
      </td>
    </tr>

    <!-- FILTER BAR -->
    <tr>
      <td style="background:#080f1e;border-left:1px solid #1e3a5f;
                 border-right:1px solid #1e3a5f;border-top:1px solid #1e293b;
                 padding:14px 20px;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <!-- Name search -->
          <td style="padding-right:8px;">
            <input id="filterName" type="text" placeholder="🔍 Search name…"
              oninput="renderTable()"
              style="width:100%;box-sizing:border-box;background:#0f172a;
                     border:1px solid #1e3a5f;border-radius:8px;
                     padding:8px 12px;color:#e2e8f0;
                     font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                     font-size:13px;outline:none;">
          </td>
          <!-- Category dropdown -->
          <td style="padding-right:8px;white-space:nowrap;">
            <select id="filterCat" onchange="renderTable()"
              style="background:#0f172a;border:1px solid #1e3a5f;
                     border-radius:8px;padding:8px 12px;color:#94a3b8;
                     font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                     font-size:13px;outline:none;cursor:pointer;">
              <option value="all">All Categories</option>
              {cat_options}
            </select>
          </td>
          <!-- Stock dropdown -->
          <td style="white-space:nowrap;">
            <select id="filterStock" onchange="renderTable()"
              style="background:#0f172a;border:1px solid #1e3a5f;
                     border-radius:8px;padding:8px 12px;color:#94a3b8;
                     font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                     font-size:13px;outline:none;cursor:pointer;">
              {stock_options}
            </select>
          </td>
        </tr></table>
      </td>
    </tr>

    <!-- PRODUCT TABLE (rendered by JS) -->
    <tr>
      <td style="background:#0a1628;border-left:1px solid #1e3a5f;
                 border-right:1px solid #1e3a5f;border-top:1px solid #1e293b;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr style="background:#080f1e;">
            <th width="56" style="padding:12px 16px;text-align:center;
                font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:10px;
                font-weight:700;letter-spacing:1.5px;text-transform:uppercase;
                color:#334155;">IMG</th>
            <th style="padding:12px 8px;text-align:left;cursor:pointer;
                font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:10px;
                font-weight:700;letter-spacing:1.5px;text-transform:uppercase;
                color:#334155;" onclick="toggleSort('name')">
              Product <span id="sortName"></span></th>
            <th width="130" style="padding:12px 8px;text-align:left;cursor:pointer;
                font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:10px;
                font-weight:700;letter-spacing:1.5px;text-transform:uppercase;
                color:#334155;" onclick="toggleSort('cat')">
              Category <span id="sortCat"></span></th>
            <th width="100" style="padding:12px 16px;text-align:center;cursor:pointer;
                font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:10px;
                font-weight:700;letter-spacing:1.5px;text-transform:uppercase;
                color:#334155;" onclick="toggleSort('qty')">
              In Stock <span id="sortQty"></span></th>
          </tr>
          <tbody id="productBody"></tbody>
        </table>
        <div id="noResults" style="display:none;text-align:center;
             padding:32px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
             font-size:14px;color:#334155;">
          No products match the current filters.
        </div>
      </td>
    </tr>

    <!-- FOOTER -->
    <tr>
      <td style="background:#080f1e;border:1px solid #1e3a5f;
                 border-top:1px solid #1e293b;border-radius:0 0 16px 16px;
                 padding:20px 32px;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                     font-size:11px;color:#334155;">
            Auto-generated by Inventory Alert System
          </td>
          <td align="right"
              style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                     font-size:11px;color:#334155;">
            {low_count} low &nbsp;·&nbsp;
            <span style="color:#ff4d6d;">{critical_count} out</span>
          </td>
        </tr></table>
      </td>
    </tr>

  </table>
  </td></tr>
</table>

<!-- ── INTERACTIVE LOGIC ─────────────────────────────────────────── -->
<script>
  const ALL_PRODUCTS = [
    {js_data}
  ];

  let sortKey = null;
  let sortAsc = true;

  function stockLevel(qty) {{
    if (qty === 0)   return 'out';
    if (qty <= 2)    return 'critical';
    return 'low';
  }}

  function badgeHtml(qty) {{
    if (qty === 0)
      return '<span style="background:#1a0005;color:#ff4d6d;padding:4px 12px;border-radius:20px;font-weight:700;font-size:13px;letter-spacing:0.5px;">OUT</span>';
    const col = qty <= 2 ? '#ff8c42' : '#52c41a';
    const bg  = qty <= 2 ? '#1c0a00' : '#0a1a10';
    return `<span style="background:${{bg}};color:${{col}};padding:4px 12px;border-radius:20px;font-weight:700;font-size:13px;">${{qty}}</span>`;
  }}

  function imgHtml(uri, name) {{
    if (uri) {{
      return `<div style="width:44px;height:44px;border-radius:10px;overflow:hidden;background:#1e2433;display:inline-block;"><img src="${{uri}}" width="44" height="44" style="width:44px;height:44px;object-fit:cover;display:block;" alt=""></div>`;
    }}
    // no image — inline SVG avatar
    const colors=['#1d4ed8','#0369a1','#047857','#7c3aed','#b45309','#be123c','#0e7490','#15803d'];
    let h=0; for(const c of name){{h=(h*31+c.charCodeAt(0))>>>0;}} const col=colors[h%colors.length];
    const letter=(name||'?')[0].toUpperCase();
    return `<div style="width:44px;height:44px;border-radius:10px;background:${{col}};display:inline-block;text-align:center;line-height:44px;font-family:Helvetica,Arial,sans-serif;font-size:18px;font-weight:700;color:#fff;">${{letter}}</div>`;
  }}

  function renderTable() {{
    const nameQ  = document.getElementById('filterName').value.toLowerCase();
    const catQ   = document.getElementById('filterCat').value;
    const stockQ = document.getElementById('filterStock').value;

    let rows = ALL_PRODUCTS.filter(p => {{
      if (nameQ  && !p.name.toLowerCase().includes(nameQ)) return false;
      if (catQ  !== 'all' && p.cat !== catQ)               return false;
      if (stockQ !== 'all' && stockLevel(p.qty) !== stockQ) return false;
      return true;
    }});

    if (sortKey) {{
      rows.sort((a, b) => {{
        let av = a[sortKey], bv = b[sortKey];
        if (typeof av === 'string') av = av.toLowerCase();
        if (typeof bv === 'string') bv = bv.toLowerCase();
        return sortAsc ? (av < bv ? -1 : av > bv ? 1 : 0)
                       : (av > bv ? -1 : av < bv ? 1 : 0);
      }});
    }}

    const tbody = document.getElementById('productBody');
    const noRes = document.getElementById('noResults');

    if (rows.length === 0) {{
      tbody.innerHTML = '';
      noRes.style.display = 'block';
      return;
    }}
    noRes.style.display = 'none';

    // Group by category
    const groups = {{}};
    for (const p of rows) {{
      (groups[p.cat] = groups[p.cat] || []).push(p);
    }}

    let html = '';
    for (const [cat, items] of Object.entries(groups)) {{
      html += `
        <tr>
          <td colspan="4" style="padding:24px 20px 8px;">
            <div style="display:inline-block;background:#0f172a;border:1px solid #334155;
                        border-radius:6px;padding:4px 14px;">
              <span style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                           font-size:11px;font-weight:700;letter-spacing:1.5px;
                           text-transform:uppercase;color:#94a3b8;">${{cat}}</span>
              <span style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                           font-size:11px;color:#475569;margin-left:8px;">
                ${{items.length}} item${{items.length !== 1 ? 's' : ''}}
              </span>
            </div>
          </td>
        </tr>`;
      for (const p of items) {{
        html += `
          <tr>
            <td style="padding:12px 16px;width:56px;text-align:center;vertical-align:middle;">
              ${{imgHtml(p.uri, p.name)}}
            </td>
            <td style="padding:12px 8px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                       font-size:14px;color:#e2e8f0;font-weight:500;vertical-align:middle;">
              ${{p.name}}
            </td>
            <td style="padding:12px 8px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                       font-size:12px;color:#64748b;vertical-align:middle;">
              ${{p.cat}}
            </td>
            <td style="padding:12px 16px;text-align:center;vertical-align:middle;">
              ${{badgeHtml(p.qty)}}
            </td>
          </tr>
          <tr>
            <td colspan="4" style="padding:0;height:1px;
                background:linear-gradient(90deg,transparent,#2d3748 20%,#2d3748 80%,transparent);">
            </td>
          </tr>`;
      }}
    }}
    tbody.innerHTML = html;
  }}

  function toggleSort(key) {{
    if (sortKey === key) {{ sortAsc = !sortAsc; }}
    else {{ sortKey = key; sortAsc = true; }}
    ['name','cat','qty'].forEach(k => {{
      document.getElementById('sort'+k.charAt(0).toUpperCase()+k.slice(1)).textContent =
        sortKey === k ? (sortAsc ? ' ▲' : ' ▼') : '';
    }});
    renderTable();
  }}

  // Initial render
  renderTable();
</script>

</body>
</html>"""


# ─────────────────────────────────────────────
#  SEND EMAIL  (plain multipart/related, NO image attachments)
# ─────────────────────────────────────────────
def send_email(html: str, subject: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_FROM
    msg["To"]      = SMTP_TO
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)


# ─────────────────────────────────────────────
#  1. CONNECT TO ODOO
# ─────────────────────────────────────────────
print("🔌 Connecting to Odoo...", flush=True)
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid    = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise Exception("❌ Odoo authentication failed")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
print("✅ Connected to Odoo", flush=True)


# ─────────────────────────────────────────────
#  2. AUTO-DETECT QUANTITY FIELD & FETCH PRODUCTS
# ─────────────────────────────────────────────
print("🔍 Detecting quantity field...", flush=True)

all_products = None
qty_field    = None

# Decide which extra fields to pull
extra_fields = ["id", "name", "categ_id", "image_512"]
if IMPORTED_FIELD:
    extra_fields.append(IMPORTED_FIELD)

for candidate in QTY_FIELD_CANDIDATES:
    try:
        print(f"   ↳ Trying field: {candidate}", flush=True)
        result = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "search_read",
            [[
                ("active",  "=", True),
                (candidate, "<", LOW_STOCK_THRESHOLD),
                (candidate, ">=", 0),
            ]],
            {"fields": extra_fields + [candidate], "limit": 0},
        )
        all_products = result
        qty_field    = candidate
        print(f"✅ Using quantity field: '{qty_field}'", flush=True)
        break
    except Exception as exc:
        print(f"   ⚠️  Field '{candidate}' unavailable: {exc}", flush=True)

if all_products is None or qty_field is None:
    print("❌ None of the quantity fields worked. Aborting.", flush=True)
    sys.exit(1)

# Safety filter
all_products = [
    p for p in all_products
    if p.get(qty_field) not in (None, False)
    and float(p[qty_field]) < LOW_STOCK_THRESHOLD
]

if not all_products:
    print("✅ No low-stock products found. Exiting.", flush=True)
    sys.exit(0)

print(f"📦 Found {len(all_products)} low-stock product(s).", flush=True)


# ─────────────────────────────────────────────
#  3. SORT
# ─────────────────────────────────────────────
all_products.sort(key=lambda p: (
    (p["categ_id"][1] if p.get("categ_id") else "zzz").lower(),
    (p["name"] or "").lower(),
    float(p.get(qty_field) or 0),
))


# ─────────────────────────────────────────────
#  4. DECODE IMAGES → DATA URIs  (no attachments)
# ─────────────────────────────────────────────
print("🖼️  Converting images to data URIs...", flush=True)

# Deduplicate: bytes hash → data URI, then map p_id → data URI
hash_to_uri:      dict[str, str]      = {}
product_data_uris: dict[int, str]     = {}

for p in all_products:
    p_id    = p["id"]
    raw_img = p.get("image_512")
    try:
        if raw_img and str(raw_img) != "False":
            img_str   = raw_img.decode("utf-8") if isinstance(raw_img, bytes) else str(raw_img)
            img_str   = img_str.replace("\n", "").replace("\r", "").strip()
            img_bytes = base64.b64decode(img_str)
            img_hash  = hashlib.md5(img_bytes).hexdigest()
            if img_hash not in hash_to_uri:
                hash_to_uri[img_hash] = img_to_data_uri(img_bytes)
            product_data_uris[p_id] = hash_to_uri[img_hash]
        else:
            product_data_uris[p_id] = ""    # JS will render a letter avatar
    except Exception as exc:
        print(f"   ⚠️  Bad image for product {p_id}: {exc}", flush=True)
        product_data_uris[p_id] = ""

print(
    f"   ↳ {len(all_products)} products → {len(hash_to_uri)} unique image(s).",
    flush=True,
)


# ─────────────────────────────────────────────
#  5. SPLIT INTO IMPORTED / NON-IMPORTED
# ─────────────────────────────────────────────
def split_products(products):
    if not IMPORTED_FIELD:
        return None, products   # feature disabled → send everything as "non-imported"

    imported     = [p for p in products if p.get(IMPORTED_FIELD)]
    non_imported = [p for p in products if not p.get(IMPORTED_FIELD)]
    return imported, non_imported


imported_products, non_imported_products = split_products(all_products)

groups = []
if IMPORTED_FIELD:
    if imported_products:
        groups.append(("Imported",     imported_products))
    if non_imported_products:
        groups.append(("Non-Imported", non_imported_products))
else:
    groups.append(("", non_imported_products))  # single email, no label


# ─────────────────────────────────────────────
#  6. BUILD & SEND ONE EMAIL PER GROUP
# ─────────────────────────────────────────────
print(f"📤 Sending to {SMTP_TO}...", flush=True)

for group_label, products in groups:
    total_products   = len(products)
    total_categories = len({p["categ_id"][0] for p in products if p.get("categ_id")})
    critical_count   = sum(1 for p in products if float(p.get(qty_field) or 0) == 0)
    low_count        = total_products - critical_count

    label_str = f"[{group_label}]" if group_label else ""

    subject = (
        f"⚠️ Low Stock Alert {label_str}"
        f" — {total_products} product{'s' if total_products != 1 else ''}"
        f" · {critical_count} out of stock"
        f" · {utc_now().strftime('%d %b %Y')}"
    )

    html = build_html(
        products          = products,
        qty_field         = qty_field,
        product_data_uris = product_data_uris,
        label             = label_str,
        total_products    = total_products,
        total_categories  = total_categories,
        critical_count    = critical_count,
        low_count         = low_count,
    )

    try:
        send_email(html, subject)
        print(
            f"   ✅ '{group_label or 'All'}' email sent "
            f"({total_products} products, {critical_count} out of stock).",
            flush=True,
        )
    except Exception as exc:
        print(f"   ❌ '{group_label or 'All'}' email failed: {exc}", flush=True)
        sys.exit(1)

print("✅ All emails sent successfully!", flush=True)
