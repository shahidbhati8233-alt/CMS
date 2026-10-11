# CMS — Café Management System

A Flask and SQLite platform where café owners create a profile and manage their own table-specific menu, orders, and QR codes. Each café has a subdomain, for example `juniper.localhost` during local development and `juniper.example.com` after a domain is configured.

There is no standalone `index.html`: Flask routes `/` to the Jinja page in `templates/landing.html`.

## Requirements

- Python 3.10 or newer
- Windows PowerShell (commands below are PowerShell commands)

## Set up on Windows

Open PowerShell in the project directory:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Generate a unique secret key and put the printed value in `.env` as `SECRET_KEY=...`:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

The app initializes all required tables safely on the first request. The legacy Juniper tenant receives active Table 1 only if it does not already have that table. Existing rows are preserved. For the sample menu and initial tables 1–20, run the optional bootstrap command:

```powershell
flask --app server init-db
```

The root page at [http://localhost:5000](http://localhost:5000) is the café signup page. Create a café profile with its display name, unique URL slug, and an owner password of at least 12 characters. No username is needed: the café address identifies the owner workspace. The profile creates that workspace and active Table 1. The owner is sent to `http://<slug>.localhost:5000/owner/login`; `.localhost` subdomains resolve to the local computer in current browsers. The legacy Juniper café and its existing SQLite records are kept as a tenant when the application migrates the database.

Returning owners can sign in from the root page using **Owner sign in**, enter their café address, and continue to that café's subdomain. Enter the owner password there to sign in. Café signup does not ask for or require a username.

Create the first owner account. Use a unique username and a password of at least 12 characters. The secure prompt avoids displaying the password; the values are used only for this command and are not written to `.env`.

```powershell
$env:OWNER_USERNAME = "cafe-owner"
$ownerPassword = Read-Host "Owner password" -AsSecureString
$env:OWNER_PASSWORD = [System.Net.NetworkCredential]::new("", $ownerPassword).Password
flask --app server setup-owner
Remove-Item Env:OWNER_USERNAME, Env:OWNER_PASSWORD
Remove-Variable ownerPassword
```

To reset the account, run the same command again using the same owner username and the new password. The database stores only a scrypt password hash.

Start the local development server:

```powershell
flask --app server run --debug
```

Open the root page at [http://localhost:5000](http://localhost:5000) to create a café profile. Customer menu URLs use its subdomain, for example `http://your-cafe.localhost:5000/menu?table=1`. The optional `init-db` command creates Juniper's sample menu and initial tables 1-20; running it again preserves existing menu and order records.

The owner sign-in is at `http://<your-cafe>.localhost:5000/owner/login`. After signing in, use **Menu manager** to add/edit categories and items, update prices and availability, set an optional HTTPS image URL, or hide/remove items. Hidden items are excluded from the public menu; removing an item preserves prior order-item snapshots.

The authenticated order dashboard is at `/owner/orders`. It highlights new orders, shows the latest 100 orders and their item/price snapshots, table, UTC timestamp, instructions, and total. Owners can update status to New, Accepted, Preparing, Ready, Served, or Cancelled. The page has a manual refresh button and refreshes every 30 seconds while visible unless a form control is active.

The authenticated table-code manager is at `/owner/qr`. Select an existing active table or enter a new number from 1 to 999; new numbers are added as active tables. Generate a preview and download a PNG to print. For local development the current loopback address is used automatically. Before deployment, set `PUBLIC_BASE_URL` to the café's canonical HTTPS origin (for example `https://cafe.example.com`) so printed QR codes point to the public site rather than a local or untrusted host.

The database is stored at `instance/cafe.sqlite3` by default. Set `DATABASE_PATH` in `.env` to store it at another persistent path. The `instance` folder and `.env` are excluded by `.gitignore`. First-request initialization migrates the legacy single-café schema in place, preserving café, menu, owner, table, order, and order-item records. It does not delete or recreate the SQLite database.

## Automated checks

Run the checks from the project directory:

```powershell
python -m unittest discover -s tests -v
```

## Customer ordering manual checks

1. Create a café profile, then open `http://<your-cafe>.localhost:5000/menu?table=1`; confirm the table label. Create Table 2 in **Table QR codes** and confirm its table-specific menu opens too. The optional Juniper demo café has Tables 1-20 after `init-db`.
2. Try `/menu?table=abc`, `/menu?table=0`, and `/menu?table=21`; invalid/inactive tables should not show a menu.
3. Add an item, increase and decrease its quantity, and confirm subtotal updates.
4. Add an instruction and place the order. The success message must say: “Your order has been sent to the café. Please wait for staff confirmation.” It also displays an order number.
5. Refresh the page and confirm an unsubmitted cart is retained for that table.
6. Restart the server and confirm menu and submitted data remain in SQLite.

## Owner menu manual checks

1. Open `http://<your-cafe>.localhost:5000/owner/menu` in a signed-out browser; it should redirect to owner sign-in.
2. Enter the café address from the root-page **Owner sign in** link, then sign in on that café's subdomain with the password chosen during signup (or the password configured for Juniper with `setup-owner`).
3. Add and rename a category, then add an item with a price such as `12.35`; preview the customer menu and confirm it displays as ₹12.35.
4. Mark an item unavailable and confirm it remains visible with an unavailable label and cannot be ordered.
5. Hide the item and confirm it is absent from the customer menu; show it again.
6. Edit its name, description, category, price, and HTTPS image URL; confirm changes appear in the customer preview.
7. Try invalid values such as a negative price, a price with more than two decimal places, or an `http://` image URL; the item should not be saved.
8. Sign out and confirm owner pages require authentication again.

## Order dashboard manual checks

1. Place an order from `http://<your-cafe>.localhost:5000/menu?table=2` with multiple items and an instruction.
2. Sign into that café's `/owner/login` and open **Orders**; confirm the order is highlighted as New with its table, UTC timestamp, item quantities, snapshots, total, and instruction.
3. Change its status to Accepted, then Preparing, Ready, Served, and Cancelled; confirm each update persists after page refresh.
4. Confirm the overview's New orders count changes after the order leaves New.
5. Keep the Orders page open and submit another customer order; confirm it appears after the automatic refresh (up to 30 seconds), or use the Refresh button.
6. Sign out and confirm `/owner/orders` redirects to sign-in.

## Table QR manual checks

1. Sign in at your café subdomain and open **Table QR codes** from the owner navigation.
2. Select Table 2 and generate a QR code; confirm its preview and URL end in `/menu?table=2`.
3. Download the PNG and confirm it opens as a QR image; scan it with a phone or QR reader and confirm it opens the same table menu.
4. Enter a new valid table number such as 21; confirm it is added and its menu URL opens with that table selected.
5. Try invalid values such as 0, 1000, or letters; confirm the backend rejects them.
6. Configure `PUBLIC_BASE_URL` and confirm the preview uses that canonical HTTPS origin.

Order prices, visibility, and availability are read from SQLite when the order is placed; browser-submitted prices are never used, and hidden menu items cannot be ordered by submitting an item ID directly. Money is stored as integer paise, and each order item stores name and price snapshots. Owner changes require authenticated sessions and CSRF tokens. Login and order endpoints have per-IP rate limits; requests and order sizes are bounded and validated.

## Deployment note

Do not deploy with Flask's development server or the sample `.env` value. Configure wildcard DNS (`*.example.com`) and TLS for café subdomains, set `BASE_DOMAIN=example.com`, `PUBLIC_BASE_URL=https://example.com`, a unique persistent `SECRET_KEY`, `SESSION_COOKIE_SECURE=true`, durable private SQLite storage, and a production WSGI server. The QR manager refuses to generate public QR codes from a local domain in production. The default rate-limit store is in-memory for local development; configure `RATELIMIT_STORAGE_URI` to shared storage for production and configure trusted proxy handling if hosted behind a proxy. SQLite is intended for a small single-instance deployment; concurrent multi-instance hosting requires a database service designed for that setup. Use an HTTPS image host for optional item images.

## MVP scope and remaining limits

Self-service café registration, tenant subdomains, the customer menu, ordering, owner sign-in and menu editor, owner order dashboard, and table QR preview/download are implemented. Menu images are specified by HTTPS URL rather than uploaded; the order page shows the latest 100 records without pagination; orders are refreshed by page polling rather than push notifications; and no phone-camera/printed-code test has been performed in this environment. Payment, delivery, inventory, and multi-branch features are intentionally out of scope.
