from flask import Flask, render_template_string, jsonify, request, redirect, url_for, Response
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from sqlalchemy import text
from math import ceil
import smtplib, ssl
from email.message import EmailMessage
import urllib.request
import secrets
import stripe
import os
import re

app = Flask(__name__)

# ==========================================
# 1. KONFIGURATION
# ==========================================
stripe.api_key = "sk_test_DEIN_STRIPE_SECRET_KEY"
STRIPE_PUBLIC_KEY = "pk_test_DEIN_STRIPE_PUBLIC_KEY"

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///moveit_final_premium.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
ALLOWED_IMG_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.pdf'}

db = SQLAlchemy(app)

# Preis-Filter für Templates: 45.0 -> "45,00"  /  1500 -> "1.500,00"
@app.template_filter('eur')
def eur(v):
    try:
        return f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return v

# ==========================================
# 2. DATENBANK-MODELLE
# ==========================================
class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    weight_class = db.Column(db.String(50), nullable=False)
    price_per_day = db.Column(db.Float, nullable=False)
    stock = db.Column(db.Integer, default=1)
    image_url = db.Column(db.String(500))
    accessories = db.Column(db.String(300), default="")
    operation_guide = db.Column(db.Text, default="")
    insurance_yearly = db.Column(db.Float, default=120.0)
    repair_costs_accumulated = db.Column(db.Float, default=0.0)
    license_plate = db.Column(db.String(50), default="")
    payload_kg = db.Column(db.String(50), default="")

class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    customer_name = db.Column(db.String(100), nullable=False)
    customer_email = db.Column(db.String(100), nullable=False)
    customer_phone = db.Column(db.String(50), nullable=False)
    customer_address = db.Column(db.String(200), nullable=False)
    driver_age = db.Column(db.Integer, default=25)
    start_date = db.Column(db.String(10), nullable=False)
    end_date = db.Column(db.String(10), nullable=False)
    start_time = db.Column(db.String(5), default="08:00")
    end_time = db.Column(db.String(5), default="08:00")
    quantity = db.Column(db.Integer, default=1)
    status = db.Column(db.String(50), default="Bezahlt & Reserviert")
    total_price = db.Column(db.Float, nullable=False)
    has_damage = db.Column(db.Boolean, default=False)
    manage_token = db.Column(db.String(40), default="")
    lic_front = db.Column(db.String(200), default="")
    lic_back = db.Column(db.String(200), default="")
    stripe_session_id = db.Column(db.String(200))

    product = db.relationship('Product', backref=db.backref('bookings', cascade="all, delete-orphan"))

class Blackout(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    start_date = db.Column(db.String(10), nullable=False)
    end_date = db.Column(db.String(10), nullable=False)
    reason = db.Column(db.String(200), default="Nicht verfügbar")
    source = db.Column(db.String(20), default="manual")
    product = db.relationship('Product', backref=db.backref('blackouts', cascade="all, delete-orphan"))

class CostEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    date = db.Column(db.String(10), default="")
    description = db.Column(db.String(200), default="")
    amount = db.Column(db.Float, default=0.0)
    product = db.relationship('Product', backref=db.backref('costs', cascade="all, delete-orphan"))

class GoogleReview(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    author = db.Column(db.String(100))
    text = db.Column(db.String(500))
    stars = db.Column(db.Integer, default=5)

class Setting(db.Model):
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, default="")

def get_setting(key, default=""):
    s = Setting.query.get(key)
    return s.value if (s and s.value) else default

def set_setting(key, value):
    s = Setting.query.get(key)
    if s:
        s.value = value
    else:
        db.session.add(Setting(key=key, value=value))
    db.session.commit()

# ==========================================
# 3. INITIALISIERUNG + LEICHTE DB-MIGRATION
# ==========================================
GUIDE_AUTO = """1. Ankuppeln: Kupplungsmaul auf den Kugelkopf des Zugfahrzeugs setzen, einrasten lassen und Sicherung prüfen (Anzeige am Kupplungshebel). Abreißseil einhängen, Stützrad ganz hochkurbeln und festklemmen. 13-poligen Stecker verbinden und die komplette Beleuchtung (Blinker, Brems-, Rücklicht) kontrollieren.
2. Auffahrrampen / Auffahrschienen nach hinten herausziehen und sicher einrasten.
3. Fahrzeug langsam und exakt gerade auffahren. Bei tiefen Sportwagen und E-Fahrzeugen auf die Bodenfreiheit achten.
4. Seilwinde nutzen (bei nicht fahrbereiten Fahrzeugen): Stahlseil vorne am Zugpunkt einhängen, mit der Ratsche/Kurbel gleichmäßig auf die Ladefläche ziehen, Sperrklinke muss einrasten.
5. Sichern: Alle vier Räder mit den Radspanngurten ÜBER die Reifen niederzurren – niemals an der Karosserie. Handbremse des geladenen Fahrzeugs anziehen, Gang einlegen.
6. Rampen verstauen, festen Sitz und Beleuchtung erneut prüfen.
7. Zulässige Gesamtmasse und Stützlast einhalten. Nach den ersten Kilometern Gurte nachspannen."""

GUIDE_MOTO = """1. Ankuppeln und Beleuchtung prüfen (siehe allgemeine Schritte).
2. Auffahrschiene einsetzen. Motorrad über die Schiene auf die Ladefläche SCHIEBEN (nicht auffahren) – am besten zu zweit.
3. Vorderrad gerade in die Radwippe (Wheel-Chock) rollen, bis es vorne einrastet und das Rad fixiert ist.
4. Mit Spanngurten vorne links und rechts am unteren Gabelbereich/Lenker verzurren, sodass die Federgabel leicht eintaucht (vorgespannt). Hinten zusätzlich sichern.
5. Seitenständer einklappen, ersten Gang einlegen.
6. Sitz und Gurtspannung vor Fahrtbeginn und nach kurzer Strecke noch einmal kontrollieren – die Gabel federt nach."""

GUIDE_GITTER = """1. Ankuppeln und gesamte Beleuchtung prüfen.
2. Bordwände bei Bedarf öffnen/abklappen. Für leichtes, sperriges Gut (Grünschnitt, Laub) die Gitteraufsätze nutzen.
3. Ladung gleichmäßig verteilen, schwere Lasten möglichst über der Achse platzieren (richtige Stützlast).
4. Ladung mit den mitgelieferten Spanngurten sichern; lose/leichte Teile zusätzlich abdecken.
5. Kippfunktion (falls vorhanden) nur auf festem, ebenem Untergrund: Sicherung lösen, langsam kippen, danach wieder verriegeln.
6. Zulässige Gesamtmasse 1500 kg nicht überschreiten."""

GUIDE_KOFFER = """1. Ankuppeln und Beleuchtung prüfen.
2. Hecktüren bzw. Heckklappe öffnen, bei Bedarf die Rampe nutzen.
3. Schwere Ladung nach unten und über die Achse stellen, Gewicht gleichmäßig verteilen.
4. Alles mit Spanngurten an den Zurrösen sichern, damit beim Bremsen nichts verrutscht.
5. Türen vollständig schließen und verriegeln/abschließen.
6. Beim Fahren auf die Bauhöhe und Seitenwind achten (geschlossener Aufbau). Zulässige Gesamtmasse 2000 kg beachten."""

GUIDE_KASTEN = """1. Ankuppeln und Beleuchtung prüfen. Hinweis: ungebremster Anhänger (750 kg) – meist mit Führerschein Klasse B fahrbar.
2. Bordwände öffnen, Ladung gleichmäßig verteilen, schwere Last über der Achse.
3. Ladung mit dem mitgelieferten Netz abdecken und sichern (kein Spanngurt-Set enthalten). Lose Gegenstände gegen Herausfallen sichern.
4. Stützlast und zulässige Gesamtmasse 750 kg einhalten.
5. Zum Entladen Heckklappe öffnen; danach Bordwände wieder verriegeln."""

def _ensure_columns():
    """Fügt neue Spalten in bestehende SQLite-DB ein, ohne Daten zu verlieren."""
    def cols(table):
        return [r[1] for r in db.session.execute(text(f"PRAGMA table_info({table})"))]
    pc = cols('product')
    if 'license_plate' not in pc:
        db.session.execute(text("ALTER TABLE product ADD COLUMN license_plate VARCHAR(50) DEFAULT ''"))
    if 'payload_kg' not in pc:
        db.session.execute(text("ALTER TABLE product ADD COLUMN payload_kg VARCHAR(50) DEFAULT ''"))
    bc = cols('booking')
    for c, ddl in [('start_time', "VARCHAR(5) DEFAULT '08:00'"), ('end_time', "VARCHAR(5) DEFAULT '08:00'"),
                   ('manage_token', "VARCHAR(40) DEFAULT ''"), ('lic_front', "VARCHAR(200) DEFAULT ''"),
                   ('lic_back', "VARCHAR(200) DEFAULT ''")]:
        if c not in bc:
            db.session.execute(text(f"ALTER TABLE booking ADD COLUMN {c} {ddl}"))
    db.session.commit()

with app.app_context():
    db.create_all()
    _ensure_columns()
    if Product.query.count() == 0:
        your_trailers = [
            {"name": "Fahrzeug Transportanhänger", "weight": "3000 kg", "price": 45.00, "plate": "WOR-J3000", "payload": "2350 kg",
             "img": "https://images.unsplash.com/photo-1566367576585-051277d52997?w=500", "acc": "Spanngurte & Seilwinde", "guide": GUIDE_AUTO},
            {"name": "Fahrzeug Transportanhänger", "weight": "2700 kg", "price": 45.00, "plate": "WOR-J2700", "payload": "2100 kg",
             "img": "https://images.unsplash.com/photo-1601584115197-04ecc0da31d7?w=500", "acc": "Spanngurte & Seilwinde", "guide": GUIDE_AUTO},
            {"name": "Motorradtransportanhänger", "weight": "600 kg", "price": 22.00, "plate": "WOR-J0600", "payload": "450 kg",
             "img": "https://images.unsplash.com/photo-1558981806-ec527fa84c39?w=500", "acc": "Spanngurte", "guide": GUIDE_MOTO},
            {"name": "Gitter Anhänger", "weight": "1500 kg", "price": 30.00, "plate": "WOR-J1500", "payload": "1069 kg",
             "img": "https://images.unsplash.com/photo-1532581291347-9c39cf10a73c?w=500", "acc": "Spanngurte", "guide": GUIDE_GITTER},
            {"name": "Kofferanhänger", "weight": "2000 kg", "price": 49.00, "plate": "WOR-J2000", "payload": "1450 kg",
             "img": "https://images.unsplash.com/photo-1586528116311-ad8dd3c8310d?w=500", "acc": "Spanngurte", "guide": GUIDE_KOFFER},
            {"name": "Offener Kastenanhänger", "weight": "750 kg", "price": 22.00, "plate": "WOR-J1300", "payload": "580 kg",
             "img": "https://images.unsplash.com/photo-1590674899484-d5640e854abe?w=500", "acc": "Ladungsnetz", "guide": GUIDE_KASTEN},
        ]
        for t in your_trailers:
            db.session.add(Product(name=t['name'], weight_class=t['weight'], price_per_day=t['price'],
                                   image_url=t['img'], accessories=t['acc'], operation_guide=t['guide'],
                                   license_plate=t['plate'], payload_kg=t['payload'], repair_costs_accumulated=0.0))
        db.session.add(GoogleReview(author="Max Mustermann", text="Perfekter Fahrzeugtransportanhänger! Zustand war wie neu. Gerne wieder."))
        db.session.add(GoogleReview(author="Josef S.", text="Reibungslose Abwicklung in Wolfratshausen. Top Preise."))
        db.session.add(GoogleReview(author="Anna L.", text="Der Kofferanhänger war sauber und extrem stabil. 5 Sterne!"))
        db.session.commit()

# ==========================================
# 4. LOGIK: VERFÜGBARKEIT, PREIS, E-MAIL
# ==========================================
def _daterange(start_str, end_str):
    start_dt = datetime.strptime(start_str, "%Y-%m-%d")
    end_dt = datetime.strptime(end_str, "%Y-%m-%d")
    curr = start_dt
    while curr <= end_dt:
        yield curr.strftime("%Y-%m-%d")
        curr += timedelta(days=1)

def unavailable_dates(product_id):
    days = set()
    for b in Booking.query.filter(Booking.product_id == product_id, Booking.status.notin_(["Zurückgebracht", "Storniert"])).all():
        for d in _daterange(b.start_date, b.end_date):
            days.add(d)
    for bl in Blackout.query.filter(Blackout.product_id == product_id).all():
        for d in _daterange(bl.start_date, bl.end_date):
            days.add(d)
    return days

def is_available(product_id, start_str, end_str, ignore_booking_id=None):
    blocked = unavailable_dates(product_id)
    if ignore_booking_id:
        b = Booking.query.get(ignore_booking_id)
        if b:
            for d in _daterange(b.start_date, b.end_date):
                blocked.discard(d)
    for d in _daterange(start_str, end_str):
        if d in blocked:
            return False
    return True

def billable_days(sd, st, ed, et):
    """Abrechnungstage: je angefangene 24 Stunden = 1 Tag (mind. 1).
    Fr 18:00 -> Sa 17:00 = 23 h = 1 Tag. Ab der 25. Stunde beginnt Tag 2."""
    st = st or "00:00"
    et = et or "00:00"
    try:
        start = datetime.strptime(f"{sd} {st}", "%Y-%m-%d %H:%M")
        end = datetime.strptime(f"{ed} {et}", "%Y-%m-%d %H:%M")
        secs = (end - start).total_seconds()
        if secs <= 0:
            return 1
        return max(1, ceil(secs / 86400.0))
    except Exception:
        return 1

def send_email(to_addrs, subject, body, attachments=None):
    host = get_setting('smtp_host', 'smtp.gmail.com')
    port = int(get_setting('smtp_port', '587') or 587)
    user = get_setting('smtp_user')
    pw = get_setting('smtp_pass')
    sender = get_setting('smtp_from') or user
    if not (user and pw):
        return False, "E-Mail-Versand nicht konfiguriert (SMTP-Daten fehlen im Dashboard)."
    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]
    to_addrs = [a for a in to_addrs if a]
    if not to_addrs:
        return False, "Keine Empfängeradresse."
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = ", ".join(to_addrs)
    msg.set_content(body)
    for fp, fn in (attachments or []):
        try:
            with open(fp, 'rb') as f:
                data = f.read()
            msg.add_attachment(data, maintype='application', subtype='octet-stream', filename=fn)
        except Exception:
            pass
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls(context=ctx)
            s.login(user, pw)
            s.send_message(msg)
        return True, "ok"
    except Exception as e:
        return False, str(e)

def lexware_create_invoice(booking):
    """Best-effort-Schnittstelle zu Lexware. Legt eine Rechnung an, wenn ein API-Schlüssel
    hinterlegt ist. Die exakte Lexware-Office-API muss ggf. an euren Account angepasst werden."""
    key = get_setting('lexware_api_key')
    if not key:
        return False, "Kein Lexware-Schlüssel hinterlegt."
    base = get_setting('lexware_base_url', 'https://api.lexoffice.io/v1') + '/vouchers'
    payload = {
        "type": "salesinvoice", "voucherDate": booking.start_date,
        "totalPrice": {"currency": "EUR"}, "remark": f"Miete {booking.product.name} ({booking.start_date}–{booking.end_date})"
    }
    try:
        import json as _json
        req = urllib.request.Request(base, data=_json.dumps(payload).encode('utf-8'),
                                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                                              'Accept': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=20) as resp:
            return True, resp.read().decode('utf-8', 'replace')[:300]
    except Exception as e:
        return False, str(e)

def manage_link(booking):
    return request.host_url.rstrip('/') + url_for('manage_booking', token=booking.manage_token)

def send_booking_emails(booking):
    """Bestätigung an Kunden (mit Verschiebe-/Storno-Link) + Info an den Betrieb (mit Führerschein)."""
    company = get_setting('company_email', 'move.itoberlandtrailer@gmail.com')
    p = booking.product
    link = manage_link(booking)
    cust_body = (
        f"Hallo {booking.customer_name},\n\n"
        f"vielen Dank für Ihre Buchung bei MOVE.IT Oberland Trailer!\n\n"
        f"Anhänger: {p.name} ({p.weight_class})\n"
        f"Abholung: {booking.start_date} um {booking.start_time} Uhr\n"
        f"Rückgabe: {booking.end_date} um {booking.end_time} Uhr\n"
        f"Gesamtpreis: {eur(booking.total_price)} €\n\n"
        f"Termin verschieben oder stornieren:\n{link}\n\n"
        f"Standort: Auenstraße 10, 82515 Wolfratshausen\n"
        f"Viele Grüße\nMOVE.IT Oberland Trailer"
    )
    send_email(booking.customer_email, "Ihre Buchungsbestätigung – MOVE.IT Oberland Trailer", cust_body)

    atts = []
    for fn in (booking.lic_front, booking.lic_back):
        if fn:
            fp = os.path.join(app.config['UPLOAD_FOLDER'], fn)
            if os.path.exists(fp):
                atts.append((fp, fn))
    comp_body = (
        f"NEUE BUCHUNG\n\n"
        f"Kunde: {booking.customer_name}\nE-Mail: {booking.customer_email}\nTel: {booking.customer_phone}\n"
        f"Anschrift: {booking.customer_address}\nAlter Fahrer: {booking.driver_age}\n\n"
        f"Anhänger: {p.name} ({p.weight_class}) – {p.license_plate}\n"
        f"Abholung: {booking.start_date} {booking.start_time}\nRückgabe: {booking.end_date} {booking.end_time}\n"
        f"Gesamt: {eur(booking.total_price)} €\n"
        f"Verwaltungslink: {link}\n\n"
        f"Führerschein-Fotos sind angehängt." if atts else
        f"NEUE BUCHUNG\n\nKunde: {booking.customer_name} ({booking.customer_email}, {booking.customer_phone})\n"
        f"Anhänger: {p.name} ({p.weight_class})\n{booking.start_date} {booking.start_time} – {booking.end_date} {booking.end_time}\n"
        f"Gesamt: {eur(booking.total_price)} €\n(Keine Führerschein-Fotos vorhanden.)"
    )
    send_email(company, f"Neue Buchung: {booking.customer_name} – {p.name}", comp_body, atts)

# ==========================================
# 5. ÖFFENTLICHE ROUTEN
# ==========================================
@app.route('/')
def storefront():
    products = Product.query.all()
    reviews = GoogleReview.query.all()
    return render_template_string(HTML_STOREFRONT, products=products, reviews=reviews, stripe_key=STRIPE_PUBLIC_KEY)

@app.route('/legal')
def legal_pages():
    return render_template_string(HTML_LEGAL)

@app.route('/api/unavailable-dates', methods=['POST'])
def api_unavailable():
    pid = int(request.json['product_id'])
    return jsonify({"dates": sorted(unavailable_dates(pid))})

@app.route('/api/check-availability', methods=['POST'])
def api_check():
    data = request.json
    product = Product.query.get(int(data['product_id']))
    avail = is_available(int(data['product_id']), data['start_date'], data['end_date'])
    days = billable_days(data['start_date'], data.get('start_time'), data['end_date'], data.get('end_time'))
    price = product.price_per_day * days
    return jsonify({"available": avail, "price": round(price, 2), "days": days})

@app.route('/api/upload-license', methods=['POST'])
def api_upload_license():
    """Speichert Führerschein-Fotos und mailt sie sofort an den Betrieb."""
    name = request.form.get('customer_name', '')
    tag = secure_filename((name or 'kunde').replace(' ', '_'))[:20] or 'kunde'
    stamp = int(datetime.now().timestamp())
    saved = {}
    atts = []
    for side in ('front', 'back'):
        f = request.files.get(side)
        if f and f.filename:
            ext = os.path.splitext(f.filename)[1].lower()
            if ext in ALLOWED_IMG_EXT:
                fn = secure_filename(f"lic_{tag}_{stamp}_{side}{ext}")
                fp = os.path.join(app.config['UPLOAD_FOLDER'], fn)
                f.save(fp)
                saved[side] = fn
                atts.append((fp, fn))
    if atts:
        company = get_setting('company_email', 'move.itoberlandtrailer@gmail.com')
        body = (f"Führerschein-Upload während einer Buchung.\n\n"
                f"Name: {name}\nE-Mail: {request.form.get('customer_email','')}\n"
                f"Tel: {request.form.get('customer_phone','')}\n\nFotos im Anhang.")
        send_email(company, f"Führerschein-Upload: {name}", body, atts)
    return jsonify({"front": saved.get('front', ''), "back": saved.get('back', '')})

@app.route('/api/create-checkout-session', methods=['POST'])
def api_checkout():
    data = request.json
    product = Product.query.get(int(data['product_id']))
    if not is_available(product.id, data['start_date'], data['end_date']):
        return jsonify({"error": "Dieser Anhänger ist im gewählten Zeitraum leider nicht mehr verfügbar."}), 400
    days = billable_days(data['start_date'], data.get('start_time'), data['end_date'], data.get('end_time'))
    total = product.price_per_day * days
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'eur',
                    'product_data': {'name': f"{product.name} ({product.weight_class}) - Miete {days} Tag(e)"},
                    'unit_amount': int(round(total * 100)),
                },
                'quantity': 1,
            }],
            mode='payment',
            metadata={
                "product_id": data['product_id'], "start_date": data['start_date'], "end_date": data['end_date'],
                "start_time": data.get('start_time', '08:00'), "end_time": data.get('end_time', '08:00'),
                "customer_name": data['customer_name'], "customer_email": data['customer_email'],
                "customer_phone": data['customer_phone'], "customer_address": data['customer_address'],
                "driver_age": data['driver_age'], "total_price": total,
                "lic_front": data.get('lic_front', ''), "lic_back": data.get('lic_back', '')
            },
            success_url=request.host_url + 'payment-success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.host_url + 'payment-cancel',
        )
        return jsonify({"id": session.id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/payment-success')
def payment_success():
    session_id = request.args.get('session_id')
    if session_id:
        session = stripe.checkout.Session.retrieve(session_id)
        meta = session.metadata
        if not Booking.query.filter_by(stripe_session_id=session_id).first():
            new_b = Booking(
                product_id=int(meta['product_id']), customer_name=meta['customer_name'],
                customer_email=meta['customer_email'], customer_phone=meta['customer_phone'],
                customer_address=meta['customer_address'], driver_age=int(meta['driver_age']),
                start_date=meta['start_date'], end_date=meta['end_date'],
                start_time=meta.get('start_time', '08:00'), end_time=meta.get('end_time', '08:00'),
                quantity=1, total_price=float(meta['total_price']),
                manage_token=secrets.token_urlsafe(12),
                lic_front=meta.get('lic_front', ''), lic_back=meta.get('lic_back', ''),
                stripe_session_id=session_id
            )
            db.session.add(new_b)
            db.session.commit()
            try:
                send_booking_emails(new_b)
            except Exception:
                pass
            try:
                lexware_create_invoice(new_b)
            except Exception:
                pass
    return """
    <div style="font-family:sans-serif; text-align:center; max-width:600px; margin:100px auto; padding:40px; border-radius:12px; box-shadow:0 4px 12px rgba(0,0,0,0.05); border:1px solid #e5e5e7;">
        <h1 style="color:#007aff;">✓ Reservierung erfolgreich!</h1>
        <p style="color:#333; font-size:16px;">Vielen Dank für Ihre Buchung bei MOVE.IT Oberland Trailer. Eine Bestätigung wurde an Ihre E-Mail gesendet.</p>
        <a href="/" style="display:inline-block; margin-top:20px; padding:12px 24px; background:#007aff; color:white; text-decoration:none; border-radius:6px; font-weight:bold;">Zurück zur Übersicht</a>
    </div>
    """

@app.route('/api/ai-advisor', methods=['POST'])
def ai_advisor():
    msg = request.json.get('message', '').lower()
    if "auto" in msg or "porsche" in msg or "fahrzeug" in msg or "kfz" in msg:
        reply = "Für Fahrzeugtransporte empfehle ich unseren schweren Fahrzeug Transportanhänger mit 3000 kg zulässiger Gesamtmasse (45€/24h). Haben Sie ein leichteres Auto, reicht die 2700 kg Variante! Spanngurte und Seilwinde sind immer dabei."
    elif "motorrad" in msg or "bike" in msg or "roller" in msg:
        reply = "Für Zweiräder haben wir den perfekt abgestimmten Motorradtransportanhänger (600 kg Gesamtmasse) für unschlagbare 22€ pro 24 Stunden – inklusive Spanngurte."
    elif "garten" in msg or "grünschnitt" in msg or "holz" in msg:
        reply = "Perfekt für sperrige Gartenabfälle oder Holz ist unser Gitter Anhänger (1500 kg Gesamtmasse) für 30€ pro 24 Stunden – Spanngurte inklusive."
    elif "möbel" in msg or "umzug" in msg or "trocken" in msg:
        reply = "Für Umzüge und wetterempfindliche Fracht eignet sich hervorragend unser geschlossener Kofferanhänger (2000 kg Gesamtmasse) für 49€/24h – Spanngurte sind dabei."
    else:
        reply = "Für allgemeine, leichtere Transporte ist unser offener Kastenanhänger (750 kg Gesamtmasse) für nur 22€/24h die wirtschaftlichste Wahl – inklusive Ladungsnetz!"
    return jsonify({"reply": reply})

# ---------- Selbstverwaltung Buchung (Kunde) ----------
@app.route('/booking/manage/<token>')
def manage_booking(token):
    b = Booking.query.filter_by(manage_token=token).first()
    if not b:
        return "Buchung nicht gefunden.", 404
    return render_template_string(HTML_MANAGE, b=b)

@app.route('/booking/cancel/<token>', methods=['POST'])
def cancel_booking(token):
    b = Booking.query.filter_by(manage_token=token).first()
    if b:
        b.status = "Storniert"
        db.session.commit()
        company = get_setting('company_email', 'move.itoberlandtrailer@gmail.com')
        send_email(company, f"STORNO: {b.customer_name} – {b.product.name}",
                   f"Die Buchung von {b.customer_name} ({b.start_date}–{b.end_date}) wurde vom Kunden storniert.")
    return redirect(url_for('manage_booking', token=token))

@app.route('/booking/reschedule/<token>', methods=['POST'])
def reschedule_booking(token):
    b = Booking.query.filter_by(manage_token=token).first()
    if not b:
        return "Buchung nicht gefunden.", 404
    sd = request.form.get('start_date'); ed = request.form.get('end_date')
    st = request.form.get('start_time', b.start_time); et = request.form.get('end_time', b.end_time)
    if sd and ed:
        if ed < sd:
            sd, ed = ed, sd
        if is_available(b.product_id, sd, ed, ignore_booking_id=b.id):
            b.start_date, b.end_date, b.start_time, b.end_time = sd, ed, st, et
            days = billable_days(sd, st, ed, et)
            b.total_price = round(b.product.price_per_day * days, 2)
            db.session.commit()
            company = get_setting('company_email', 'move.itoberlandtrailer@gmail.com')
            send_email([company, b.customer_email], f"Termin verschoben: {b.customer_name}",
                       f"Neuer Zeitraum: {sd} {st} – {ed} {et}. Neuer Preis: {eur(b.total_price)} €.")
            return render_template_string(HTML_MANAGE, b=b, msg="Termin erfolgreich verschoben.")
        return render_template_string(HTML_MANAGE, b=b, msg="Dieser Zeitraum ist leider nicht verfügbar.")
    return redirect(url_for('manage_booking', token=token))

# ==========================================
# 6. ADMIN-DASHBOARD
# ==========================================
@app.route('/admin')
def admin_dashboard():
    bookings = Booking.query.order_by(Booking.id.desc()).all()
    products = Product.query.all()
    reviews = GoogleReview.query.all()
    blackouts = Blackout.query.order_by(Blackout.start_date.desc()).all()
    paid = [b for b in bookings if b.status != "Storniert"]

    total_revenue = sum(b.total_price for b in paid)
    total_extra_costs = sum(c.amount for p in products for c in p.costs)
    total_repairs = sum(p.repair_costs_accumulated for p in products) + total_extra_costs
    total_insurance = sum(p.insurance_yearly for p in products)
    insurance_month = total_insurance / 12
    net_profit = total_revenue - total_repairs - insurance_month

    num_bookings = len(paid)
    active_bookings = sum(1 for b in paid if b.status != "Zurückgebracht")
    returned_bookings = sum(1 for b in paid if b.status == "Zurückgebracht")
    damage_count = sum(1 for b in paid if b.has_damage)
    damage_rate = (damage_count / num_bookings * 100) if num_bookings else 0
    avg_booking_value = (total_revenue / num_bookings) if num_bookings else 0

    def bdays(b):
        return billable_days(b.start_date, b.start_time, b.end_date, b.end_time)
    total_rented_days = sum(bdays(b) for b in paid)
    avg_rental_days = (total_rented_days / num_bookings) if num_bookings else 0
    avg_rating = (sum(r.stars for r in reviews) / len(reviews)) if reviews else 0

    labels, revenue_values, booking_counts, rented_days_per, damage_values = [], [], [], [], []
    profit_table = []
    for p in products:
        pb = [b for b in p.bookings if b.status != "Storniert"]
        rev = round(sum(b.total_price for b in pb), 2)
        p_costs = round(sum(c.amount for c in p.costs) + p.repair_costs_accumulated, 2)
        p_insurance = round(p.insurance_yearly, 2)
        total_cost = round(p_costs + p_insurance, 2)
        profit = round(rev - total_cost, 2)
        months_with = len({b.start_date[:7] for b in pb}) or 0
        avg_month = (rev / months_with) if months_with else 0
        if rev >= total_cost and total_cost > 0:
            payoff = "✅ amortisiert"
        elif avg_month > 0:
            payoff = f"~ {ceil((total_cost - rev) / avg_month)} Monate"
        else:
            payoff = "—"
        labels.append(p.name + " (" + p.weight_class + ")")
        revenue_values.append(rev)
        booking_counts.append(len(pb))
        rented_days_per.append(sum(bdays(b) for b in pb))
        damage_values.append(sum(1 for b in pb if b.has_damage))
        profit_table.append({"name": p.name, "weight": p.weight_class, "rev": rev, "cost": total_cost,
                             "profit": profit, "avg_month": round(avg_month, 2), "payoff": payoff})

    monthly_revenue = [0.0] * 12
    monthly_count = [0] * 12
    monthly_cost = [0.0] * 12
    for b in paid:
        try:
            m = datetime.strptime(b.start_date, "%Y-%m-%d").month
            monthly_revenue[m - 1] += b.total_price
            monthly_count[m - 1] += 1
        except Exception:
            pass
    for p in products:
        for c in p.costs:
            try:
                m = datetime.strptime(c.date, "%Y-%m-%d").month
                monthly_cost[m - 1] += c.amount
            except Exception:
                pass
    monthly_profit = [round(monthly_revenue[i] - monthly_cost[i] - insurance_month, 2) for i in range(12)]
    monthly_revenue = [round(x, 2) for x in monthly_revenue]
    monthly_cost = [round(x, 2) for x in monthly_cost]

    age_damage = [0, 0, 0, 0]
    age_bookings = [0, 0, 0, 0]
    for b in paid:
        a = b.driver_age or 0
        idx = 0 if a <= 22 else 1 if a <= 30 else 2 if a <= 50 else 3
        age_bookings[idx] += 1
        if b.has_damage:
            age_damage[idx] += 1

    products_json = [{"id": p.id, "name": p.name, "weight": p.weight_class, "plate": p.license_plate or "",
                      "payload": p.payload_kg or "", "acc": p.accessories or "", "price": p.price_per_day} for p in products]

    return render_template_string(
        HTML_ADMIN, bookings=bookings, products=products, reviews=reviews, blackouts=blackouts,
        today=datetime.now().strftime("%Y-%m-%d"), gcal_url=get_setting('gcal_ics_url'),
        revenue=round(total_revenue, 2), profit=round(net_profit, 2), repairs=round(total_repairs, 2),
        insurance_total=round(total_insurance, 2), insurance_month=round(insurance_month, 2),
        num_bookings=num_bookings, active_bookings=active_bookings, returned_bookings=returned_bookings,
        damage_count=damage_count, damage_rate=round(damage_rate, 1),
        avg_booking_value=round(avg_booking_value, 2), avg_rental_days=round(avg_rental_days, 1),
        total_rented_days=total_rented_days, avg_rating=round(avg_rating, 1), num_reviews=len(reviews),
        chart_labels=labels, chart_revenue=revenue_values, chart_bookings=booking_counts,
        chart_rented_days=rented_days_per, chart_damages=damage_values,
        monthly_revenue=monthly_revenue, monthly_count=monthly_count, monthly_profit=monthly_profit,
        age_damage=age_damage, age_bookings=age_bookings, profit_table=profit_table,
        products_json=products_json,
        smtp_host=get_setting('smtp_host', 'smtp.gmail.com'), smtp_port=get_setting('smtp_port', '587'),
        smtp_user=get_setting('smtp_user'), smtp_from=get_setting('smtp_from'),
        company_email=get_setting('company_email', 'move.itoberlandtrailer@gmail.com'),
        lexware_api_key=get_setting('lexware_api_key'), smtp_pass_set=bool(get_setting('smtp_pass'))
    )

@app.route('/admin/update-product', methods=['POST'])
def update_product():
    product = Product.query.get(request.form.get('id'))
    if product:
        product.price_per_day = float(request.form.get('price'))
        product.insurance_yearly = float(request.form.get('insurance'))
        product.repair_costs_accumulated = float(request.form.get('repairs'))
        product.accessories = request.form.get('accessories', '')
        product.operation_guide = request.form.get('operation_guide', '')
        product.license_plate = request.form.get('license_plate', '')
        product.payload_kg = request.form.get('payload_kg', '')
        file = request.files.get('image_file')
        if file and file.filename:
            ext = os.path.splitext(file.filename)[1].lower()
            if ext in ALLOWED_IMG_EXT:
                fname = secure_filename(f"trailer_{product.id}_{int(datetime.now().timestamp())}{ext}")
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], fname))
                product.image_url = url_for('static', filename='uploads/' + fname)
        else:
            new_url = request.form.get('image_url', '').strip()
            if new_url:
                product.image_url = new_url
        db.session.commit()
    return redirect(url_for('admin_dashboard') + '#sec-fleet')

@app.route('/admin/cost/add', methods=['POST'])
def add_cost():
    pid = request.form.get('product_id')
    date = request.form.get('date') or datetime.now().strftime("%Y-%m-%d")
    desc = request.form.get('description', '')
    try:
        amount = float((request.form.get('amount') or '0').replace(',', '.'))
    except Exception:
        amount = 0.0
    if pid:
        db.session.add(CostEntry(product_id=int(pid), date=date, description=desc, amount=amount))
        db.session.commit()
    return redirect(url_for('admin_dashboard') + '#sec-cost')

@app.route('/admin/cost/delete/<int:cid>', methods=['POST'])
def delete_cost(cid):
    c = CostEntry.query.get(cid)
    if c:
        db.session.delete(c)
        db.session.commit()
    return redirect(url_for('admin_dashboard') + '#sec-cost')

@app.route('/admin/settings/save', methods=['POST'])
def save_settings():
    for k in ['smtp_host', 'smtp_port', 'smtp_user', 'smtp_from', 'company_email', 'lexware_api_key']:
        set_setting(k, request.form.get(k, ''))
    pw = request.form.get('smtp_pass', '')
    if pw:
        set_setting('smtp_pass', pw)
    return redirect(url_for('admin_dashboard') + '#sec-settings')

@app.route('/admin/block/add', methods=['POST'])
def add_block():
    pid = request.form.get('product_id'); start = request.form.get('start_date'); end = request.form.get('end_date')
    reason = request.form.get('reason') or "Nicht verfügbar"
    if pid and start and end:
        if end < start:
            start, end = end, start
        db.session.add(Blackout(product_id=int(pid), start_date=start, end_date=end, reason=reason))
        db.session.commit()
    return redirect(url_for('admin_dashboard') + '#sec-block')

@app.route('/admin/block/delete/<int:block_id>', methods=['POST'])
def delete_block(block_id):
    bl = Blackout.query.get(block_id)
    if bl:
        db.session.delete(bl)
        db.session.commit()
    return redirect(url_for('admin_dashboard') + '#sec-block')

@app.route('/admin/booking/return/<int:booking_id>', methods=['POST'])
def return_booking(booking_id):
    b = Booking.query.get(booking_id)
    if b:
        b.status = "Zurückgebracht"
        if request.form.get('damage') == 'on':
            b.has_damage = True
        db.session.commit()
    return redirect(url_for('admin_dashboard') + '#sec-bookings')

@app.route('/admin/review/add', methods=['POST'])
def add_review():
    author = request.form.get('author'); text_ = request.form.get('text')
    if author and text_:
        db.session.add(GoogleReview(author=author, text=text_))
        db.session.commit()
    return redirect(url_for('admin_dashboard') + '#sec-reviews')

@app.route('/admin/contract')
def admin_contract():
    args = request.args
    try:
        days = int(args.get('days', '1') or 1)
    except Exception:
        days = 1
    try:
        price = float((args.get('price', '0') or '0').replace(',', '.'))
    except Exception:
        price = 0.0
    ramp = 10.0 if args.get('ramp') == 'on' else 0.0
    total = price * days + ramp
    return render_template_string(HTML_CONTRACT, a=args, days=days, price=price, ramp=ramp, total=total,
                                  now=datetime.now().strftime("%d.%m.%Y"))

# ==========================================
# 6b. GOOGLE-KALENDER
# ==========================================
def _ics_escape(t):
    return (t or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

def generate_ics():
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//MOVE.IT Oberland Trailer//DE", "CALSCALE:GREGORIAN", "X-WR-CALNAME:MOVE.IT Anhänger"]
    stamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    def add_event(uid, start, end, summary, desc=""):
        dtstart = start.replace("-", "")
        end_excl = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y%m%d")
        lines.extend(["BEGIN:VEVENT", f"UID:{uid}@moveit", f"DTSTAMP:{stamp}", f"DTSTART;VALUE=DATE:{dtstart}",
                      f"DTEND;VALUE=DATE:{end_excl}", f"SUMMARY:{_ics_escape(summary)}", f"DESCRIPTION:{_ics_escape(desc)}", "END:VEVENT"])
    for b in Booking.query.filter(Booking.status != "Storniert").all():
        add_event(f"booking-{b.id}", b.start_date, b.end_date, f"🚚 {b.product.name} – {b.customer_name}",
                  f"{b.customer_email} · {b.customer_phone} · {b.total_price} €")
    for bl in Blackout.query.all():
        add_event(f"block-{bl.id}", bl.start_date, bl.end_date, f"🔒 GESPERRT: {bl.product.name}", bl.reason)
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)

@app.route('/calendar.ics')
def calendar_feed():
    return Response(generate_ics(), mimetype='text/calendar')

def _ics_to_date(val):
    digits = re.sub(r'[^0-9]', '', val)[:8]
    return datetime.strptime(digits, "%Y%m%d").date()

def _parse_ics_events(txt):
    txt = re.sub(r'\r?\n[ \t]', '', txt)
    events = []
    for block in re.findall(r'BEGIN:VEVENT(.*?)END:VEVENT', txt, re.DOTALL):
        props = {}
        for line in block.splitlines():
            if ':' not in line:
                continue
            key, val = line.split(':', 1)
            props[key.split(';')[0].strip().upper()] = (key, val.strip())
        if 'DTSTART' not in props:
            continue
        start = _ics_to_date(props['DTSTART'][1])
        if 'DTEND' in props:
            ekey, eval_ = props['DTEND']
            end = _ics_to_date(eval_)
            if 'T' not in eval_:
                end = end - timedelta(days=1)
            if end < start:
                end = start
        else:
            end = start
        events.append((start, end, props.get('SUMMARY', ('', ''))[1]))
    return events

def sync_google_calendar():
    url = get_setting('gcal_ics_url')
    if not url:
        return 0, "Keine Google-Kalender-URL hinterlegt."
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'MOVEIT/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            txt = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        return 0, f"Abruf fehlgeschlagen: {e}"
    events = _parse_ics_events(txt)
    products = Product.query.all()
    Blackout.query.filter_by(source='google').delete()
    count = 0
    for start, end, summary in events:
        s_low = summary.lower()
        matched = [p for p in products if p.name.lower() in s_low]
        if len(matched) > 1:
            refined = [p for p in matched if p.weight_class.split()[0] in s_low]
            if refined:
                matched = refined
        if not matched:
            if any(k in s_low for k in ['alle anhänger', 'alle anhaenger', 'betriebsurlaub', 'geschlossen', 'feiertag']):
                matched = products
            else:
                continue
        for p in matched:
            db.session.add(Blackout(product_id=p.id, start_date=start.strftime('%Y-%m-%d'), end_date=end.strftime('%Y-%m-%d'),
                                    reason="Google: " + (summary[:60] or "Termin"), source='google'))
            count += 1
    db.session.commit()
    return count, None

@app.route('/admin/gcal/save', methods=['POST'])
def gcal_save():
    set_setting('gcal_ics_url', request.form.get('gcal_url', '').strip())
    return redirect(url_for('admin_dashboard') + '#sec-gcal')

@app.route('/admin/gcal/sync', methods=['POST'])
def gcal_sync():
    sync_google_calendar()
    return redirect(url_for('admin_dashboard') + '#sec-gcal')

# ==========================================
# 7. STOREFRONT TEMPLATE
# ==========================================
HTML_STOREFRONT = """<!DOCTYPE html>
<html lang="de"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>MOVE.IT Oberland Trailer</title><style>:root{--primary:#007aff;--bg:#f5f7fa;--card:#ffffff;--text:#1d1d1f}*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);margin:0;color:var(--text)}.header{background:var(--card);padding:20px 40px;border-bottom:1px solid #e5e5e7;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}.header h1{margin:0;font-size:22px;font-weight:800;color:#111;letter-spacing:-0.5px}.header p{margin:4px 0 0 0;font-size:14px;color:#86868b}.btn-admin{background:transparent;border:1px solid var(--primary);color:var(--primary);padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:600;font-size:13px}.btn-admin:hover{background:var(--primary);color:white}.topnav{background:var(--card);border-bottom:1px solid #e5e5e7;padding:0 40px;display:flex;gap:8px}.topnav button{background:none;border:none;padding:16px 18px;font-size:15px;font-weight:600;color:#86868b;cursor:pointer;border-bottom:3px solid transparent}.topnav button.active{color:var(--primary);border-bottom-color:var(--primary)}.container{max-width:1200px;margin:40px auto;padding:0 20px}.view{display:none}.view.active{display:block}.timeline{display:flex;justify-content:space-between;margin-bottom:40px;background:var(--card);padding:20px;border-radius:12px;border:1px solid #e5e5e7;flex-wrap:wrap;gap:8px}.step{flex:1;text-align:center;font-size:14px;font-weight:600;color:#86868b;min-width:140px}.step.active{color:var(--primary)}.step.active span{background:var(--primary);color:white}.step span{display:inline-block;width:24px;height:24px;line-height:24px;background:#e5e5e7;border-radius:50%;margin-right:8px;font-size:12px}.panel{display:none}.panel.active{display:block}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:30px}.card{background:var(--card);border-radius:14px;overflow:hidden;border:1px solid #e5e5e7;box-shadow:0 8px 24px rgba(0,0,0,0.02);display:flex;flex-direction:column;justify-content:space-between}.card-img{width:100%;height:200px;object-fit:cover;background:#eaedf1}.card-body{padding:24px;flex-grow:1;display:flex;flex-direction:column;justify-content:space-between}.card-title{font-size:19px;font-weight:700;margin:0 0 6px 0}.weight-tag{display:inline-block;background:#f5f5f7;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:600;color:#424245;margin-bottom:8px}.acc-box{background:#f5f5f7;border-left:3px solid var(--primary);padding:8px 12px;border-radius:4px;font-size:13px;color:#424245;margin:8px 0 14px 0;line-height:1.4}.acc-box strong{color:#000;font-weight:700}.price-tag{font-size:22px;font-weight:700;margin:15px 0 10px 0}.btn{background:var(--primary);color:white;border:none;padding:14px;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;width:100%;text-align:center;box-sizing:border-box}.btn:hover{opacity:0.9}.btn:disabled{background:#ccc !important;cursor:not-allowed}.btn-sec{background:#e5e5e7;color:#1d1d1f;margin-top:8px}.cal-box{background:white;padding:24px;border-radius:12px;border:1px solid #e5e5e7;max-width:440px}.cal-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}.cal-head strong{font-size:16px}.cal-nav{background:#f5f5f7;border:none;width:34px;height:34px;border-radius:8px;font-size:18px;cursor:pointer;color:#1d1d1f}.cal-weekdays,.cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;text-align:center}.cal-weekdays div{font-size:11px;color:#86868b;font-weight:600;padding:4px 0}.cal-grid{margin-top:6px}.cal-day{padding:10px 0;background:#f5f5f7;border-radius:6px;font-size:13px;cursor:pointer;transition:all 0.15s}.cal-day:hover{background:#d8e6ff}.cal-day.empty{background:transparent;cursor:default}.cal-day.disabled{background:#f0f0f2;color:#c7c7cc;cursor:not-allowed;text-decoration:line-through}.cal-day.selected{background:var(--primary) !important;color:white !important;font-weight:bold}.cal-day.inrange{background:#cfe1ff}.legend{font-size:12px;color:#86868b;margin-top:12px;display:flex;gap:16px;flex-wrap:wrap}.legend span{display:inline-flex;align-items:center;gap:6px}.dot{width:12px;height:12px;border-radius:3px;display:inline-block}.timesel{display:flex;gap:12px;margin-bottom:14px}.timesel label{flex:1;font-size:12px;font-weight:600;color:#515154}.timesel select{width:100%;padding:10px;border:1px solid #d2d2d7;border-radius:8px;font-size:14px;margin-top:4px}.ticker-wrap{padding:20px 0;margin-top:10px;overflow:hidden}.ticker{display:flex;gap:20px;animation:tk 35s linear infinite;width:max-content}@keyframes tk{0%{transform:translate3d(0,0,0)}100%{transform:translate3d(-50%,0,0)}}.review-box{background:var(--card);border:1px solid #e5e5e7;padding:16px 20px;border-radius:12px;width:280px}.review-stars{color:#ff9500;font-size:14px;margin-bottom:6px}.review-author{font-weight:700;font-size:14px;margin-bottom:4px}.review-text{font-size:13px;color:#424245;line-height:1.4}.section-reiter{font-size:18px;font-weight:700;margin:0 0 25px 0;padding-bottom:8px;border-bottom:2px solid var(--primary);display:inline-block}.chat-widget{position:fixed;bottom:25px;right:25px;z-index:1000}.chat-btn{width:60px;height:60px;background:var(--primary);border-radius:50%;display:flex;align-items:center;justify-content:center;color:white;font-size:24px;cursor:pointer;box-shadow:0 4px 16px rgba(0,122,255,0.3)}.chat-window{width:320px;height:400px;background:white;border-radius:12px;border:1px solid #e5e5e7;box-shadow:0 8px 32px rgba(0,0,0,0.1);display:none;flex-direction:column;overflow:hidden;position:absolute;bottom:75px;right:0}.chat-header{background:var(--primary);color:white;padding:15px;font-weight:bold;font-size:14px}.chat-body{padding:15px;flex:1;overflow-y:auto;font-size:13px;background:#f5f7fa}.chat-input{display:flex;border-top:1px solid #e5e5e7}.chat-input input{border:none;padding:12px;flex:1;outline:none}.form-g{margin-bottom:16px}.form-g label{display:block;font-size:13px;font-weight:600;margin-bottom:6px;color:#515154}input[type="text"],input[type="email"],input[type="tel"],input[type="number"],input[type="file"]{width:100%;padding:12px;border:1px solid #d2d2d7;border-radius:8px;box-sizing:border-box;font-size:14px}.guide-card{background:var(--card);border:1px solid #e5e5e7;border-radius:14px;margin-bottom:20px;overflow:hidden}.guide-top{display:flex;gap:18px;padding:18px 22px;align-items:center;cursor:pointer}.guide-top img{width:90px;height:64px;object-fit:cover;border-radius:8px;background:#eaedf1}.guide-top .gt-title{font-size:17px;font-weight:700}.guide-top .gt-sub{font-size:13px;color:#86868b}.guide-top .chev{margin-left:auto;color:#86868b;font-size:18px}.guide-card.open .chev{transform:rotate(90deg)}.guide-body{display:none;padding:0 22px 22px 22px}.guide-card.open .guide-body{display:block}.guide-body .acc-line{background:#f5f5f7;border-left:3px solid var(--primary);border-radius:4px;padding:10px 12px;font-size:13px;color:#424245;margin-bottom:14px}.guide-body .acc-line strong{color:#000}.guide-body pre{white-space:pre-wrap;font-family:inherit;font-size:14px;line-height:1.7;margin:0}</style></head><body><div class="header"><div><h1>MOVE.IT Oberland Trailer</h1><p>Ihr Anhängerverleih in Wolfratshausen</p></div><a href="/admin" class="btn-admin">Mitarbeiter Login</a></div><div class="topnav"><button id="nav-rent" class="active" onclick="showView('rent')">🚚 Anhänger mieten</button><button id="nav-guide" onclick="showView('guide')">📖 Bedienung der Anhänger</button></div><div class="container"><div class="view active" id="view-rent"><div class="timeline"><div class="step active" id="tl-1"><span>1</span>Anhänger wählen</div><div class="step" id="tl-2"><span>2</span>Mietzeitraum</div><div class="step" id="tl-3"><span>3</span>Führerschein & Daten</div><div class="step" id="tl-4"><span>4</span>Zahlung</div></div><div class="panel active" id="panel-1"><div class="section-reiter">Unsere Anhänger</div><div class="grid">{% for p in products %}<div class="card"><img src="{{ p.image_url }}" class="card-img" alt="Anhänger"><div class="card-body"><div><div class="card-title">{{ p.name }}</div><div class="weight-tag">Zul. Gesamtmasse: {{ p.weight_class }}</div><div style="color:#34c759;font-size:13px;font-weight:600;margin-bottom:5px;">● Sofort verfügbar</div>{% if p.accessories %}<div class="acc-box"><strong>Inklusive Zubehör:</strong><br>{{ p.accessories }}</div>{% endif %}</div><div><div class="price-tag">{{ p.price_per_day|eur }} € <span style="font-size:14px;font-weight:normal;color:#86868b;">/ 24h</span></div><button class="btn" onclick="selectTrailer({{ p.id }}, '{{ p.name }} ({{ p.weight_class }})', '{{ p.accessories }}')">Auswählen</button></div></div></div>{% endfor %}</div></div><div class="panel" id="panel-2"><h2>Mietzeitraum festlegen für: <span id="target-title" style="color:var(--primary);"></span></h2><div style="display:flex;gap:40px;margin-top:20px;flex-wrap:wrap;"><div class="cal-box"><div class="cal-head"><button class="cal-nav" onclick="changeMonth(-1)">‹</button><strong id="cal-label"></strong><button class="cal-nav" onclick="changeMonth(1)">›</button></div><div class="cal-weekdays"><div>Mo</div><div>Di</div><div>Mi</div><div>Do</div><div>Fr</div><div>Sa</div><div>So</div></div><div class="cal-grid" id="cal-grid"></div><div class="legend"><span><span class="dot" style="background:var(--primary)"></span>gewählt</span><span><span class="dot" style="background:#f0f0f2"></span>belegt / gesperrt</span></div></div><div style="background:white;padding:24px;border-radius:12px;border:1px solid #e5e5e7;flex:1;min-width:260px;"><div id="acc-reminder" class="acc-box" style="display:none;"></div><div class="timesel"><label>Abholung Uhrzeit<select id="start-time" onchange="runLiveCheck()"></select></label><label>Rückgabe Uhrzeit<select id="end-time" onchange="runLiveCheck()"></select></label></div><div style="font-size:12px;color:#86868b;margin-bottom:12px;">Abgerechnet wird je angefangene 24 Stunden. Bsp.: Fr 18:00 → Sa 17:00 = nur 1 Tag.</div><div id="live-calc-info" style="padding:15px;background:#f5f5f7;border-radius:8px;font-weight:600;margin-bottom:15px;">Bitte Start- und Enddatum im Kalender wählen.</div><button class="btn" id="to-step3" onclick="goToStep(3)" disabled>Weiter zu den Daten</button><button class="btn btn-sec" onclick="goToStep(1)">Zurück</button></div></div></div><div class="panel" id="panel-3"><h2>Persönliche Daten & Führerscheinkontrolle</h2><div style="background:white;padding:30px;border-radius:12px;border:1px solid #e5e5e7;max-width:600px;margin:auto;"><div class="form-g"><label>Vollständiger Name:</label><input type="text" id="c-name" placeholder="Max Mustermann"></div><div class="form-g"><label>E-Mail-Adresse:</label><input type="email" id="c-email" placeholder="max@gmail.com"></div><div class="form-g"><label>Telefonnummer:</label><input type="tel" id="c-phone" placeholder="0170 1234567"></div><div class="form-g"><label>Anschrift (Straße, PLZ, Ort):</label><input type="text" id="c-address" placeholder="Auenstraße 1, 82515 Wolfratshausen"></div><div class="form-g"><label>Alter des Fahrers:</label><input type="number" id="c-age" value="28"></div><div style="background:#f5f7fa;padding:15px;border-radius:8px;margin:20px 0;border:1px dashed var(--primary);"><strong style="font-size:13px;display:block;margin-bottom:10px;">🔒 Führerschein-Upload (wird sicher an den Verleih gesendet):</strong><div class="form-g"><label>Vorderseite hochladen:</label><input type="file" id="lic-front" accept="image/*"></div><div class="form-g"><label>Rückseite hochladen:</label><input type="file" id="lic-back" accept="image/*"></div></div><button class="btn" id="btn-validate" onclick="proceedFromData()">Validieren & Weiter</button><button class="btn btn-sec" onclick="goToStep(2)">Zurück</button></div></div><div class="panel" id="panel-4"><h2>Zusammenfassung & Bezahlung</h2><div style="background:white;padding:40px;border-radius:12px;border:1px solid #e5e5e7;max-width:500px;margin:auto;text-align:center;"><div style="font-size:40px;margin-bottom:10px;">💳</div><h3>Sichere Zahlung via Stripe Checkout</h3><p style="color:#666;font-size:14px;">Ihre Dokumente wurden übermittelt. Klicken Sie unten, um die Reservierung verbindlich abzuschließen.</p><div id="checkout-summary" style="margin:20px 0;padding:15px;background:#f5f5f7;border-radius:8px;font-weight:600;"></div><button class="btn" onclick="executeStripeCheckout()" style="background:#34c759;">Jetzt kostenpflichtig buchen</button><button class="btn btn-sec" onclick="goToStep(3)">Zurück</button></div></div><div style="margin-top:50px;"><div class="section-reiter">Unsere zufriedenen Kunden</div></div><div class="ticker-wrap"><div class="ticker">{% for r in reviews %}<div class="review-box"><div class="review-stars">{{ "★" * r.stars }}</div><div class="review-author">{{ r.author }}</div><div class="review-text">"{{ r.text }}"</div></div>{% endfor %}{% for r in reviews %}<div class="review-box"><div class="review-stars">{{ "★" * r.stars }}</div><div class="review-author">{{ r.author }}</div><div class="review-text">"{{ r.text }}"</div></div>{% endfor %}</div></div></div><div class="view" id="view-guide"><h2 style="margin-top:0;">Bedienung der Anhänger</h2><p style="color:#86868b;margin-top:-6px;">So funktioniert jeder Anhänger Schritt für Schritt – tippen Sie zum Aufklappen.</p>{% for p in products %}<div class="guide-card" onclick="this.classList.toggle('open')"><div class="guide-top"><img src="{{ p.image_url }}" alt=""><div><div class="gt-title">{{ p.name }}</div><div class="gt-sub">Zul. Gesamtmasse: {{ p.weight_class }}</div></div><div class="chev">›</div></div><div class="guide-body">{% if p.accessories %}<div class="acc-line"><strong>Mitgeliefertes Zubehör:</strong> {{ p.accessories }}</div>{% endif %}<pre>{{ p.operation_guide }}</pre></div></div>{% endfor %}<div style="background:#fff8e6;border:1px solid #ffe08a;border-radius:10px;padding:16px;font-size:13px;color:#7a5b00;">⚠️ Allgemeiner Hinweis: Vor jeder Fahrt Kupplung, Sicherung, Beleuchtung, Reifendruck und die zulässige Gesamtmasse prüfen. Ladung immer ordnungsgemäß sichern.</div></div></div><div class="chat-widget"><div class="chat-btn" onclick="toggleChat()">💬</div><div class="chat-window" id="chat-win"><div class="chat-header">🤖 MOVE.IT KI-Anhängerberater</div><div class="chat-body" id="chat-b"><div>Hallo! Welches Projekt planen Sie? Schreiben Sie mir, was Sie transportieren möchten.</div></div><div class="chat-input"><input type="text" id="chat-m" placeholder="Nachricht eingeben..." onkeypress="if(event.key==='Enter') sendChat()"></div></div></div><div style="text-align:center;margin:60px 0 30px 0;font-size:13px;color:#86868b;"><a href="/legal" style="color:var(--primary);text-decoration:none;font-weight:600;">Impressum & Datenschutzerklärung</a><br><br>&copy; 2026 MOVE.IT Oberland Trailer. Alle Rechte vorbehalten.</div><script src="https://js.stripe.com/v3/"></script><script>
let selId=null,selStart=null,selEnd=null,licFront='',licBack='';let blockedDates=new Set();let viewYear,viewMonth;
const MONTHS=["Januar","Februar","März","April","Mai","Juni","Juli","August","September","Oktober","November","Dezember"];
(function(){const ss=document.getElementById('start-time'),es=document.getElementById('end-time');for(let h=0;h<24;h++){const t=String(h).padStart(2,'0')+':00';ss.add(new Option(t,t));es.add(new Option(t,t));}ss.value='08:00';es.value='08:00';})();
function showView(v){document.querySelectorAll('.view').forEach(x=>x.classList.remove('active'));document.getElementById('view-'+v).classList.add('active');document.getElementById('nav-rent').classList.toggle('active',v==='rent');document.getElementById('nav-guide').classList.toggle('active',v==='guide');window.scrollTo(0,0)}
function goToStep(s){document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));document.querySelectorAll('.step').forEach(st=>st.classList.remove('active'));document.getElementById('panel-'+s).classList.add('active');for(let i=1;i<=s;i++)document.getElementById('tl-'+i).classList.add('active');if(s===4){document.getElementById('checkout-summary').innerText=document.getElementById('c-name').value+" | "+selStart+" "+document.getElementById('start-time').value+" → "+selEnd+" "+document.getElementById('end-time').value}window.scrollTo(0,0)}
function selectTrailer(id,name,acc){selId=id;selStart=null;selEnd=null;licFront='';licBack='';document.getElementById('target-title').innerText=name;const rem=document.getElementById('acc-reminder');if(acc){rem.style.display='block';rem.innerHTML='<strong>Inklusive Zubehör:</strong><br>'+acc}else{rem.style.display='none'}resetCalcInfo();const now=new Date();viewYear=now.getFullYear();viewMonth=now.getMonth();fetch('/api/unavailable-dates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:selId})}).then(r=>r.json()).then(d=>{blockedDates=new Set(d.dates);renderCalendar()});goToStep(2)}
function resetCalcInfo(){const i=document.getElementById('live-calc-info');i.style.background="#f5f5f7";i.style.color="#1d1d1f";i.innerText="Bitte Start- und Enddatum im Kalender wählen.";document.getElementById('to-step3').disabled=true}
function changeMonth(d){viewMonth+=d;if(viewMonth<0){viewMonth=11;viewYear--}if(viewMonth>11){viewMonth=0;viewYear++}renderCalendar()}
function fmt(y,m,d){return y+"-"+String(m+1).padStart(2,'0')+"-"+String(d).padStart(2,'0')}
function renderCalendar(){document.getElementById('cal-label').innerText=MONTHS[viewMonth]+" "+viewYear;const g=document.getElementById('cal-grid');g.innerHTML="";const fd=new Date(viewYear,viewMonth,1);let off=(fd.getDay()+6)%7;const dim=new Date(viewYear,viewMonth+1,0).getDate();const td=new Date();td.setHours(0,0,0,0);for(let i=0;i<off;i++){const e=document.createElement('div');e.className='cal-day empty';g.appendChild(e)}for(let day=1;day<=dim;day++){const ds=fmt(viewYear,viewMonth,day);const c=document.createElement('div');c.className='cal-day';c.innerText=day;const o=new Date(viewYear,viewMonth,day);if(o<td||blockedDates.has(ds)){c.classList.add('disabled')}else{c.onclick=()=>handleCalClick(ds)}if(ds===selStart||ds===selEnd)c.classList.add('selected');else if(selStart&&selEnd&&ds>selStart&&ds<selEnd)c.classList.add('inrange');g.appendChild(c)}}
function rangeHasBlocked(a,b){let c=new Date(a+"T00:00:00");const e=new Date(b+"T00:00:00");while(c<=e){const ds=fmt(c.getFullYear(),c.getMonth(),c.getDate());if(blockedDates.has(ds))return true;c.setDate(c.getDate()+1)}return false}
function handleCalClick(ds){if(!selStart||(selStart&&selEnd)){selStart=ds;selEnd=null}else{if(ds<selStart){selEnd=selStart;selStart=ds}else{selEnd=ds}if(rangeHasBlocked(selStart,selEnd)){const i=document.getElementById('live-calc-info');i.style.background="#fbeae5";i.style.color="#bf0711";i.innerText="✕ In diesem Zeitraum liegen belegte/gesperrte Tage.";document.getElementById('to-step3').disabled=true;selStart=ds;selEnd=null;renderCalendar();return}}renderCalendar();if(selStart&&selEnd)runLiveCheck();else resetCalcInfo()}
function runLiveCheck(){if(!selStart||!selEnd)return;fetch('/api/check-availability',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:selId,start_date:selStart,end_date:selEnd,start_time:document.getElementById('start-time').value,end_time:document.getElementById('end-time').value})}).then(r=>r.json()).then(d=>{const i=document.getElementById('live-calc-info'),b=document.getElementById('to-step3');if(d.available){i.style.background="#e3f1df";i.style.color="#108043";i.innerHTML="✓ "+d.days+" Miettag(e). Gesamtbetrag: <b>"+d.price.toFixed(2).replace('.',',')+" €</b>";b.disabled=false}else{i.style.background="#fbeae5";i.style.color="#bf0711";i.innerText="✕ In diesem Zeitraum nicht verfügbar.";b.disabled=true}})}
function proceedFromData(){const fd=new FormData();const f=document.getElementById('lic-front').files[0];const b=document.getElementById('lic-back').files[0];if(f)fd.append('front',f);if(b)fd.append('back',b);fd.append('customer_name',document.getElementById('c-name').value);fd.append('customer_email',document.getElementById('c-email').value);fd.append('customer_phone',document.getElementById('c-phone').value);const btn=document.getElementById('btn-validate');btn.innerText='Wird übertragen...';btn.disabled=true;fetch('/api/upload-license',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{licFront=d.front||'';licBack=d.back||'';btn.innerText='Validieren & Weiter';btn.disabled=false;goToStep(4)}).catch(()=>{btn.innerText='Validieren & Weiter';btn.disabled=false;goToStep(4)})}
function executeStripeCheckout(){fetch('/api/create-checkout-session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:selId,start_date:selStart,end_date:selEnd,start_time:document.getElementById('start-time').value,end_time:document.getElementById('end-time').value,customer_name:document.getElementById('c-name').value,customer_email:document.getElementById('c-email').value,customer_phone:document.getElementById('c-phone').value,customer_address:document.getElementById('c-address').value,driver_age:document.getElementById('c-age').value,lic_front:licFront,lic_back:licBack})}).then(r=>r.json()).then(s=>{if(s.error){alert(s.error);return}const stripe=Stripe('{{ stripe_key }}');stripe.redirectToCheckout({sessionId:s.id})})}
function toggleChat(){let w=document.getElementById('chat-win');w.style.display=(w.style.display==='flex')?'none':'flex'}
function sendChat(){let inp=document.getElementById('chat-m'),b=document.getElementById('chat-b');if(!inp.value.trim())return;b.innerHTML+="<div style='text-align:right;margin:5px 0;'><b>Du:</b> "+inp.value+"</div>";fetch('/api/ai-advisor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:inp.value})}).then(r=>r.json()).then(d=>{b.innerHTML+="<div style='margin:5px 0;color:var(--primary);'><b>KI:</b> "+d.reply+"</div>";b.scrollTop=b.scrollHeight});inp.value=''}
</script></body></html>"""

# ==========================================
# 8. KUNDEN-VERWALTUNG TEMPLATE
# ==========================================
HTML_MANAGE = """<!DOCTYPE html><html lang="de"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Buchung verwalten</title><style>body{font-family:-apple-system,"Segoe UI",sans-serif;background:#f5f7fa;margin:0;color:#1d1d1f}.wrap{max-width:560px;margin:50px auto;padding:0 18px}.box{background:white;border:1px solid #e5e5e7;border-radius:14px;padding:26px;margin-bottom:20px}h1{font-size:22px}label{display:block;font-size:13px;font-weight:600;margin:10px 0 4px;color:#515154}input{width:100%;padding:11px;border:1px solid #d2d2d7;border-radius:8px;box-sizing:border-box}.btn{background:#007aff;color:white;border:none;padding:13px;border-radius:8px;font-weight:600;cursor:pointer;width:100%;margin-top:14px}.btn-red{background:#ff3b30}.row{display:flex;gap:12px}.row>div{flex:1}.msg{background:#e3f1df;color:#108043;padding:12px;border-radius:8px;margin-bottom:16px;font-weight:600}.pill{display:inline-block;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:700;background:#e3f1df;color:#108043}.pill.st{background:#fbeae5;color:#bf0711}</style></head><body><div class="wrap"><h1>MOVE.IT – Ihre Buchung</h1>{% if msg %}<div class="msg">{{ msg }}</div>{% endif %}<div class="box"><p style="margin:0 0 8px"><b>{{ b.product.name }}</b> ({{ b.product.weight_class }})</p><p style="margin:4px 0;color:#515154">Abholung: {{ b.start_date }} um {{ b.start_time }} Uhr<br>Rückgabe: {{ b.end_date }} um {{ b.end_time }} Uhr<br>Preis: {{ b.total_price|eur }} €</p><p>Status: {% if b.status=='Storniert' %}<span class="pill st">Storniert</span>{% else %}<span class="pill">{{ b.status }}</span>{% endif %}</p></div>{% if b.status!='Storniert' %}<div class="box"><h3 style="margin-top:0">Termin verschieben</h3><form method="POST" action="/booking/reschedule/{{ b.manage_token }}"><div class="row"><div><label>Neues Startdatum</label><input type="date" name="start_date" value="{{ b.start_date }}" required></div><div><label>Uhrzeit</label><input type="time" name="start_time" value="{{ b.start_time }}"></div></div><div class="row"><div><label>Neues Enddatum</label><input type="date" name="end_date" value="{{ b.end_date }}" required></div><div><label>Uhrzeit</label><input type="time" name="end_time" value="{{ b.end_time }}"></div></div><button class="btn" type="submit">Termin verschieben</button></form></div><div class="box"><h3 style="margin-top:0">Stornieren</h3><form method="POST" action="/booking/cancel/{{ b.manage_token }}" onsubmit="return confirm('Buchung wirklich stornieren?');"><button class="btn btn-red" type="submit">Buchung stornieren</button></form></div>{% endif %}<p style="text-align:center"><a href="/" style="color:#007aff;text-decoration:none">← Zur Startseite</a></p></div></body></html>"""

# ==========================================
# 9. MIETVERTRAG / RECHNUNG (DRUCKBAR)
# ==========================================
HTML_CONTRACT = """<!DOCTYPE html><html lang="de"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Mietvertrag MOVE.IT</title><style>body{font-family:-apple-system,"Segoe UI",sans-serif;background:#e9edf2;margin:0;color:#1d1d1f}.toolbar{position:sticky;top:0;background:#fff;border-bottom:1px solid #e5e5e7;padding:12px 18px;display:flex;gap:10px;justify-content:center}.toolbar button,.toolbar a{padding:10px 18px;border-radius:8px;border:none;font-weight:700;cursor:pointer;text-decoration:none;font-size:14px}.b1{background:#007aff;color:#fff}.b2{background:#e5e5e7;color:#1d1d1f}.sheet{background:white;max-width:760px;margin:24px auto;padding:42px 48px;box-shadow:0 8px 30px rgba(0,0,0,.08)}.head{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:3px solid #007aff;padding-bottom:14px;margin-bottom:18px}.head h1{font-size:22px;margin:0;color:#111}.head .small{font-size:12px;color:#666;text-align:right;line-height:1.5}h2{font-size:15px;margin:22px 0 8px;color:#007aff}table{width:100%;border-collapse:collapse}td{padding:6px 4px;font-size:13px;vertical-align:top;border-bottom:1px solid #f0f0f2}td.k{color:#666;width:46%;font-weight:600}.tot{font-size:18px;font-weight:800;color:#007aff}.legal{font-size:11px;color:#444;line-height:1.5;margin-top:22px;border-top:1px solid #e5e5e7;padding-top:14px}.sign{display:flex;gap:40px;margin-top:50px}.sign div{flex:1;border-top:1px solid #333;padding-top:6px;font-size:12px;color:#555}@media print{.toolbar{display:none}body{background:white}.sheet{box-shadow:none;margin:0;max-width:none}}</style></head><body><div class="toolbar"><button class="b1" onclick="window.print()">🖨️ Drucken / Als PDF speichern</button><a class="b2" href="/admin#sec-contract">← Zurück</a></div><div class="sheet"><div class="head"><div><h1>Mietvertrag / Rechnung</h1><div style="font-size:12px;color:#666;margin-top:4px;">MOVE.IT Oberland Trailer · Liewald und Reichlmair GbR</div></div><div class="small">Auenstraße 10<br>82515 Wolfratshausen<br>move.itoberlandtrailer@gmail.com<br>Datum: {{ now }}</div></div>
<h2>Mieter</h2><table><tr><td class="k">Mieter gleich Fahrer</td><td>{{ '☒ Ja' if a.get('same')=='on' else '☐ Nein' }}</td></tr><tr><td class="k">Name</td><td>{{ a.get('name','') }}</td></tr><tr><td class="k">Straße</td><td>{{ a.get('street','') }}</td></tr><tr><td class="k">Wohnort</td><td>{{ a.get('city','') }}</td></tr><tr><td class="k">Geburtsdatum</td><td>{{ a.get('birth','') }}</td></tr><tr><td class="k">Tel. privat</td><td>{{ a.get('phone','') }}</td></tr><tr><td class="k">Führerschein Nr.</td><td>{{ a.get('licnr','') }}</td></tr><tr><td class="k">Führerschein ausgestellt am</td><td>{{ a.get('licdate','') }}</td></tr><tr><td class="k">Führerschein ausgestellt in</td><td>{{ a.get('licplace','') }}</td></tr><tr><td class="k">Personalausweis/Reisepass Nr.</td><td>{{ a.get('idnr','') }}</td></tr></table>
<h2>Anhänger</h2><table><tr><td class="k">Amtl. Kennz. Anhänger</td><td>{{ a.get('plate','') }}</td></tr><tr><td class="k">Anhängertyp</td><td>{{ a.get('type','') }}</td></tr><tr><td class="k">Zul. Gesamtgewicht</td><td>{{ a.get('weight','') }}</td></tr><tr><td class="k">Nutzlast</td><td>{{ a.get('payload','') }}</td></tr><tr><td class="k">Zubehör</td><td>{{ a.get('acc','') }}</td></tr></table>
<h2>Zeitraum & Preis</h2><table><tr><td class="k">Übernahme</td><td>Datum: {{ a.get('pickup_date','') }} &nbsp; Uhrzeit: {{ a.get('pickup_time','') }}</td></tr><tr><td class="k">Vereinbarte Rückgabe</td><td>Datum: {{ a.get('return_date','') }} &nbsp; Uhrzeit: {{ a.get('return_time','') }}</td></tr><tr><td class="k">Rückgabe (tatsächlich)</td><td>Datum: __________ Uhrzeit: ______</td></tr><tr><td class="k">Tagespreis (bis 24h)</td><td>+ {{ price|eur }} €</td></tr><tr><td class="k">Tage</td><td>= {{ days }}</td></tr><tr><td class="k">Rampe</td><td>{{ ('+ ' + (ramp|eur) + ' €') if ramp>0 else '—' }}</td></tr><tr><td class="k tot">Gesamt</td><td class="tot">= {{ total|eur }} €</td></tr></table>
<div class="legal"><p>Als Mieter bestätige ich, dass ich die auf der Rückseite aufgeführten Geschäftsbedingungen gelesen und akzeptiert habe. Das Fahrzeug wurde mir in verkehrssicherem und einwandfreiem Zustand übergeben.</p><p>Der Mieter erkennt an, dass das Fahrzeug mit GPS überwacht ist. Der Vermieter darf die Daten zur Sicherstellung der Rückgabe oder bei Missbrauch nutzen. Der Anhänger ist sauber zurückzugeben; andernfalls behalten wir uns eine Reinigungsgebühr von 10 € vor.</p></div>
<div class="sign"><div>Ort, Datum &amp; Unterschrift Mieter</div><div>Unterschrift Vermieter</div></div></div></body></html>"""

# ==========================================
# 10. ADMIN-DASHBOARD TEMPLATE
# ==========================================
HTML_ADMIN = """<!DOCTYPE html>
<html lang="de"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>MOVE.IT Admin</title><script src="https://cdn.jsdelivr.net/npm/chart.js"></script><style>*{box-sizing:border-box}body{font-family:-apple-system,"Segoe UI",sans-serif;background:#f5f7fa;margin:0;display:flex;color:#1d1d1f}.sidebar{width:240px;background:#fff;border-right:1px solid #e5e5e7;min-height:100vh;padding:24px 16px;position:fixed}.sidebar h2{margin:0 0 2px;font-size:18px}.navlink{display:block;padding:11px 14px;border-radius:8px;font-size:14px;font-weight:600;color:#515154;text-decoration:none;cursor:pointer;margin-bottom:4px}.navlink:hover{background:#f0f4ff}.navlink.active{background:#eef4ff;color:#007aff}.main-content{margin-left:240px;padding:30px;width:calc(100% - 240px)}.section{display:none}.section.active{display:block}h1{margin-top:0}.menutoggle{display:none}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(185px,1fr));gap:16px;margin-bottom:22px}.stat-card{background:#fff;padding:18px;border-radius:14px;border:1px solid #e5e5e7;border-left:5px solid #007aff}.stat-card h3{margin:6px 0 0;font-size:12px;color:#86868b;text-transform:uppercase;letter-spacing:.3px;font-weight:700}.stat-card .val{margin:6px 0 0;font-size:25px;font-weight:800;line-height:1.1}.stat-card .sub{margin:4px 0 0;font-size:12px;color:#86868b}.c-blue{border-left-color:#007aff}.c-green{border-left-color:#34c759}.c-red{border-left-color:#ff3b30}.c-orange{border-left-color:#ff9500}.c-purple{border-left-color:#af52de}.c-teal{border-left-color:#5ac8fa}
.chart-card{background:#fff;padding:20px;border-radius:14px;border:1px solid #e5e5e7;margin-bottom:22px}.chart-card h3{margin:0 0 4px;font-size:15px}.chart-card .hint{margin:0 0 14px;font-size:12px;color:#86868b}.chart-box{position:relative;height:300px}.chart-box.sm{height:260px}.chart-2col{display:grid;grid-template-columns:1fr 1fr;gap:22px}
.seg{display:inline-flex;background:#eef0f3;border-radius:10px;padding:4px;gap:4px;margin-bottom:14px}.seg button{border:none;background:none;padding:8px 16px;border-radius:8px;font-weight:700;font-size:13px;cursor:pointer;color:#515154}.seg button.active{background:#fff;color:#007aff;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.panel-box{background:#fff;padding:22px;border-radius:12px;border:1px solid #e5e5e7;margin-bottom:24px}.panel-box h3{margin-top:0}table{width:100%;border-collapse:collapse;margin-top:12px}th,td{padding:11px;text-align:left;border-bottom:1px solid #f0f0f2;font-size:13px;vertical-align:top}th{background:#f5f5f7;color:#666}input,textarea,select{padding:8px;border:1px solid #ccc;border-radius:6px;font-size:13px;font-family:inherit}textarea{width:100%;min-height:80px}.btn-sm{padding:8px 14px;background:#007aff;color:#fff;border:none;border-radius:6px;font-weight:bold;cursor:pointer;font-size:12px}.btn-danger{background:#ff3b30}.btn-dark{background:#333}.btn-green{background:#34c759}.pill{padding:4px 8px;border-radius:6px;font-weight:bold;font-size:11px}.pill-green{background:#e3f1df;color:#108043}.pill-grey{background:#eee;color:#666}.pill-red{background:#fbeae5;color:#bf0711}.inline-form{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap}.inline-form .form-field{display:flex;flex-direction:column;gap:4px;font-size:12px;font-weight:600;color:#515154}
@media(max-width:1000px){.chart-2col{grid-template-columns:1fr}}
@media(max-width:760px){.sidebar{position:fixed;left:-260px;transition:left .25s;z-index:50;box-shadow:0 0 30px rgba(0,0,0,.2)}.sidebar.open{left:0}.main-content{margin-left:0;width:100%;padding:18px}.menutoggle{display:inline-block;position:fixed;top:12px;right:12px;z-index:60;background:#007aff;color:#fff;border:none;border-radius:8px;padding:10px 14px;font-size:18px}}
</style></head><body>
<button class="menutoggle" onclick="document.querySelector('.sidebar').classList.toggle('open')">☰</button>
<div class="sidebar"><h2>MOVE.IT Hub</h2><p style="color:#86868b;font-size:13px;margin:0 0 14px">Mitarbeiter-Modus</p>
<a class="navlink active" data-sec="overview" onclick="showSec('overview',this)">📊 Übersicht & Auswertung</a>
<a class="navlink" data-sec="profit" onclick="showSec('profit',this)">💰 Umsatz & Rentabilität</a>
<a class="navlink" data-sec="cost" onclick="showSec('cost',this)">🧾 Kosten je Anhänger</a>
<a class="navlink" data-sec="contract" onclick="showSec('contract',this)">📝 Mietvertrag erstellen</a>
<a class="navlink" data-sec="fleet" onclick="showSec('fleet',this)">🚚 Flotte & Zubehör</a>
<a class="navlink" data-sec="block" onclick="showSec('block',this)">🔒 Anhänger sperren</a>
<a class="navlink" data-sec="bookings" onclick="showSec('bookings',this)">📋 Buchungen</a>
<a class="navlink" data-sec="gcal" onclick="showSec('gcal',this)">📅 Google Kalender</a>
<a class="navlink" data-sec="settings" onclick="showSec('settings',this)">⚙️ E-Mail & Lexware</a>
<a class="navlink" data-sec="reviews" onclick="showSec('reviews',this)">⭐ Rezensionen</a>
<hr style="border:none;border-top:1px solid #e5e5e7;margin:14px 0;"><a class="navlink" href="/">← Zur Mietseite</a></div>
<div class="main-content">

<div class="section active" id="sec-overview"><h1>Übersicht & Auswertung</h1><p style="color:#86868b;margin-top:-8px">Alle Kennzahlen live aus euren echten Buchungen.</p>
<div class="stat-grid">
<div class="stat-card c-green"><h3>Gesamtumsatz</h3><div class="val">{{ revenue|eur }} €</div><div class="sub">brutto, alle Buchungen</div></div>
<div class="stat-card c-blue"><h3>Reingewinn (kalk.)</h3><div class="val">{{ profit|eur }} €</div><div class="sub">Umsatz − Kosten − Vers./Monat</div></div>
<div class="stat-card c-red"><h3>Kosten gesamt</h3><div class="val">{{ repairs|eur }} €</div><div class="sub">Reparaturen + eingetragene Kosten</div></div>
<div class="stat-card c-orange"><h3>Versicherung</h3><div class="val">{{ insurance_total|eur }} €</div><div class="sub">/ Jahr · {{ insurance_month|eur }} € / Monat</div></div>
</div>
<div class="stat-grid">
<div class="stat-card c-blue"><h3>Buchungen gesamt</h3><div class="val">{{ num_bookings }}</div><div class="sub">{{ active_bookings }} aktiv · {{ returned_bookings }} zurück</div></div>
<div class="stat-card c-teal"><h3>Ø Umsatz / Buchung</h3><div class="val">{{ avg_booking_value|eur }} €</div><div class="sub">Durchschnitt</div></div>
<div class="stat-card c-purple"><h3>Vermietete Tage</h3><div class="val">{{ total_rented_days }}</div><div class="sub">Ø {{ avg_rental_days }} Tage / Buchung</div></div>
<div class="stat-card c-red"><h3>Schadensquote</h3><div class="val">{{ damage_rate }} %</div><div class="sub">{{ damage_count }} Schadensfälle</div></div>
</div>
<div class="chart-card"><h3>Umsatz & Buchungen pro Monat</h3><p class="hint">Balken = Umsatz (€), Linie = Anzahl Buchungen.</p><div class="chart-box"><canvas id="chartMonthly"></canvas></div></div>
<div class="chart-2col">
<div class="chart-card"><h3>Umsatz pro Anhänger</h3><div class="chart-box sm"><canvas id="chartRevTrailer"></canvas></div></div>
<div class="chart-card"><h3>Umsatzanteil</h3><div class="chart-box sm"><canvas id="chartRevShare"></canvas></div></div>
</div>
<div class="chart-2col">
<div class="chart-card"><h3>Buchungen pro Anhänger</h3><div class="chart-box sm"><canvas id="chartBookings"></canvas></div></div>
<div class="chart-card"><h3>Auslastung (vermietete Tage)</h3><div class="chart-box sm"><canvas id="chartUtil"></canvas></div></div>
</div>
<div class="chart-2col">
<div class="chart-card"><h3>Schäden nach Modell</h3><div class="chart-box sm"><canvas id="chartDam"></canvas></div></div>
<div class="chart-card"><h3>Buchungen & Schäden nach Alter</h3><div class="chart-box sm"><canvas id="chartAge"></canvas></div></div>
</div>
</div>

<div class="section" id="sec-profit"><h1>Umsatz & Rentabilität</h1>
<div class="panel-box"><h3>Umsatz & Gewinn im Zeitverlauf</h3><div class="seg"><button class="active" onclick="setPeriod('month',this)">Monatlich</button><button onclick="setPeriod('quarter',this)">Quartalsweise</button><button onclick="setPeriod('year',this)">Jährlich</button></div><div class="chart-box"><canvas id="chartPeriod"></canvas></div><div id="period-sum" style="margin-top:14px;font-weight:700"></div></div>
<div class="panel-box"><h3>Rentabilität je Anhänger</h3><p style="color:#86868b;font-size:13px">Umsatz, Kosten (Reparaturen + eingetragene Kosten + Versicherung) und ab wann sich der Anhänger rechnet.</p>
<table><tr><th>Anhänger</th><th>Umsatz</th><th>Kosten</th><th>Gewinn</th><th>Ø Umsatz/Monat</th><th>Amortisation</th></tr>
{% for r in profit_table %}<tr><td><b>{{ r.name }}</b> ({{ r.weight }})</td><td>{{ r.rev|eur }} €</td><td style="color:#ff3b30">{{ r.cost|eur }} €</td><td style="color:{{ '#34c759' if r.profit>=0 else '#ff3b30' }};font-weight:700">{{ r.profit|eur }} €</td><td>{{ r.avg_month|eur }} €</td><td>{{ r.payoff }}</td></tr>{% endfor %}</table></div>
</div>

<div class="section" id="sec-cost"><h1>Kosten je Anhänger</h1>
<div class="panel-box"><h3>Neue Kosten eintragen</h3><p style="color:#86868b;font-size:13px">Z. B. neue Reifen, Bremsen, TÜV. Fließt automatisch in die Rentabilitätsrechnung ein.</p>
<form action="/admin/cost/add" method="POST" class="inline-form"><label class="form-field">Anhänger<select name="product_id" required>{% for p in products %}<option value="{{ p.id }}">{{ p.name }} ({{ p.weight_class }})</option>{% endfor %}</select></label><label class="form-field">Datum<input type="date" name="date" value="{{ today }}"></label><label class="form-field">Beschreibung<input type="text" name="description" placeholder="z. B. Reifen + Bremsen + TÜV" style="width:240px"></label><label class="form-field">Betrag (€)<input type="text" name="amount" placeholder="800" style="width:100px"></label><button class="btn-sm" type="submit">Eintragen</button></form></div>
{% for p in products %}{% if p.costs %}<div class="panel-box"><h3>{{ p.name }} ({{ p.weight_class }})</h3><table><tr><th>Datum</th><th>Beschreibung</th><th>Betrag</th><th></th></tr>{% for c in p.costs %}<tr><td>{{ c.date }}</td><td>{{ c.description }}</td><td>{{ c.amount|eur }} €</td><td><form action="/admin/cost/delete/{{ c.id }}" method="POST"><button class="btn-sm btn-danger" type="submit">Löschen</button></form></td></tr>{% endfor %}</table></div>{% endif %}{% endfor %}
</div>

<div class="section" id="sec-contract"><h1>Mietvertrag / Rechnung erstellen</h1><p style="color:#86868b;margin-top:-6px">Anhänger wählen → Daten eintragen → erzeugt eine druckbare Seite (auch am Handy als PDF speichern).</p>
<div class="panel-box"><form action="/admin/contract" method="GET" target="_blank">
<h3>Anhänger</h3><div class="inline-form" style="margin-bottom:14px"><label class="form-field">Anhänger wählen<select id="prodsel" onchange="fillProd()"><option value="">— wählen —</option>{% for p in products %}<option value="{{ p.id }}">{{ p.name }} ({{ p.weight_class }})</option>{% endfor %}</select></label></div>
<div class="inline-form" style="margin-bottom:14px"><label class="form-field">Kennzeichen<input type="text" name="plate" id="f_plate"></label><label class="form-field">Anhängertyp<input type="text" name="type" id="f_type" style="width:200px"></label><label class="form-field">Zul. Gesamtgewicht<input type="text" name="weight" id="f_weight"></label><label class="form-field">Nutzlast<input type="text" name="payload" id="f_payload"></label><label class="form-field">Zubehör<input type="text" name="acc" id="f_acc" style="width:200px"></label></div>
<h3>Mieter / Fahrer</h3><div class="inline-form" style="margin-bottom:14px"><label class="form-field">Name<input type="text" name="name" style="width:220px"></label><label class="form-field">Straße<input type="text" name="street" style="width:220px"></label><label class="form-field">Wohnort<input type="text" name="city" style="width:200px"></label></div>
<div class="inline-form" style="margin-bottom:14px"><label class="form-field">Geburtsdatum<input type="text" name="birth" placeholder="TT.MM.JJJJ"></label><label class="form-field">Tel.<input type="text" name="phone" placeholder="+49 …"></label><label class="form-field" style="font-weight:600"><span>Mieter = Fahrer</span><span><input type="checkbox" name="same" checked> ja</span></label></div>
<div class="inline-form" style="margin-bottom:14px"><label class="form-field">Führerschein Nr.<input type="text" name="licnr"></label><label class="form-field">ausgestellt am<input type="text" name="licdate" placeholder="TT.MM.JJJJ"></label><label class="form-field">ausgestellt in<input type="text" name="licplace"></label><label class="form-field">Ausweis/Pass Nr.<input type="text" name="idnr"></label></div>
<h3>Zeitraum & Preis</h3><div class="inline-form" style="margin-bottom:14px"><label class="form-field">Übernahme Datum<input type="date" name="pickup_date" value="{{ today }}"></label><label class="form-field">Uhrzeit<input type="time" name="pickup_time" value="08:00"></label><label class="form-field">Rückgabe Datum<input type="date" name="return_date" value="{{ today }}"></label><label class="form-field">Uhrzeit<input type="time" name="return_time" value="08:00"></label></div>
<div class="inline-form" style="margin-bottom:14px"><label class="form-field">Tagespreis (€)<input type="text" name="price" id="f_price" value="0"></label><label class="form-field">Tage<input type="number" name="days" id="f_days" value="1" min="1"></label><label class="form-field" style="font-weight:600"><span>Rampe (+10 €)</span><span><input type="checkbox" name="ramp"> ja</span></label></div>
<button class="btn-sm btn-green" type="submit" style="padding:11px 22px">📄 Mietvertrag erzeugen</button></form></div></div>

<div class="section" id="sec-fleet"><h1>Flotte, Preise & Zubehör</h1><p style="color:#86868b">Preise, Kosten, Kennzeichen, Nutzlast, Zubehör, Bild und Bedienungsanleitung bearbeiten.</p>{% for p in products %}<div class="panel-box"><form action="/admin/update-product" method="POST" enctype="multipart/form-data"><input type="hidden" name="id" value="{{ p.id }}"><h3>{{ p.name }} <span style="color:#86868b;font-weight:normal">({{ p.weight_class }})</span></h3><div style="display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap;margin-bottom:16px"><img src="{{ p.image_url }}" alt="" style="width:130px;height:88px;object-fit:cover;border-radius:8px;border:1px solid #e5e5e7;background:#eaedf1"><div style="flex:1;min-width:260px"><label class="form-field" style="display:block;margin-bottom:10px">Neues Bild hochladen (vom Computer)<input type="file" name="image_file" accept="image/*"></label><label class="form-field" style="display:block">… oder Bild-URL einfügen<input type="text" name="image_url" placeholder="https://…" style="width:100%;max-width:520px"></label></div></div><div class="inline-form" style="margin-bottom:14px"><label class="form-field">Tagespreis (€)<input type="number" step="0.01" name="price" value="{{ p.price_per_day }}" style="width:110px"></label><label class="form-field">Versicherung / Jahr (€)<input type="number" step="0.01" name="insurance" value="{{ p.insurance_yearly }}" style="width:130px"></label><label class="form-field">Reparaturen alt (€)<input type="number" step="0.01" name="repairs" value="{{ p.repair_costs_accumulated }}" style="width:130px"></label></div><div class="inline-form" style="margin-bottom:14px"><label class="form-field">Kennzeichen<input type="text" name="license_plate" value="{{ p.license_plate }}" style="width:140px"></label><label class="form-field">Nutzlast<input type="text" name="payload_kg" value="{{ p.payload_kg }}" style="width:120px"></label></div><label class="form-field" style="display:block;margin-bottom:14px">Mitgeliefertes Zubehör<input type="text" name="accessories" value="{{ p.accessories }}" style="width:100%;max-width:520px"></label><label class="form-field" style="display:block;margin-bottom:14px">Bedienungsanleitung<textarea name="operation_guide">{{ p.operation_guide }}</textarea></label><button type="submit" class="btn-sm">Änderungen speichern</button></form></div>{% endfor %}</div>

<div class="section" id="sec-block"><h1>Anhänger sperren</h1><div class="panel-box"><h3>Neuen Sperrzeitraum anlegen</h3><p style="color:#86868b;font-size:13px">Z. B. Werkstatt oder Eigenbedarf. Gesperrte Tage sind im Buchungskalender nicht mehr wählbar.</p><form action="/admin/block/add" method="POST" class="inline-form"><label class="form-field">Anhänger<select name="product_id" required>{% for p in products %}<option value="{{ p.id }}">{{ p.name }} ({{ p.weight_class }})</option>{% endfor %}</select></label><label class="form-field">Von<input type="date" name="start_date" value="{{ today }}" required></label><label class="form-field">Bis<input type="date" name="end_date" value="{{ today }}" required></label><label class="form-field">Grund<input type="text" name="reason" placeholder="z. B. Werkstatt" style="width:200px"></label><button type="submit" class="btn-sm">Sperren</button></form></div><div class="panel-box"><h3>Aktive Sperrungen</h3>{% if blackouts %}<table><tr><th>Anhänger</th><th>Zeitraum</th><th>Grund</th><th></th></tr>{% for bl in blackouts %}<tr><td>{{ bl.product.name }} ({{ bl.product.weight_class }})</td><td>{{ bl.start_date }} → {{ bl.end_date }}</td><td>{{ bl.reason }}</td><td><form action="/admin/block/delete/{{ bl.id }}" method="POST" onsubmit="return confirm('Aufheben?');"><button class="btn-sm btn-danger" type="submit">Aufheben</button></form></td></tr>{% endfor %}</table>{% else %}<p style="color:#86868b">Keine Sperrungen.</p>{% endif %}</div></div>

<div class="section" id="sec-bookings"><h1>Buchungen</h1><div class="panel-box"><table><tr><th>Kunde</th><th>Anhänger</th><th>Zeitraum</th><th>Umsatz</th><th>Status</th><th>Aktion</th></tr>{% for b in bookings %}<tr><td><b>{{ b.customer_name }}</b><br><small>{{ b.customer_email }}<br>{{ b.customer_phone }} · Alter: {{ b.driver_age }}</small></td><td>{{ b.product.name }}<br><small>{{ b.product.weight_class }}</small></td><td>{{ b.start_date }} {{ b.start_time }}<br>→ {{ b.end_date }} {{ b.end_time }}</td><td>{{ b.total_price|eur }} €</td><td>{% if b.status=='Zurückgebracht' %}<span class="pill pill-grey">{{ b.status }}</span>{% elif b.status=='Storniert' %}<span class="pill pill-red">{{ b.status }}</span>{% else %}<span class="pill pill-green">{{ b.status }}</span>{% endif %}{% if b.has_damage %}<br><span class="pill pill-red" style="margin-top:4px;display:inline-block">⚠ Schaden</span>{% endif %}</td><td>{% if b.status not in ['Zurückgebracht','Storniert'] %}<form action="/admin/booking/return/{{ b.id }}" method="POST"><label style="font-size:11px;display:block;margin-bottom:6px"><input type="checkbox" name="damage"> Schaden</label><button class="btn-sm btn-dark" type="submit">Rückgabe buchen</button></form>{% else %}<small style="color:#86868b">—</small>{% endif %}</td></tr>{% endfor %}{% if not bookings %}<tr><td colspan="6" style="color:#86868b">Noch keine Buchungen.</td></tr>{% endif %}</table></div></div>

<div class="section" id="sec-gcal"><h1>Google Kalender</h1><div class="panel-box"><h3>1. Buchungen in Google Kalender anzeigen</h3><p style="color:#86868b;font-size:13px">Diese Adresse in Google Kalender unter „Weitere Kalender → Per URL hinzufügen" abonnieren.</p><div class="inline-form"><input type="text" id="ics-url" readonly value="{{ request.host_url }}calendar.ics" style="width:420px"><button class="btn-sm" onclick="navigator.clipboard.writeText(document.getElementById('ics-url').value);this.innerText='Kopiert ✓';">Adresse kopieren</button><a class="btn-sm btn-dark" href="/calendar.ics" style="text-decoration:none">Feed öffnen</a></div></div><div class="panel-box"><h3>2. Google-Termine als Sperren übernehmen</h3><p style="color:#86868b;font-size:13px">Geheime iCal-Adresse einfügen. Titel mit Anhängernamen sperrt diesen Anhänger; „Betriebsurlaub/geschlossen" sperrt alle.</p><form action="/admin/gcal/save" method="POST" class="inline-form"><label class="form-field" style="flex:1">Geheime iCal-URL<input type="text" name="gcal_url" value="{{ gcal_url }}" placeholder="https://calendar.google.com/calendar/ical/.../basic.ics" style="width:100%;min-width:360px"></label><button type="submit" class="btn-sm">Speichern</button></form><form action="/admin/gcal/sync" method="POST" style="margin-top:14px"><button class="btn-sm btn-dark" type="submit" {% if not gcal_url %}disabled{% endif %}>🔄 Jetzt synchronisieren</button></form></div></div>

<div class="section" id="sec-settings"><h1>E-Mail & Lexware</h1>
<div class="panel-box"><h3>E-Mail-Versand (Gmail)</h3><p style="color:#86868b;font-size:13px">Damit Führerschein-Fotos an euch und Bestätigungen an Kunden gesendet werden. Bei Gmail ein <b>App-Passwort</b> erstellen (Google-Konto → Sicherheit → 2-Faktor → App-Passwörter) und unten eintragen.</p>
<form action="/admin/settings/save" method="POST"><div class="inline-form" style="margin-bottom:12px"><label class="form-field">SMTP-Server<input type="text" name="smtp_host" value="{{ smtp_host }}" style="width:200px"></label><label class="form-field">Port<input type="text" name="smtp_port" value="{{ smtp_port }}" style="width:80px"></label></div><div class="inline-form" style="margin-bottom:12px"><label class="form-field">Gmail-Adresse (Login)<input type="text" name="smtp_user" value="{{ smtp_user }}" style="width:280px"></label><label class="form-field">App-Passwort {% if smtp_pass_set %}<span style="color:#34c759">(gespeichert)</span>{% endif %}<input type="password" name="smtp_pass" placeholder="{{ '•••• vorhanden – leer lassen zum Behalten' if smtp_pass_set else 'App-Passwort' }}" style="width:240px"></label></div><div class="inline-form" style="margin-bottom:12px"><label class="form-field">Absender (From)<input type="text" name="smtp_from" value="{{ smtp_from }}" placeholder="z. B. move.itoberlandtrailer@gmail.com" style="width:280px"></label><label class="form-field">Eure Empfangsadresse<input type="text" name="company_email" value="{{ company_email }}" style="width:280px"></label></div>
<h3 style="margin-top:18px">Lexware</h3><p style="color:#86868b;font-size:13px">API-Schlüssel von Lexware/lexoffice einfügen. Bei jeder Buchung wird automatisch versucht, eine Rechnung anzulegen.</p><label class="form-field" style="display:block;margin-bottom:12px">Lexware API-Schlüssel<input type="text" name="lexware_api_key" value="{{ lexware_api_key }}" style="width:100%;max-width:520px"></label>
<button class="btn-sm btn-green" type="submit" style="padding:11px 22px">Einstellungen speichern</button></form></div></div>

<div class="section" id="sec-reviews"><h1>Rezensionen</h1><div class="panel-box"><h3>Neue Rezension</h3><form action="/admin/review/add" method="POST" class="inline-form"><label class="form-field">Name<input type="text" name="author" required style="width:200px"></label><label class="form-field">Text<input type="text" name="text" required style="width:420px"></label><button class="btn-sm" type="submit">Hinzufügen</button></form><table><tr><th>Autor</th><th>Text</th><th>Sterne</th></tr>{% for r in reviews %}<tr><td>{{ r.author }}</td><td>{{ r.text }}</td><td>{{ "⭐"*r.stars }}</td></tr>{% endfor %}</table></div></div>

</div>
<script>
function showSec(n,el){document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));document.getElementById('sec-'+n).classList.add('active');document.querySelectorAll('.navlink[data-sec]').forEach(x=>x.classList.remove('active'));if(el)el.classList.add('active');document.querySelector('.sidebar').classList.remove('open')}
window.addEventListener('DOMContentLoaded',function(){const h=(location.hash||'').replace('#sec-','');if(h){const l=document.querySelector('.navlink[data-sec="'+h+'"]');if(l)showSec(h,l)}});
const PRODUCTS={{ products_json|tojson }};
function fillProd(){const id=document.getElementById('prodsel').value;const p=PRODUCTS.find(x=>String(x.id)===id);if(!p)return;document.getElementById('f_plate').value=p.plate;document.getElementById('f_type').value=p.name;document.getElementById('f_weight').value=p.weight;document.getElementById('f_payload').value=p.payload;document.getElementById('f_acc').value=p.acc;document.getElementById('f_price').value=p.price;}
const LBL={{ chart_labels|tojson }},REV={{ chart_revenue|tojson }},BOOK={{ chart_bookings|tojson }},DAYS={{ chart_rented_days|tojson }},DAM={{ chart_damages|tojson }},MREV={{ monthly_revenue|tojson }},MCNT={{ monthly_count|tojson }},MPROF={{ monthly_profit|tojson }},AGE_D={{ age_damage|tojson }},AGE_B={{ age_bookings|tojson }};
const MONTHS=['Jan','Feb','Mär','Apr','Mai','Jun','Jul','Aug','Sep','Okt','Nov','Dez'],PAL=['#007aff','#34c759','#ff9500','#af52de','#5ac8fa','#ff3b30','#ffcc00','#5856d6'];
Chart.defaults.font.family='-apple-system,"Segoe UI",sans-serif';Chart.defaults.color='#515154';
function eur(v){return Number(v).toLocaleString('de-DE',{minimumFractionDigits:2,maximumFractionDigits:2})}
new Chart(document.getElementById('chartMonthly'),{data:{labels:MONTHS,datasets:[{type:'bar',label:'Umsatz (€)',data:MREV,backgroundColor:'#007aff',borderRadius:6,yAxisID:'y',order:2},{type:'line',label:'Buchungen',data:MCNT,borderColor:'#34c759',backgroundColor:'#34c759',borderWidth:3,tension:.35,pointRadius:4,yAxisID:'y1',order:1}]},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},scales:{y:{position:'left',title:{display:true,text:'Umsatz (€)'},ticks:{callback:v=>v+' €'}},y1:{position:'right',grid:{drawOnChartArea:false},title:{display:true,text:'Buchungen'},ticks:{precision:0}}}}});
new Chart(document.getElementById('chartRevTrailer'),{type:'bar',data:{labels:LBL,datasets:[{label:'Umsatz (€)',data:REV,backgroundColor:'#007aff',borderRadius:6}]},options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{callback:v=>v+' €'}}}}});
new Chart(document.getElementById('chartRevShare'),{type:'doughnut',data:{labels:LBL,datasets:[{data:REV,backgroundColor:PAL}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:'bottom',labels:{boxWidth:12,font:{size:11}}}}}});
new Chart(document.getElementById('chartBookings'),{type:'bar',data:{labels:LBL,datasets:[{label:'Buchungen',data:BOOK,backgroundColor:'#af52de',borderRadius:6}]},options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{precision:0}}}}});
new Chart(document.getElementById('chartUtil'),{type:'bar',data:{labels:LBL,datasets:[{label:'Tage',data:DAYS,backgroundColor:'#5ac8fa',borderRadius:6}]},options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{precision:0}}}}});
new Chart(document.getElementById('chartDam'),{type:'doughnut',data:{labels:LBL,datasets:[{data:DAM,backgroundColor:PAL}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:'bottom',labels:{boxWidth:12,font:{size:11}}}}}});
new Chart(document.getElementById('chartAge'),{type:'bar',data:{labels:['18-22','23-30','31-50','51+'],datasets:[{label:'Buchungen',data:AGE_B,backgroundColor:'#007aff',borderRadius:6},{label:'Schäden',data:AGE_D,backgroundColor:'#ff3b30',borderRadius:6}]},options:{responsive:true,maintainAspectRatio:false,scales:{y:{ticks:{precision:0}}}}});
const periodChart=new Chart(document.getElementById('chartPeriod'),{data:{labels:MONTHS,datasets:[{type:'bar',label:'Umsatz (€)',data:MREV,backgroundColor:'#007aff',borderRadius:6},{type:'bar',label:'Gewinn (€)',data:MPROF,backgroundColor:'#34c759',borderRadius:6}]},options:{responsive:true,maintainAspectRatio:false,scales:{y:{ticks:{callback:v=>v+' €'}}}}});
function setPeriod(mode,el){document.querySelectorAll('.seg button').forEach(b=>b.classList.remove('active'));el.classList.add('active');let labels,rev,prof;if(mode==='month'){labels=MONTHS;rev=MREV;prof=MPROF}else if(mode==='quarter'){labels=['Q1','Q2','Q3','Q4'];rev=[0,0,0,0];prof=[0,0,0,0];for(let i=0;i<12;i++){let q=Math.floor(i/3);rev[q]+=MREV[i];prof[q]+=MPROF[i]}}else{labels=['Gesamtjahr'];rev=[MREV.reduce((a,b)=>a+b,0)];prof=[MPROF.reduce((a,b)=>a+b,0)]}periodChart.data.labels=labels;periodChart.data.datasets[0].data=rev;periodChart.data.datasets[1].data=prof;periodChart.update();let tr=rev.reduce((a,b)=>a+b,0),tp=prof.reduce((a,b)=>a+b,0);document.getElementById('period-sum').innerHTML='Summe Umsatz: <span style="color:#007aff">'+eur(tr)+' €</span> &nbsp;·&nbsp; Summe Gewinn: <span style="color:#34c759">'+eur(tp)+' €</span>'}
setPeriod('month',document.querySelector('.seg button'));
</script></body></html>"""

# ==========================================
# 11. RECHTLICHES
# ==========================================
HTML_LEGAL = """<!DOCTYPE html>
<html lang="de"><head><meta charset="UTF-8"><title>Impressum & Datenschutz - MOVE.IT</title><style>body{font-family:-apple-system,"Segoe UI",sans-serif;line-height:1.6;max-width:800px;margin:60px auto;padding:0 20px;color:#1d1d1f}h1,h2{color:#111}.back-link{display:inline-block;margin-bottom:30px;color:#007aff;text-decoration:none;font-weight:600}</style></head><body><a href="/" class="back-link">← Zurück zum Portal</a><h1>Impressum</h1><p><strong>liewald und Reichlmair GbR</strong><br>Auenstraße 10<br>82515 Wolfratshausen<br></p><p><strong>Gesellschafter:</strong><br>Julius Liewald, Josef Reichlmair</p><p><strong>Kontakt:</strong><br>E-Mail: move.itoberlandtrailer@gmail.com<br>Telefon: 01626648546</p><hr style="border:none;border-top:1px solid #e5e5e7;margin:40px 0;"><h1>Datenschutzerklärung</h1><h2>1. Datenerfassung auf unserer Website</h2><p>Die Datenerfassung (Name, Anschrift, E-Mail, Telefon sowie hochgeladene Führerscheindokumente) erfolgt ausschließlich zum Zweck der Erstellung und Abwicklung eines Mietvertrags für Anhänger gemäß Art. 6 Abs. 1 lit. b DSGVO.</p><h2>2. Dokumenten-Sicherheit</h2><p>Die zur Validierung hochgeladenen Führerschein-Bilddateien werden durch interne Sicherheitsmechanismen verarbeitet und nach Ablauf der gesetzlichen Nachweisfristen des Mietverhältnisses vollständig und datenschutzkonform gelöscht.</p></body></html>"""

if __name__ == '__main__':
    app.run(debug=True)