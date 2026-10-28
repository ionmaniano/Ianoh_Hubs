import os, re, time, base64, logging
from datetime import datetime, timezone, timedelta
from functools import wraps

import requests
from flask import Flask, request, jsonify, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from sqlalchemy import event
from time import perf_counter


load_dotenv()

E = os.environ.get


db_url = E("DATABASE_URL", "sqlite:///ianoh.db")

if db_url.startswith("postgres://"):
    db_url = db_url.replace(
        "postgres://", "postgresql+psycopg://", 1
    )
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace(
        "postgresql://", "postgresql+psycopg://", 1
    )

if db_url.startswith("postgresql+psycopg://"):
    from sqlalchemy.engine import make_url

    url = make_url(db_url)
    query = dict(url.query)
    query["sslmode"] = "require"
    query["channel_binding"] = "disable"

    db_url = url.set(query=query).render_as_string(
        hide_password=False
    )
app = Flask(__name__, static_folder="static", static_url_path="")

app.config.update(
    SQLALCHEMY_DATABASE_URI=db_url,
    SECRET_KEY=E("SECRET_KEY") or os.urandom(32),
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(E("RENDER")),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024
)


db = SQLAlchemy(app)

def log_query_start(conn, cursor, statement, parameters, context, executemany):
    conn.info.setdefault("query_start_times", []).append(perf_counter())


def log_query_end(conn, cursor, statement, parameters, context, executemany):
    start = conn.info["query_start_times"].pop()
    elapsed = perf_counter() - start
    app.logger.warning("SQL query took %.3f seconds", elapsed)


with app.app_context():
    event.listen(db.engine, "before_cursor_execute", log_query_start)
    event.listen(db.engine, "after_cursor_execute", log_query_end)

# Use the same logger name throughout the application.
log = logging.getLogger("ianoh")
log.setLevel(logging.INFO)

FAILS = {}

# ---------- HOMEPAGE ----------
@app.get("/")
def home():
    return send_from_directory(app.static_folder, "index.html")


# ---------- DATABASE MODELS ----------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(200), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    pw = db.Column(db.String(300), nullable=False)
    role = db.Column(db.String(20), default="customer")


class Shoe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    type = db.Column(db.String(60), nullable=False)
    size = db.Column(db.Float, nullable=False)
    cond = db.Column(db.String(20), default="Good")
    price = db.Column(db.Integer, nullable=False)
    offer = db.Column(db.Integer, default=0)
    img = db.Column(db.Text, default="")
    sold = db.Column(db.Boolean, default=False)
    hidden = db.Column(db.Boolean, default=False)

    def final(self):
        return max(1, round(self.price * (100 - (self.offer or 0)) / 100))

    def dict(self):
        return dict(
            id=self.id, name=self.name, type=self.type, size=self.size,
            cond=self.cond, price=self.price, offer=self.offer or 0,
            img=self.img or "", sold=self.sold,
            hidden=bool(self.hidden), final=self.final()
        )


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shoe_id = db.Column(db.Integer, db.ForeignKey("shoe.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    phone = db.Column(db.String(15))
    amount = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default="pending")
    checkout_id = db.Column(db.String(80), unique=True)
    receipt = db.Column(db.String(40))
    note = db.Column(db.String(255))
    method = db.Column(db.String(10), default="pickup")
    address = db.Column(db.String(255))
    fee = db.Column(db.Integer, default=0)
    stage = db.Column(db.String(20), default="new")
    created = db.Column(db.DateTime, default=datetime.utcnow)
    shoe = db.relationship("Shoe")
    user = db.relationship("User")

    def dict(self):
        return dict(
            id=self.id, shoe=self.shoe.name, customer=self.user.name,
            phone=self.phone, amount=self.amount, status=self.status,
            receipt=self.receipt, note=self.note, method=self.method,
            address=self.address, fee=self.fee, stage=self.stage,
            created=self.created.strftime("%d %b %H:%M")
        )


class C2B(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    trans_id = db.Column(db.String(40), unique=True)
    amount = db.Column(db.Float)
    msisdn = db.Column(db.String(80))
    bill_ref = db.Column(db.String(80))
    name = db.Column(db.String(120))
    created = db.Column(db.DateTime, default=datetime.utcnow)


# ---------- HELPERS ----------
def err(msg, code=400):
    return jsonify(error=msg), code


def current_user():
    return db.session.get(User, session["uid"]) if "uid" in session else None


def need(role=None):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u = current_user()
            if not u:
                return err("Please log in.", 401)
            if role and u.role != role:
                return err("Admins only.", 403)
            return f(u, *a, **k)
        return w
    return deco


def norm_phone(p):
    p = re.sub(r"[\s+\-]", "", p or "")
    if re.fullmatch(r"0[17]\d{8}", p):
        p = "254" + p[1:]
    elif re.fullmatch(r"[17]\d{8}", p):
        p = "254" + p
    return p if re.fullmatch(r"254[17]\d{8}", p) else None


def mark_paid(o, receipt):
    o.status, o.receipt = "paid", receipt
    o.shoe.sold = True


# ---------- M-PESA ----------
def mp_base():
    return (
        "https://api.safaricom.co.ke"
        if E("MPESA_ENV", "sandbox") == "production"
        else "https://sandbox.safaricom.co.ke"
    )


def mp_token():
    r = requests.get(
        mp_base() + "/oauth/v1/generate?grant_type=client_credentials",
        auth=(
            E("MPESA_CONSUMER_KEY", ""),
            E("MPESA_CONSUMER_SECRET", "")
        ),
        timeout=15
    )
    r.raise_for_status()
    return r.json()["access_token"]




def stk_push(o):
    ts = datetime.now(
        timezone(timedelta(hours=3))
    ).strftime("%Y%m%d%H%M%S")

    sc = E("MPESA_SHORTCODE", "")
    pw = base64.b64encode(
        (sc + E("MPESA_PASSKEY", "") + ts).encode()
    ).decode()

    cb = E("MPESA_CALLBACK_URL", "")

    if E("MPESA_CALLBACK_TOKEN"):
        cb += (
            ("&" if "?" in cb else "?")
            + "token="
            + E("MPESA_CALLBACK_TOKEN")
        )

    body = {
    "BusinessShortCode": sc,
    "Password": pw,
    "Timestamp": ts,
    "TransactionType": "CustomerPayBillOnline",
    "Amount": int(o.amount),
    "PartyA": o.phone,
    "PartyB": sc,
    "PhoneNumber": o.phone,
    "CallBackURL": cb,
    "AccountReference": f"IH{o.id}",
    "TransactionDesc": "Ianoh_hubs"
}

    r = requests.post(
        mp_base() + "/mpesa/stkpush/v1/processrequest",
        json=body,
        timeout=20,
        headers={
            "Authorization": "Bearer " + mp_token()
        }
    )

    try:
        data = r.json()
    except ValueError:
        data = {"raw_response": r.text[:1000]}

    if not r.ok:
        log.error(
            "Daraja STK error: HTTP %s; response: %s",
            r.status_code,
            data
        )
        raise RuntimeError(
            f"Daraja rejected STK Push "
            f"(HTTP {r.status_code}): {data}"
        )

    return data


ACK = {"ResultCode": 0, "ResultDesc": "Accepted"}



@app.post("/hooks/stk")
def stk_callback():
    if E("MPESA_CALLBACK_TOKEN") and request.args.get("token") != E("MPESA_CALLBACK_TOKEN"):
        log.warning("STK callback rejected: token mismatch")
        return err("Forbidden", 403)

    payload = request.get_json(silent=True) or {}
    log.info("STK callback payload: %s", payload)

    cb = (payload.get("Body") or {}).get("stkCallback") or {}
    cid = cb.get("CheckoutRequestID")
    o = Order.query.filter_by(checkout_id=cid).first() if cid else None
    code = cb.get("ResultCode")

    log.info(
        "STK callback checkout_id=%s result_code=%s order_found=%s",
        cid, code, bool(o)
    )

    if o and o.status == "pending":
        if code == 0:
            items = {
                i.get("Name"): i.get("Value")
                for i in (cb.get("CallbackMetadata") or {}).get("Item", [])
            }

            try:
                amount = float(items.get("Amount", 0))
            except (TypeError, ValueError):
                amount = 0

            if amount >= o.amount:
                mark_paid(o, items.get("MpesaReceiptNumber"))
            else:
                o.status = "failed"
                o.note = "Amount paid did not match the price."
        else:
            o.status = "cancelled" if code == 1032 else "failed"
            o.note = (
                cb.get("ResultDesc") or "Payment was not completed."
            )[:250]

        db.session.commit()

    return jsonify(ACK)


@app.post("/hooks/c2b/validation")
def c2b_validation():
    return jsonify(ACK)


@app.post("/hooks/c2b/confirmation")
def c2b_confirmation():
    d = request.get_json(silent=True) or {}
    tid = d.get("TransID")

    if tid and not C2B.query.filter_by(trans_id=tid).first():
        amt = float(d.get("TransAmount") or 0)
        db.session.add(C2B(
            trans_id=tid,
            amount=amt,
            msisdn=str(d.get("MSISDN", ""))[:80],
            bill_ref=str(d.get("BillRefNumber", ""))[:80],
            name=" ".join(filter(None, [
                d.get("FirstName"), d.get("LastName")
            ]))[:120]
        ))

        m = re.fullmatch(r"(?i)IH(\d+)", str(d.get("BillRefNumber", "")).strip())
        o = db.session.get(Order, int(m.group(1))) if m else None

        if o and o.status == "pending" and amt >= o.amount:
            mark_paid(o, tid)

        db.session.commit()

    return jsonify(ACK)


# ---------- API: AUTHENTICATION ----------
@app.get("/api/me")
def me():
    u = current_user()
    return jsonify(
        user=dict(name=u.name, email=u.email, role=u.role) if u else None
    )


@app.post("/api/signup")
def signup():
    d = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    name = (d.get("name") or "").strip()
    pw = d.get("password") or ""

    if not re.fullmatch(r"\S+@\S+\.\S+", email) or not name or len(pw) < 8:
        return err("Enter your name, a valid email and a password of at least 8 characters.")

    if User.query.filter_by(email=email).first():
        return err("That email is already registered. Log in instead.", 409)

    u = User(
        email=email,
        name=name[:120],
        pw=generate_password_hash(pw)
    )
    db.session.add(u)
    db.session.commit()
    session["uid"] = u.id

    return jsonify(user=dict(name=u.name, email=u.email, role=u.role))


@app.post("/api/login")
def login():
    d = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    key = (
        request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip(),
        email
    )
    now = time.time()
    FAILS[key] = [t for t in FAILS.get(key, []) if now - t < 600]

    if len(FAILS[key]) >= 5:
        return err("Too many attempts. Wait 10 minutes and try again.", 429)

    u = User.query.filter_by(email=email).first()

    if not u or not check_password_hash(u.pw, d.get("password") or ""):
        FAILS[key].append(now)
        return err("Email or password is wrong. Check both and try again.", 401)

    FAILS.pop(key, None)
    session["uid"] = u.id
    return jsonify(user=dict(name=u.name, email=u.email, role=u.role))


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


# ---------- API: SHOES ----------
@app.get("/api/shoes")
def shoes():
    q, u = Shoe.query.order_by(Shoe.id.desc()), current_user()
    if not (u and u.role == "admin"):
        q = q.filter_by(hidden=False)
    return jsonify([s.dict() for s in q])


def shoe_from(d, s):
    try:
        s.name = (d.get("name") or "").strip()[:120]
        s.type = (d.get("type") or "").strip()[:60]
        s.size = float(d.get("size"))
        s.price = int(d.get("price"))
        s.offer = int(d.get("offer") or 0)
    except (TypeError, ValueError):
        return "Check the size, price and offer values."

    s.cond = d.get("cond") if d.get("cond") in ("Like new", "Good", "Fair") else "Good"

    if not s.name or not s.type or s.size <= 0 or s.price <= 0 or not 0 <= s.offer <= 90:
        return "Name, type, size and price are required; offer must be 0 to 90%."

    img = d.get("img") or ""
    if img:
        if not img.startswith("data:image/") or len(img) > 600_000:
            return "Photo must be a small image."
        s.img = img


@app.post("/api/shoes")
@need("admin")
def add_shoe(u):
    s = Shoe()
    e = shoe_from(request.get_json(silent=True) or {}, s)
    if e:
        return err(e)
    db.session.add(s)
    db.session.commit()
    return jsonify(s.dict()), 201


@app.put("/api/shoes/<int:sid>")
@need("admin")
def edit_shoe(u, sid):
    s = db.get_or_404(Shoe, sid)
    e = shoe_from(request.get_json(silent=True) or {}, s)
    if e:
        db.session.rollback()
        return err(e)
    db.session.commit()
    return jsonify(s.dict())


@app.delete("/api/shoes/<int:sid>")
@need("admin")
def del_shoe(u, sid):
    s = db.get_or_404(Shoe, sid)

    if Order.query.filter_by(shoe_id=sid).first():
        s.sold = True
        db.session.commit()
        return jsonify(
            ok=True,
            note="Shoe has orders, so it was marked sold instead of deleted."
        )

    db.session.delete(s)
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/shoes/<int:sid>/hidden")
@need("admin")
def toggle_hidden(u, sid):
    s = db.get_or_404(Shoe, sid)
    s.hidden = not s.hidden
    db.session.commit()
    return jsonify(hidden=s.hidden)


# ---------- API: PAYMENTS AND ORDERS ----------
@app.post("/api/pay")
@need()
def pay(u):
    d = request.get_json(silent=True) or {}
    s = db.session.get(Shoe, d.get("shoe_id") or 0)
    phone = norm_phone(d.get("phone"))

    if not s or s.sold:
        return err("This pair is no longer available.", 409)

    if not phone:
        return err("Enter a Safaricom number like 0712 345 678.")

    method = "delivery" if d.get("method") == "delivery" else "pickup"
    address = (d.get("address") or "").strip()[:250]

    if method == "delivery" and len(address) < 5:
        return err("Enter your delivery address.")

    if Order.query.filter(
        Order.shoe_id == s.id,
        Order.status == "pending",
        Order.user_id != u.id,
        Order.created > (
    datetime.now(timezone.utc).replace(tzinfo=None)
    - timedelta(minutes=15)
)    ).first():
        return err("Someone is paying for this pair right now. Try again in a few minutes.", 409)

    fee = int(E("DELIVERY_FEE", "300")) if method == "delivery" else 0
    o = Order(
        shoe_id=s.id,
        user_id=u.id,
        phone=phone,
        amount=s.final() + fee,
        method=method,
        address=address,
        fee=fee
    )
    db.session.add(o)
    db.session.commit()

    try:
        r = stk_push(o)
        if r.get("ResponseCode") != "0":
            raise RuntimeError(
                r.get("errorMessage") or r.get("ResponseDescription") or "rejected"
            )
        o.checkout_id = r["CheckoutRequestID"]
        db.session.commit()
    except Exception:
        log.exception("STK push failed")
        o.status, o.note = "failed", "Could not start M-Pesa payment."
        db.session.commit()
        return err("We could not start the M-Pesa prompt. Try again in a moment.", 502)

    return jsonify(order_id=o.id)


@app.get("/api/orders/<int:oid>")
@need()
def order_status(u, oid):
    o = db.get_or_404(Order, oid)
    if o.user_id != u.id and u.role != "admin":
        return err("Not found.", 404)
    return jsonify(status=o.status, receipt=o.receipt, note=o.note)


@app.get("/api/admin/orders")
@need("admin")
def admin_orders(u):
    paid = Order.query.filter_by(status="paid").all()
    stats = dict(
        revenue=sum(o.amount for o in paid),
        paid=len(paid),
        to_pack=sum(1 for o in paid if o.stage == "new")
    )

    return jsonify(
        stats=stats,
        orders=[
            o.dict()
            for o in Order.query.order_by(Order.id.desc()).limit(100)
        ],
        c2b=[
            dict(
                trans_id=c.trans_id,
                amount=c.amount,
                name=c.name,
                bill_ref=c.bill_ref,
                created=c.created.strftime("%d %b %H:%M")
            )
            for c in C2B.query.order_by(C2B.id.desc()).limit(50)
        ]
    )


@app.get("/api/my-orders")
@need()
def my_orders(u):
    return jsonify([
        o.dict()
        for o in Order.query.filter_by(user_id=u.id)
        .order_by(Order.id.desc()).limit(50)
    ])


@app.post("/api/admin/orders/<int:oid>/stage")
@need("admin")
def set_stage(u, oid):
    o = db.get_or_404(Order, oid)
    st = (request.get_json(silent=True) or {}).get("stage")

    if o.status != "paid" or st not in ("new", "packed", "dispatched", "delivered"):
        return err("Only paid orders can change stage.")

    o.stage = st
    db.session.commit()
    return jsonify(ok=True)


@app.get("/api/config")
def config():
    return jsonify(
        whatsapp=re.sub(r"\D", "", E("WHATSAPP_NUMBER", "")),
        delivery_fee=int(E("DELIVERY_FEE", "300"))
    )


# ---------- HEALTH CHECK ----------
@app.get("/healthz")
def health():
    return "ok"


# ---------- DATABASE INITIALIZATION ----------

# ---------- DATABASE INITIALIZATION ----------

# ---------- DATABASE INITIALIZATION ----------
def init():
    db.create_all()

    em = E("ADMIN_EMAIL")
    pw = E("ADMIN_PASSWORD")

    if not em or not pw:
        log.warning("ADMIN_EMAIL or ADMIN_PASSWORD is missing.")
        return

    # Find the existing admin account
    admin = User.query.filter_by(role="admin").first()

    if admin:
        # Update the existing admin credentials
        admin.email = em.strip().lower()
        admin.pw = generate_password_hash(pw)
        admin.name = "Admin"
        log.info("Existing admin credentials updated.")
    else:
        # Create the first admin account
        admin = User(
            email=em.strip().lower(),
            name="Admin",
            pw=generate_password_hash(pw),
            role="admin"
        )
        db.session.add(admin)
        log.info("Initial admin account created.")

    db.session.commit()



with app.app_context():
    init()


if __name__ == "__main__":
    app.run(debug=False, port=int(E("PORT", 5000)))
