from flask import Flask, render_template, request, redirect, jsonify, Response, session, url_for
import psycopg2
import os
import time
import json
import threading
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ipl-auction-secret-2024")

DATABASE_URL = os.environ.get("DATABASE_URL")

# In-memory auction state
auction_state = {
    "live_player_id": None,
    "timer_end": None,
    "current_bid": 0,
    "current_bidder": None,
    "status": "idle"  # idle | live | ended
}
auction_lock = threading.Lock()
sse_clients = []
sse_lock = threading.Lock()

PLAYER_IMAGES = {
    "Virat Kohli":   "https://documents.iplt20.com/ipl/IPLHeadshot2024/2.png",
    "Rohit Sharma":  "https://documents.iplt20.com/ipl/IPLHeadshot2024/107.png",
    "Jasprit Bumrah":"https://documents.iplt20.com/ipl/IPLHeadshot2024/1124.png",
    "Hardik Pandya": "https://documents.iplt20.com/ipl/IPLHeadshot2024/184.png",
    "KL Rahul":      "https://documents.iplt20.com/ipl/IPLHeadshot2024/1125.png",
}

TEAM_COLORS = {
    "RCB": "#EC1C24",
    "MI":  "#005DA0",
    "LSG": "#A4C639",
    "CSK": "#F9CD05",
    "KKR": "#3A225D",
    "DC":  "#0078BC",
    "SRH": "#F26522",
    "RR":  "#FF69B4",
    "PBKS":"#AA4545",
    "GT":  "#1C4966",
}

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def notify_clients(data):
    msg = f"data: {json.dumps(data)}\n\n"
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.append(msg)
            except Exception:
                dead.append(q)
        for d in dead:
            sse_clients.remove(d)

def get_auction_snapshot():
    with auction_lock:
        snap = dict(auction_state)
        if snap["timer_end"]:
            remaining = max(0, snap["timer_end"] - time.time())
            snap["remaining"] = int(remaining)
        else:
            snap["remaining"] = 0
    return snap

# ── Timer expiry background thread ──────────────────────────────────────────
def timer_watcher():
    while True:
        time.sleep(1)
        with auction_lock:
            if auction_state["status"] == "live" and auction_state["timer_end"]:
                if time.time() >= auction_state["timer_end"]:
                    auction_state["status"] = "ended"
                    snap = dict(auction_state)
        if auction_state["status"] == "ended":
            notify_clients({"type": "auction_ended", "state": get_auction_snapshot()})

watcher = threading.Thread(target=timer_watcher, daemon=True)
watcher.start()

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        role = request.form.get("role")
        name = request.form.get("username", "Guest")
        session["role"] = role
        session["username"] = name
        return redirect("/dashboard")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/dashboard")
def dashboard():
    role = session.get("role")
    if not role:
        return redirect("/")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, team, role, strike_rate, base_price, auction_price, sold_to FROM players ORDER BY id")
    rows = cur.fetchall()
    conn.close()

    players = []
    for r in rows:
        players.append({
            "id": r[0], "name": r[1], "team": r[2], "role": r[3],
            "strike_rate": r[4], "base_price": r[5], "auction_price": r[6],
            "sold_to": r[7],
            "image": PLAYER_IMAGES.get(r[1], "https://placehold.co/160x160/1a1a2e/gold?text=Player"),
            "team_color": TEAM_COLORS.get(r[2], "#FFD700"),
        })

    snap = get_auction_snapshot()
    live_player = None
    if snap["live_player_id"]:
        for p in players:
            if p["id"] == snap["live_player_id"]:
                live_player = p
                break

    return render_template("dashboard.html", role=role,
                           username=session.get("username"),
                           players=players,
                           snap=snap,
                           live_player=live_player)

# ── Admin: start auction for a player ───────────────────────────────────────
@app.route("/admin/start", methods=["POST"])
def admin_start():
    if session.get("role") != "admin":
        return jsonify({"error": "forbidden"}), 403
    player_id = int(request.json.get("player_id"))

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT base_price FROM players WHERE id=%s", (player_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "not found"}), 404

    with auction_lock:
        auction_state["live_player_id"] = player_id
        auction_state["timer_end"] = time.time() + 60
        auction_state["current_bid"] = row[0]
        auction_state["current_bidder"] = None
        auction_state["status"] = "live"

    notify_clients({"type": "auction_started", "state": get_auction_snapshot()})
    return jsonify({"ok": True})

# ── Admin: end auction early ─────────────────────────────────────────────────
@app.route("/admin/end", methods=["POST"])
def admin_end():
    if session.get("role") != "admin":
        return jsonify({"error": "forbidden"}), 403

    with auction_lock:
        if auction_state["status"] == "live":
            auction_state["status"] = "ended"
            auction_state["timer_end"] = time.time()

    # save to DB
    _finalize_auction()
    notify_clients({"type": "auction_ended", "state": get_auction_snapshot()})
    return jsonify({"ok": True})

def _finalize_auction():
    with auction_lock:
        pid = auction_state["live_player_id"]
        price = auction_state["current_bid"]
        bidder = auction_state["current_bidder"]
        status = auction_state["status"]

    if pid and status == "ended":
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE players SET auction_price=%s, sold_to=%s WHERE id=%s",
            (price, bidder, pid)
        )
        conn.commit()
        conn.close()

# ── Client: place bid ────────────────────────────────────────────────────────
@app.route("/bid", methods=["POST"])
def bid():
    if session.get("role") != "client":
        return jsonify({"error": "forbidden"}), 403

    data = request.json
    bid_amount = int(data.get("amount", 0))
    bidder = session.get("username", "Unknown")

    with auction_lock:
        if auction_state["status"] != "live":
            return jsonify({"error": "No live auction"}), 400
        if time.time() > auction_state["timer_end"]:
            return jsonify({"error": "Timer expired"}), 400
        if bid_amount <= auction_state["current_bid"]:
            return jsonify({"error": "Bid too low"}), 400

        auction_state["current_bid"] = bid_amount
        auction_state["current_bidder"] = bidder
        # extend timer by 10s if < 10s left
        remaining = auction_state["timer_end"] - time.time()
        if remaining < 10:
            auction_state["timer_end"] = time.time() + 10

    notify_clients({"type": "bid_placed", "state": get_auction_snapshot()})
    return jsonify({"ok": True, "state": get_auction_snapshot()})

# ── SSE stream ────────────────────────────────────────────────────────────────
@app.route("/stream")
def stream():
    client_queue = []
    with sse_lock:
        sse_clients.append(client_queue)

    def generate():
        # send current state immediately
        yield f"data: {json.dumps({'type': 'init', 'state': get_auction_snapshot()})}\n\n"
        while True:
            if client_queue:
                msg = client_queue.pop(0)
                yield msg
            else:
                # heartbeat every 15s
                yield f": heartbeat\n\n"
                time.sleep(1)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── API: current state ────────────────────────────────────────────────────────
@app.route("/api/state")
def api_state():
    return jsonify(get_auction_snapshot())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
