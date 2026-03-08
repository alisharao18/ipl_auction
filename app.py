from flask import Flask, render_template, request, redirect, session, url_for, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
import psycopg2
import os
import time

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ipl_secret_2024")
socketio = SocketIO(app, cors_allowed_origins="*")

DATABASE_URL = os.environ.get("DATABASE_URL")

# In-memory auction state
auction_state = {
    "active_player_id": None,
    "timer": 30,
    "timer_running": False,
    "current_bid": 0,
    "current_bidder": "",
    "bidders_in_room": {}
}

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

# ─── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        role = request.form.get("role")
        username = request.form.get("username", "").strip()
        if not username:
            username = "Guest"
        session["role"] = role
        session["username"] = username
        if role == "admin":
            return redirect("/admin")
        else:
            return redirect("/lobby")
    return render_template("login.html")

@app.route("/admin")
def admin():
    if session.get("role") != "admin":
        return redirect("/")
    players = get_players()
    active = get_player(auction_state["active_player_id"]) if auction_state["active_player_id"] else None
    return render_template("admin.html", players=players, active_player=active, state=auction_state)

@app.route("/lobby")
def lobby():
    if session.get("role") != "client":
        return redirect("/")
    players = get_players()
    active = get_player(auction_state["active_player_id"]) if auction_state["active_player_id"] else None
    return render_template("lobby.html", players=players, active_player=active,
                           state=auction_state, username=session.get("username"))

@app.route("/admin/set_player", methods=["POST"])
def set_player():
    if session.get("role") != "admin":
        return redirect("/")
    player_id = int(request.form.get("player_id"))
    player = get_player(player_id)
    auction_state["active_player_id"] = player_id
    auction_state["current_bid"] = player[5]  # base_price
    auction_state["current_bidder"] = ""
    auction_state["timer"] = 30
    auction_state["timer_running"] = True

    socketio.emit("auction_update", {
        "player_id": player_id,
        "player_name": player[1],
        "team": player[2],
        "role": player[3],
        "strike_rate": player[4],
        "base_price": player[5],
        "current_bid": auction_state["current_bid"],
        "current_bidder": "",
        "timer": 30,
        "timer_running": True
    }, room="auction")
    return redirect("/admin")

@app.route("/admin/sell_player", methods=["POST"])
def sell_player():
    if session.get("role") != "admin":
        return redirect("/")
    player_id = auction_state["active_player_id"]
    if not player_id:
        return redirect("/admin")
    sold_to = auction_state["current_bidder"] or "Unsold"
    final_price = auction_state["current_bid"]
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET auction_price=%s, sold_to=%s WHERE id=%s",
                (final_price, sold_to, player_id))
    conn.commit()
    conn.close()
    player = get_player(player_id)
    auction_state["timer_running"] = False
    auction_state["active_player_id"] = None

    socketio.emit("player_sold", {
        "player_name": player[1],
        "sold_to": sold_to,
        "final_price": final_price
    }, room="auction")
    return redirect("/admin")

@app.route("/admin/reset_player", methods=["POST"])
def reset_player():
    if session.get("role") != "admin":
        return redirect("/")
    player_id = int(request.form.get("player_id"))
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE players SET auction_price=0, sold_to=NULL WHERE id=%s", (player_id,))
    conn.commit()
    conn.close()
    return redirect("/admin")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ─── SOCKETIO EVENTS ──────────────────────────────────────────────────────────

@socketio.on("join_auction")
def handle_join(data):
    username = data.get("username", "Anonymous")
    join_room("auction")
    if username not in auction_state["bidders_in_room"]:
        auction_state["bidders_in_room"][username] = True
    emit("room_update", {
        "bidders": list(auction_state["bidders_in_room"].keys()),
        "count": len(auction_state["bidders_in_room"])
    }, room="auction")

    # Send current state to newly joined user
    if auction_state["active_player_id"]:
        player = get_player(auction_state["active_player_id"])
        emit("auction_update", {
            "player_id": player[0],
            "player_name": player[1],
            "team": player[2],
            "role": player[3],
            "strike_rate": player[4],
            "base_price": player[5],
            "current_bid": auction_state["current_bid"],
            "current_bidder": auction_state["current_bidder"],
            "timer": auction_state["timer"],
            "timer_running": auction_state["timer_running"]
        })

@socketio.on("disconnect")
def handle_disconnect():
    pass

@socketio.on("place_bid")
def handle_bid(data):
    username = data.get("username")
    bid_amount = int(data.get("bid_amount", 0))

    if not auction_state["active_player_id"]:
        emit("bid_error", {"message": "No active auction"})
        return
    if bid_amount <= auction_state["current_bid"]:
        emit("bid_error", {"message": f"Bid must be higher than ₹{auction_state['current_bid']}L"})
        return

    auction_state["current_bid"] = bid_amount
    auction_state["current_bidder"] = username
    auction_state["timer"] = min(auction_state["timer"] + 5, 30)  # reset timer on new bid

    emit("bid_placed", {
        "username": username,
        "bid_amount": bid_amount,
        "timer": auction_state["timer"]
    }, room="auction")

@socketio.on("timer_tick")
def handle_timer_tick(data):
    if session.get("role") != "admin":
        return
    if auction_state["timer_running"] and auction_state["timer"] > 0:
        auction_state["timer"] -= 1
        emit("timer_update", {"timer": auction_state["timer"]}, room="auction")
        if auction_state["timer"] == 0:
            auction_state["timer_running"] = False
            emit("timer_ended", {
                "current_bidder": auction_state["current_bidder"],
                "current_bid": auction_state["current_bid"]
            }, room="auction")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host="0.0.0.0", port=port)
