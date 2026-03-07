from flask import Flask, render_template, request, redirect, session, url_for, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
import psycopg2
import os
import time
import threading
import secrets

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ipl_auction_secret_2024")
socketio = SocketIO(app, cors_allowed_origins="*")

# Token store: { token: { "role": "admin"/"client", "team": "MI"/... } }
# This replaces cookie-based sessions so each browser tab is independent
active_tokens = {}

DATABASE_URL = os.environ.get("DATABASE_URL")

# Team credentials (team_name: password)
TEAM_CREDENTIALS = {
    "MI":  "mi123",
    "RCB": "rcb123",
    "LSG": "lsg123",
    "CSK": "csk123",
    "KKR": "kkr123",
}

# Team budgets (in Lakhs)
TEAM_BUDGETS = {
    "MI":  10000,
    "RCB": 10000,
    "LSG": 10000,
    "CSK": 10000,
    "KKR": 10000,
}

# Auction state (in-memory)
auction_state = {
    "active": False,
    "current_player_id": None,
    "time_left": 0,
    "full_duration": 60,
    "timer_running": False,
    "highest_bidder": None,
    "highest_bid": 0,
    "sold_to": None,
}

auction_timer_thread = None


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def get_players():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM players ORDER BY id")
    players = cur.fetchall()
    conn.close()
    return players


def get_player(player_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM players WHERE id=%s", (player_id,))
    player = cur.fetchone()
    conn.close()
    return player


def update_auction_price(player_id, price, team):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE players SET auction_price=%s, sold_to=%s WHERE id=%s",
        (price, team, player_id)
    )
    conn.commit()
    conn.close()


def run_auction_timer():
    global auction_state
    while auction_state["timer_running"] and auction_state["time_left"] > 0:
        time.sleep(1)
        auction_state["time_left"] -= 1
        socketio.emit("timer_update", {
            "time_left": auction_state["time_left"],
            "highest_bidder": auction_state["highest_bidder"],
            "highest_bid": auction_state["highest_bid"],
        }, room="auction_room", namespace="/")

        if auction_state["time_left"] == 0:
            auction_state["timer_running"] = False
            pid = auction_state["current_player_id"]
            if pid and auction_state["highest_bidder"]:
                update_auction_price(pid, auction_state["highest_bid"], auction_state["highest_bidder"])
                auction_state["sold_to"] = auction_state["highest_bidder"]
                socketio.emit("auction_ended", {
                    "sold_to": auction_state["highest_bidder"],
                    "sold_price": auction_state["highest_bid"],
                    "player_id": pid,
                }, room="auction_room", namespace="/")
            else:
                socketio.emit("auction_ended", {
                    "sold_to": None,
                    "sold_price": 0,
                    "player_id": pid,
                }, room="auction_room", namespace="/")
            auction_state["active"] = False


# ─── ROUTES ────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def home():
    return render_template("app.html")


@app.route("/admin-login", methods=["POST"])
def admin_login():
    password = request.form.get("password")
    if password == os.environ.get("ADMIN_PASSWORD", "admin123"):
        token = secrets.token_urlsafe(16)
        active_tokens[token] = {"role": "admin", "team": "ADMIN"}
        return redirect(f"/dashboard?token={token}")
    return render_template("app.html", error="Invalid admin password")


@app.route("/team-login", methods=["POST"])
def team_login():
    team = request.form.get("team").upper()
    password = request.form.get("password")
    if team in TEAM_CREDENTIALS and TEAM_CREDENTIALS[team] == password:
        token = secrets.token_urlsafe(16)
        active_tokens[token] = {"role": "client", "team": team}
        return redirect(f"/dashboard?token={token}")
    return render_template("app.html", error="Invalid team credentials")


@app.route("/logout")
def logout():
    token = request.args.get("token", "")
    active_tokens.pop(token, None)
    return redirect("/")


@app.route("/dashboard")
def dashboard():
    token = request.args.get("token", "")
    user = active_tokens.get(token)
    if not user:
        return redirect("/")
    players = get_players()
    role = user["role"]
    team = user["team"]
    budget = TEAM_BUDGETS.get(team, 0)
    return render_template("app.html",
                           role=role,
                           team=team,
                           token=token,
                           players=players,
                           budget=budget,
                           auction_state=auction_state)


# ─── ADMIN API ──────────────────────────────────────────────

@app.route("/admin/start-auction", methods=["POST"])
def start_auction():
    token = request.json.get("token", "")
    if active_tokens.get(token, {}).get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    global auction_timer_thread
    data = request.json
    player_id = data.get("player_id")
    duration = int(data.get("duration", 60))

    player = get_player(player_id)
    if not player:
        return jsonify({"error": "Player not found"}), 404

    # Stop any existing timer thread before starting a new one
    auction_state["timer_running"] = False
    if auction_timer_thread and auction_timer_thread.is_alive():
        auction_timer_thread.join(timeout=2)

    auction_state["active"] = True
    auction_state["current_player_id"] = player_id
    auction_state["time_left"] = duration
    auction_state["full_duration"] = duration   # stored so late-joiners know the 100% mark
    auction_state["timer_running"] = True
    auction_state["highest_bidder"] = None
    auction_state["highest_bid"] = int(player[6])   # cast Decimal→int immediately
    auction_state["sold_to"] = None

    socketio.emit("auction_started", {
        "player_id": player_id,
        "player_name": player[1],
        "ipl_team": player[2],        # renamed key to avoid clash with Flask 'team'
        "player_role": player[3],
        "strike_rate": float(player[4]),   # fix: Decimal → float for JSON
        "base_price": int(player[6]),      # fix: ensure int not Decimal
        "time_left": duration,
    }, room="auction_room", namespace="/")

    auction_timer_thread = threading.Thread(target=run_auction_timer, daemon=True)
    auction_timer_thread.start()

    return jsonify({"status": "started"})


@app.route("/admin/stop-auction", methods=["POST"])
def stop_auction():
    token = request.json.get("token", "")
    if active_tokens.get(token, {}).get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    auction_state["timer_running"] = False
    auction_state["active"] = False
    socketio.emit("auction_stopped", {}, room="auction_room", namespace="/")
    return jsonify({"status": "stopped"})


@app.route("/admin/reset-player", methods=["POST"])
def reset_player():
    token = request.json.get("token", "")
    if active_tokens.get(token, {}).get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    player_id = request.json.get("player_id")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET sold_to=NULL WHERE id=%s", (player_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "reset"})


# State sync — client calls this after joining to catch up if auction already running
@app.route("/auction-state")
def get_auction_state():
    if not auction_state["active"]:
        return jsonify({"active": False})
    pid = auction_state["current_player_id"]
    player = get_player(pid) if pid else None
    return jsonify({
        "active": True,
        "player_id": pid,
        "player_name": player[1] if player else "",
        "ipl_team": player[2] if player else "",
        "player_role": player[3] if player else "",
        "strike_rate": float(player[4]) if player else 0,
        "base_price": int(auction_state["highest_bid"]),
        "time_left": auction_state["time_left"],
        "highest_bidder": auction_state["highest_bidder"],
        "highest_bid": int(auction_state["highest_bid"]),
        "full_duration": auction_state["full_duration"],
    })


# ─── SOCKETIO ───────────────────────────────────────────────

@socketio.on("join")
def on_join(data):
    join_room("auction_room")
    emit("joined", {"status": "ok"})
    return {"status": "ok"}   # ← this is the ack returned to socket.emit("join", {}, callback)


@socketio.on("place_bid")
def on_bid(data):
    if not auction_state["active"] or auction_state["time_left"] == 0:
        emit("bid_error", {"message": "No active auction"})
        return

    # Read team from server-side token — never trust client-sent team name
    token = data.get("token", "")
    user = active_tokens.get(token, {})
    team = user.get("team")
    if not team or user.get("role") != "client":
        emit("bid_error", {"message": "Invalid session"})
        return

    bid_amount = int(data.get("bid_amount", 0))
    current_highest = auction_state["highest_bid"]

    if bid_amount <= current_highest:
        emit("bid_error", {"message": f"Bid must be higher than ₹{current_highest}L"})
        return

    if team not in TEAM_BUDGETS or TEAM_BUDGETS[team] < bid_amount:
        emit("bid_error", {"message": "Insufficient budget"})
        return

    if auction_state["time_left"] < 10:
        auction_state["time_left"] = 10

    auction_state["highest_bid"] = bid_amount
    auction_state["highest_bidder"] = team

    socketio.emit("new_bid", {
        "team": team,
        "bid_amount": bid_amount,
        "time_left": auction_state["time_left"],
    }, room="auction_room", namespace="/")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host="0.0.0.0", port=port)
