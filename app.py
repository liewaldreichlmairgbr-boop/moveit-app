from flask import Flask, render_template_string, jsonify, request, redirect, url_for

from flask_sqlalchemy import SQLAlchemy

from datetime import datetime, timedelta

import stripe



app = Flask(__name__)



# --- CONFIGURATION ---

# Trage hier deine echten Stripe-Schlüssel ein, wenn du live testen willst

stripe.api_key = "sk_test_DEIN_STRIPE_SECRET_KEY"

STRIPE_PUBLIC_KEY = "pk_test_DEIN_STRIPE_PUBLIC_KEY"



app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///rentengine_pro.db'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)



# --- DATABASE MODELS ---

class Product(db.Model):

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(100), nullable=False)

    stock = db.Column(db.Integer, nullable=False)

    price_per_day = db.Column(db.Float, nullable=False)



class Booking(db.Model):

    id = db.Column(db.Integer, primary_key=True)

    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)

    customer_name = db.Column(db.String(100), nullable=False)

    start_date = db.Column(db.String(10), nullable=False)

    end_date = db.Column(db.String(10), nullable=False)

    quantity = db.Column(db.Integer, nullable=False)

    status = db.Column(db.String(30), default="Bezahlt & Reserviert") # Reserviert, Abgeholt, Zurückgebracht

    total_price = db.Column(db.Float, nullable=False)

    stripe_session_id = db.Column(db.String(200), nullable=True)



    product = db.relationship('Product', backref=db.backref('bookings', cascade="all, delete-orphan"))



# Datenbank erstellen und mit initialen Demodaten befüllen

with app.app_context():

    db.create_all()

    if Product.query.count() == 0:

        db.session.add(Product(name="Sony Alpha 7 IV (Body)", stock=5, price_per_day=49.00))

        db.session.add(Product(name="DJI Mavic 3 Pro Cine Premium", stock=3, price_per_day=89.00))

        db.session.add(Product(name="Sigma 24-70mm f/2.8 DG DN Art", stock=6, price_per_day=29.00))

        db.session.commit()



# --- AVAILABILITY MATHEMATICS ---

def get_available_stock(product_id, start_date_str, end_date_str):

    product = Product.query.get(product_id)

    if not product:

        return 0

    wish_start = datetime.strptime(start_date_str, "%Y-%m-%d")

    wish_end = datetime.strptime(end_date_str, "%Y-%m-%d")

    

    active_bookings = Booking.query.filter(

        Booking.product_id == product_id,

        Booking.status != "Zurückgebracht"

    ).all()

    

    max_booked_at_same_time = 0

    current_day = wish_start

    while current_day <= wish_end:

        booked_on_this_day = 0

        for b in active_bookings:

            b_start = datetime.strptime(b.start_date, "%Y-%m-%d")

            b_end = datetime.strptime(b.end_date, "%Y-%m-%d")

            if b_start <= current_day <= b_end:

                booked_on_this_day += b.quantity

        if booked_on_this_day > max_booked_at_same_time:

            max_booked_at_same_time = booked_on_this_day

        current_day += timedelta(days=1)

            

    return product.stock - max_booked_at_same_time



# --- ROUTES ---



@app.route('/')

def storefront():

    products = Product.query.all()

    return render_template_string(HTML_STOREFRONT, products=products, stripe_key=STRIPE_PUBLIC_KEY)



@app.route('/admin')

def admin_dashboard():

    bookings = Booking.query.order_by(Booking.id.desc()).all()

    products = Product.query.all()

    

    # Berechne Live-Statistiken für das Dashboard

    total_revenue = sum(b.total_price for b in bookings)

    active_rentals = Booking.query.filter_by(status="Abgeholt").count()

    pending_reservations = Booking.query.filter_by(status="Bezahlt & Reserviert").count()

    

    return render_template_string(

        HTML_ADMIN, 

        bookings=bookings, 

        products=products, 

        revenue=total_revenue, 

        active=active_rentals, 

        pending=pending_reservations

    )



@app.route('/admin/product/add', methods=['POST'])

def add_product():

    name = request.form.get('name')

    stock = int(request.form.get('stock'))

    price = float(request.form.get('price'))

    

    if name and stock and price:

        db.session.add(Product(name=name, stock=stock, price_per_day=price))

        db.session.commit()

    return redirect(url_for('admin_dashboard'))



@app.route('/admin/product/delete/<int:product_id>')

def delete_product(product_id):

    product = Product.query.get(product_id)

    if product:

        db.session.delete(product)

        db.session.commit()

    return redirect(url_for('admin_dashboard'))



@app.route('/admin/booking/status/<int:booking_id>/<string:new_status>')

def update_booking_status(booking_id, new_status):

    booking = Booking.query.get(booking_id)

    if booking:

        booking.status = new_status

        db.session.commit()

    return redirect(url_for('admin_dashboard'))



# --- API ENDPOINTS ---

@app.route('/api/check-availability', methods=['POST'])

def api_check_availability():

    data = request.json

    p_id = int(data['product_id'])

    start = data['start_date']

    end = data['end_date']

    qty = int(data['quantity'])

    

    product = Product.query.get(p_id)

    if not product:

        return jsonify({"error": "Produkt nicht gefunden"}), 404

        

    available = get_available_stock(p_id, start, end)

    days = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days + 1

    total_price = product.price_per_day * days * qty

    

    return jsonify({

        "available": available >= qty,

        "remaining_stock": available,

        "total_price": round(total_price, 2),

        "days": days

    })



@app.route('/api/create-checkout-session', methods=['POST'])

def api_create_checkout():

    data = request.json

    p_id = int(data['product_id'])

    start = data['start_date']

    end = data['end_date']

    qty = int(data['quantity'])

    customer = data.get('customer_name', 'Gastkunde')

    

    product = Product.query.get(p_id)

    available = get_available_stock(p_id, start, end)

    

    if available < qty:

        return jsonify({"error": "Dieses Produkt ist in dem gewählten Zeitraum leider ausgebucht!"}), 400

        

    days = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days + 1

    total_price = product.price_per_day * days * qty

    

    try:

        session = stripe.checkout.Session.create(

            payment_method_types=['card'],

            line_items=[{

                'price_data': {

                    'currency': 'eur',

                    'product_data': {

                        'name': f"{product.name}",

                        'description': f"Mietzeitraum: {start} bis {end} ({days} Tage)",

                    },

                    'unit_amount': int(product.price_per_day * days * 100),

                },

                'quantity': qty,

            }],

            mode='payment',

            metadata={

                "product_id": p_id, "start_date": start, "end_date": end,

                "quantity": qty, "customer_name": customer, "total_price": total_price

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

        existing = Booking.query.filter_by(stripe_session_id=session_id).first()

        

        if not existing:

            meta = session.metadata

            new_booking = Booking(

                product_id=int(meta['product_id']),

                customer_name=meta['customer_name'],

                start_date=meta['start_date'],

                end_date=meta['end_date'],

                quantity=int(meta['quantity']),

                total_price=float(meta['total_price']),

                stripe_session_id=session_id,

                status="Bezahlt & Reserviert"

            )

            db.session.add(new_booking)

            db.session.commit()

            

    return render_template_string(HTML_MESSAGE, title="Zahlung erfolgreich!", msg="Vielen Dank! Deine Buchung wurde registriert und dein Equipment ist fest für dich reserviert.", type="success")



@app.route('/payment-cancel')

def payment_cancel():

    return render_template_string(HTML_MESSAGE, title="Zahlung abgebrochen", msg="Der Zahlungsvorgang wurde abgebrochen. Es wurden keine Artikel reserviert.", type="error")



# --- USER STOREFRONT (UI) ---

HTML_STOREFRONT = """

<!DOCTYPE html>

<html lang="de">

<head>

    <meta charset="UTF-8">

    <meta name="viewport" content="width=device-width, initial-scale=1.0">

    <title>Equipment Rental Store</title>

    <script src="https://cdn.tailwindcss.com"></script>

    <script src="https://js.stripe.com/v3/"></script>

    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">

    <style> body { font-family: 'Inter', sans-serif; } </style>

</head>

<body class="bg-slate-50 text-slate-900 antialiased">



    <div class="min-h-screen flex flex-col">

        <nav class="bg-white border-b border-slate-200 sticky top-0 z-50">

            <div class="max-w-6xl mx-auto px-4 h-16 flex items-center justify-between">

                <div class="flex items-center space-x-3">

                    <div class="bg-indigo-600 text-white p-2 rounded-xl font-bold shadow-md shadow-indigo-200 text-lg tracking-wider">RE</div>

                    <span class="font-extrabold text-xl text-slate-800 tracking-tight">RentEngine<span class="text-indigo-600">.pro</span></span>

                </div>

                <a href="/admin" class="flex items-center space-x-2 border border-slate-300 text-slate-700 bg-slate-50 px-4 py-2 rounded-xl text-sm font-semibold hover:bg-slate-100 transition shadow-sm">

                    <span>Mitarbeiter Dashboard</span>

                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>

                </a>

            </div>

        </nav>



        <div class="bg-white border-b border-slate-150 py-8 px-4">

            <div class="max-w-6xl mx-auto">

                <h1 class="text-3xl font-extrabold text-slate-900 tracking-tight sm:text-4xl">Premium Equipment mieten.</h1>

                <p class="mt-2 text-slate-500 max-w-xl text-md">Wähle deine Wunschartikel, prüfe die Echtzeit-Verfügbarkeit und schließe deine Buchung sicher ab.</p>

            </div>

        </div>



        <div class="max-w-6xl mx-auto px-4 py-10 flex-1 grid grid-cols-1 lg:grid-cols-3 gap-8 w-full">

            

            <div class="lg:col-span-2 space-y-4">

                <h2 class="text-lg font-bold text-slate-800 mb-2 flex items-center space-x-2">

                    <svg class="w-5 h-5 text-indigo-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"></path></svg>

                    <span>Verfügbares Leihmaterial</span>

                </h2>

                

                {% for p in products %}

                <div class="bg-white rounded-2xl p-6 border border-slate-200 shadow-sm flex flex-col sm:flex-row justify-between sm:items-center gap-4 hover:border-indigo-300 transition duration-200">

                    <div class="space-y-1">

                        <h3 class="font-bold text-lg text-slate-800 tracking-tight">{{ p.name }}</h3>

                        <div class="flex items-center space-x-2 text-xs font-semibold text-slate-500">

                            <span class="bg-slate-100 px-2 py-0.5 rounded-md">Bestand: {{ p.stock }} Stk.</span>

                            <span class="text-emerald-600 flex items-center">

                                <span class="w-1.5 h-1.5 bg-emerald-500 rounded-full mr-1.5 animate-pulse"></span>Bereit zum Verleih

                            </span>

                        </div>

                    </div>

                    <div class="flex sm:flex-col justify-between sm:items-end items-center border-t sm:border-none pt-3 sm:pt-0">

                        <div>

                            <span class="text-2xl font-black text-slate-900">{{ "%.2f"|format(p.price_per_day) }} €</span>

                            <span class="text-xs text-slate-400 font-medium"> / Tag</span>

                        </div>

                        <button onclick="selectProduct({{ p.id }}, '{{ p.name }}')" class="mt-2 bg-slate-900 text-white font-semibold text-sm px-5 py-2.5 rounded-xl hover:bg-indigo-600 hover:shadow-lg hover:shadow-indigo-100 transition duration-150">

                            Auswählen

                        </button>

                    </div>

                </div>

                {% endfor %}

            </div>



            <div class="lg:col-span-1">

                <div class="bg-white rounded-2xl border border-slate-200 shadow-md p-6 sticky top-24 space-y-6">

                    <h2 class="text-lg font-bold text-slate-800 pb-3 border-b border-slate-100 flex items-center space-x-2">

                        <svg class="w-5 h-5 text-indigo-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>

                        <span>Miet-Konfigurator</span>

                    </h2>



                    <div class="space-y-4">

                        <div>

                            <label class="block text-xs font-bold text-slate-500 uppercase tracking-wider">Vollständiger Name</label>

                            <input id="customer-name" type="text" placeholder="z.B. Max Mustermann" class="w-full mt-1.5 px-4 py-2.5 bg-slate-50 border border-slate-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 transition">

                        </div>



                        <div>

                            <label class="block text-xs font-bold text-slate-500 uppercase tracking-wider">Gewähltes Objekt</label>

                            <input id="selected-product-name" type="text" readonly value="Bitte Produkt wählen..." class="w-full mt-1.5 px-4 py-2.5 bg-slate-100 border border-slate-200 rounded-xl text-sm font-semibold text-slate-700 focus:outline-none">

                            <input id="selected-product-id" type="hidden">

                        </div>



                        <div class="grid grid-cols-2 gap-3">

                            <div>

                                <label class="block text-xs font-bold text-slate-500 uppercase tracking-wider">Mietbeginn</label>

                                <input id="start-date" type="date" onchange="updateLiveCalculation()" class="w-full mt-1.5 px-3 py-2.5 border border-slate-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">

                            </div>

                            <div>

                                <label class="block text-xs font-bold text-slate-500 uppercase tracking-wider">Mietende</label>

                                <input id="end-date" type="date" onchange="updateLiveCalculation()" class="w-full mt-1.5 px-3 py-2.5 border border-slate-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">

                            </div>

                        </div>



                        <div>

                            <label class="block text-xs font-bold text-slate-500 uppercase tracking-wider">Anzahl Geräte</label>

                            <input id="quantity" type="number" value="1" min="1" onchange="updateLiveCalculation()" class="w-full mt-1.5 px-4 py-2.5 border border-slate-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">

                        </div>



                        <div id="live-status" class="hidden p-3.5 rounded-xl text-sm font-medium border"></div>



                        <div class="bg-slate-50 p-4 rounded-xl border border-slate-100 space-y-2">

                            <div class="flex justify-between text-xs font-semibold text-slate-500">

                                <span>Berechnete Miettage:</span>

                                <span id="calc-days">0 Tage</span>

                            </div>

                            <div class="flex justify-between items-baseline pt-2 border-t border-slate-200">

                                <span class="text-sm font-bold text-slate-700">Gesamtsumme:</span>

                                <span id="total-price" class="text-2xl font-black text-slate-900">0,00 €</span>

                            </div>

                        </div>



                        <button id="checkout-btn" disabled onclick="redirectToStripe()" class="w-full py-4 rounded-xl font-bold text-white bg-slate-300 cursor-not-allowed shadow-md transition duration-150">

                            Sicher bezahlen mit Stripe

                        </button>

                    </div>

                </div>

            </div>



        </div>

    </div>



    <script>

        const stripe = Stripe('{{ stripe_key }}');

        const today = new Date().toISOString().split('T')[0];

        document.getElementById('start-date').min = today;

        document.getElementById('end-date').min = today;



        function selectProduct(id, name) {

            document.getElementById('selected-product-id').value = id;

            document.getElementById('selected-product-name').value = name;

            updateLiveCalculation();

        }



        function updateLiveCalculation() {

            const pId = document.getElementById('selected-product-id').value;

            const start = document.getElementById('start-date').value;

            const end = document.getElementById('end-date').value;

            const qty = document.getElementById('quantity').value;

            

            if (!pId || !start || !end) return;



            fetch('/api/check-availability', {

                method: 'POST',

                headers: { 'Content-Type': 'application/json' },

                body: JSON.stringify({ product_id: pId, start_date: start, end_date: end, quantity: qty })

            }).then(res => res.json()).then(data => {

                document.getElementById('calc-days').innerText = data.days + " Tage";

                document.getElementById('total-price').innerText = data.total_price.toFixed(2) + " €";

                

                const statusDiv = document.getElementById('live-status');

                const btn = document.getElementById('checkout-btn');

                statusDiv.classList.remove('hidden');

                

                if (data.available) {

                    statusDiv.className = "p-3.5 rounded-xl text-sm font-semibold bg-emerald-50 border-emerald-200 text-emerald-800";

                    statusDiv.innerText = `✓ Verfügbar (Noch ${data.remaining_stock} Stück freigegeben)`;

                    btn.disabled = false;

                    btn.className = "w-full py-4 rounded-xl font-bold text-white bg-indigo-600 hover:bg-indigo-700 shadow-md shadow-indigo-100 cursor-pointer transition transform active:scale-[0.99]";

                } else {

                    statusDiv.className = "p-3.5 rounded-xl text-sm font-semibold bg-rose-50 border-rose-200 text-rose-800";

                    statusDiv.innerText = `✕ Ausgebucht! Nur noch ${data.remaining_stock} Stück frei im Zeitraum.`;

                    btn.disabled = true;

                    btn.className = "w-full py-4 rounded-xl font-bold text-white bg-slate-300 cursor-not-allowed shadow-none";

                }

            });

        }



        function redirectToStripe() {

            const pId = document.getElementById('selected-product-id').value;

            const start = document.getElementById('start-date').value;

            const end = document.getElementById('end-date').value;

            const qty = document.getElementById('quantity').value;

            const name = document.getElementById('customer-name').value;



            if(!name.trim()) {

                alert("Bitte gib deinen Namen ein, bevor du fortfährst.");

                return;

            }



            fetch('/api/create-checkout-session', {

                method: 'POST',

                headers: { 'Content-Type': 'application/json' },

                body: JSON.stringify({ product_id: pId, start_date: start, end_date: end, quantity: qty, customer_name: name })

            })

            .then(res => res.json())

            .then(session => {

                if(session.error) { alert(session.error); return; }

                return stripe.redirectToCheckout({ sessionId: session.id });

            }).catch(err => console.error(err));

        }

    </script>

</body>

</html>

"""



# --- ADMIN DASHBOARD (UI) ---

HTML_ADMIN = """

<!DOCTYPE html>

<html lang="de">

<head>

    <meta charset="UTF-8">

    <title>RentEngine | Admin Center</title>

    <script src="https://cdn.tailwindcss.com"></script>

    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;900&display=swap" rel="stylesheet">

    <style> body { font-family: 'Inter', sans-serif; } </style>

</head>

<body class="bg-slate-900 text-slate-100 antialiased min-h-screen">



    <div class="max-w-7xl mx-auto py-10 px-4 space-y-8">

        

        <header class="flex flex-col sm:flex-row justify-between sm:items-center gap-4 bg-slate-800 p-6 rounded-2xl border border-slate-700 shadow-xl">

            <div>

                <h1 class="text-2xl font-black tracking-tight text-white sm:text-3xl">RentEngine Admin HQ</h1>

                <p class="text-slate-400 text-sm">Zentrale Buchungsverwaltung & Bestandsplanung</p>

            </div>

            <a href="/" class="bg-indigo-600 text-white font-semibold text-sm px-5 py-2.5 rounded-xl hover:bg-indigo-700 transition flex items-center space-x-2 w-fit">

                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l-7-7m7-7H3"></path></svg>

                <span>Zum Kunden-Shop</span>

            </a>

        </header>



        <div class="grid grid-cols-1 md:grid-cols-3 gap-6">

            <div class="bg-slate-800 border border-slate-700 p-6 rounded-2xl shadow-md flex items-center space-x-4">

                <div class="p-3.5 bg-emerald-500/10 text-emerald-400 rounded-xl font-bold text-xl">€</div>

                <div>

                    <p class="text-xs font-bold text-slate-400 uppercase tracking-wider">Umsatz über Stripe</p>

                    <h3 class="text-2xl font-black text-white mt-1">{{ "%.2f"|format(revenue) }} €</h3>

                </div>

            </div>

            <div class="bg-slate-800 border border-slate-700 p-6 rounded-2xl shadow-md flex items-center space-x-4">

                <div class="p-3.5 bg-blue-500/10 text-blue-400 rounded-xl">⚡</div>

                <div>

                    <p class="text-xs font-bold text-slate-400 uppercase tracking-wider">Aktuell verliehen</p>

                    <h3 class="text-2xl font-black text-white mt-1">{{ active }} Geräte</h3>

                </div>

            </div>

            <div class="bg-slate-800 border border-slate-700 p-6 rounded-2xl shadow-md flex items-center space-x-4">

                <div class="p-3.5 bg-amber-500/10 text-amber-400 rounded-xl">⏰</div>

                <div>

                    <p class="text-xs font-bold text-slate-400 uppercase tracking-wider">Anstehende Abholungen</p>

                    <h3 class="text-2xl font-black text-white mt-1">{{ pending }} Buchungen</h3>

                </div>

            </div>

        </div>



        <div class="grid grid-cols-1 lg:grid-cols-3 gap-8">

            

            <div class="lg:col-span-2 bg-slate-800 border border-slate-700 rounded-2xl shadow-xl overflow-hidden flex flex-col">

                <div class="p-5 border-b border-slate-700 bg-slate-800/50 font-bold text-white flex items-center space-x-2">

                    <span>Mietaufträge</span>

                </div>

                <div class="overflow-x-auto flex-1">

                    <table class="w-full text-left border-collapse whitespace-nowrap">

                        <thead>

                            <tr class="bg-slate-800 border-b border-slate-700 text-xs font-bold uppercase text-slate-400">

                                <th class="p-4">Kunde</th>

                                <th class="p-4">Equipment</th>

                                <th class="p-4">Menge</th>

                                <th class="p-4">Zeitraum</th>

                                <th class="p-4">Umsatz</th>

                                <th class="p-4">Status</th>

                                <th class="p-4 text-right">Workflow</th>

                            </tr>

                        </thead>

                        <tbody class="divide-y divide-slate-700 text-sm">

                            {% for b in bookings %}

                            <tr class="hover:bg-slate-750/40 transition">

                                <td class="p-4 font-bold text-white">{{ b.customer_name }}</td>

                                <td class="p-4 text-slate-300">{{ b.product.name }}</td>

                                <td class="p-4 text-slate-300">{{ b.quantity }}x</td>

                                <td class="p-4 text-xs font-medium text-slate-400">{{ b.start_date }} bis {{ b.end_date }}</td>

                                <td class="p-4 font-bold text-indigo-400">{{ "%.2f"|format(b.total_price) }} €</td>

                                <td class="p-4">

                                    <span class="px-2.5 py-1 rounded-full text-xs font-extrabold 

                                        {% if b.status == 'Bezahlt & Reserviert' %} bg-amber-500/10 text-amber-400 border border-amber-500/20

                                        {% elif b.status == 'Abgeholt' %} bg-blue-500/10 text-blue-400 border border-blue-500/20

                                        {% else %} bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 {% endif %}">

                                        {{ b.status }}

                                    </span>

                                </td>

                                <td class="p-4 text-right">

                                    {% if b.status == 'Bezahlt & Reserviert' %}

                                    <a href="/admin/booking/status/{{ b.id }}/Abgeholt" class="text-xs bg-blue-600 text-white font-bold px-3 py-1.5 rounded-lg hover:bg-blue-700 transition">Herausgeben</a>

                                    {% elif b.status == 'Abgeholt' %}

                                    <a href="/admin/booking/status/{{ b.id }}/Zurückgebracht" class="text-xs bg-emerald-600 text-white font-bold px-3 py-1.5 rounded-lg hover:bg-emerald-700 transition">Rückgabe</a>

                                    {% else %}

                                    <span class="text-xs text-slate-500 font-medium">Archiviert</span>

                                    {% endif %}

                                </td>

                            </tr>

                            {% else %}

                            <tr><td colspan="7" class="p-10 text-center text-slate-500 font-medium">Bisher keine Buchungen vorhanden.</td></tr>

                            {% endfor %}

                        </tbody>

                    </table>

                </div>

            </div>



            <div class="lg:col-span-1 space-y-6">

                

                <div class="bg-slate-800 border border-slate-700 p-6 rounded-2xl shadow-xl">

                    <h3 class="text-md font-bold text-white mb-4 flex items-center space-x-2"><span>+ Inventar aufstocken</span></h3>

                    <form action="/admin/product/add" method="POST" class="space-y-4">

                        <div>

                            <label class="block text-xs font-bold text-slate-400 uppercase">Gerätename</label>

                            <input name="name" type="text" required placeholder="z.B. Canon EOS R5" class="w-full mt-1.5 px-4 py-2 bg-slate-900 border border-slate-700 rounded-xl text-sm focus:outline-none focus:border-indigo-500 text-white">

                        </div>

                        <div class="grid grid-cols-2 gap-3">

                            <div>

                                <label class="block text-xs font-bold text-slate-400 uppercase">Gesamtbestand</label>

                                <input name="stock" type="number" min="1" required placeholder="5" class="w-full mt-1.5 px-4 py-2 bg-slate-900 border border-slate-700 rounded-xl text-sm focus:outline-none text-white">

                            </div>

                            <div>

                                <label class="block text-xs font-bold text-slate-400 uppercase">Preis / Tag</label>

                                <input name="price" type="number" step="0.01" min="0" required placeholder="39.00" class="w-full mt-1.5 px-4 py-2 bg-slate-900 border border-slate-700 rounded-xl text-sm focus:outline-none text-white">

                            </div>

                        </div>

                        <button type="submit" class="w-full bg-indigo-600 text-white font-semibold py-2.5 rounded-xl hover:bg-indigo-700 transition text-sm">Produkt hinzufügen</button>

                    </form>

                </div>



                <div class="bg-slate-800 border border-slate-700 rounded-2xl shadow-xl overflow-hidden">

                    <div class="p-4 border-b border-slate-700 bg-slate-800/50 font-bold text-xs uppercase tracking-wider text-slate-400">Aktuelles Inventar</div>

                    <div class="divide-y divide-slate-700 max-h-64 overflow-y-auto">

                        {% for p in products %}

                        <div class="p-4 flex justify-between items-center hover:bg-slate-750 transition text-sm">

                            <div>

                                <h4 class="font-bold text-white">{{ p.name }}</h4>

                                <p class="text-xs text-slate-400">Menge: {{ p.stock }} | {{ "%.2f"|format(p.price_per_day) }}€/Tag</p>

                            </div>

                            <a href="/admin/product/delete/<% if 1 %>%>{{ p.id }}<% endif %>" class="text-xs font-bold text-rose-400 hover:text-rose-500 bg-rose-500/10 hover:bg-rose-500/20 px-2.5 py-1.5 rounded-lg transition">Löschen</a>

                        </div>

                        {% endfor %}

                    </div>

                </div>



            </div>



        </div>

    </div>

</body>

</html>

"""



# --- AUXILIARY MESSAGE INTERFACE ---

HTML_MESSAGE = """

<!DOCTYPE html>

<html lang="de">

<head>

    <meta charset="UTF-8"><title>{{ title }}</title>

    <script src="https://cdn.tailwindcss.com"></script>

    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">

    <style> body { font-family: 'Inter', sans-serif; } </style>

</head>

<body class="bg-slate-50 min-h-screen flex items-center justify-center p-4">

    <div class="max-w-md w-full bg-white p-8 rounded-2xl shadow-xl border border-slate-200 text-center space-y-5">

        <div class="w-16 h-16 mx-auto rounded-full flex items-center justify-center font-black text-2xl 

            {% if type == 'success' %} bg-emerald- 

