from flask import Flask, render_template, request, redirect, url_for, jsonify, session
import psycopg2
import os
import threading
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ipl-auction-secret-2024")

DATABASE_URL = os.environ.get("DATABASE_URL")

auction_state = {
    "active_player_id": None,
    "end_time": None,
    "current_bid": 0,
    "current_bidder": None,
    "status": "idle"
}
auction_lock = threading.Lock()

PLAYER_IMAGES = {
    "Virat Kohli": "https://assets.iplt20.com/ipl/IPLHeadshot2024/1.png",
    "Rohit Sharma": "https://assets.iplt20.com/ipl/IPLHeadshot2024/107.png",
    "Jasprit Bumrah": "https://assets.iplt20.com/ipl/IPLHeadshot2024/1124.png",
    "Hardik Pandya": "https://assets.iplt20.com/ipl/IPLHeadshot2024/2740.png",
    "KL Rahul": "https://assets.iplt20.com/ipl/IPLHeadshot2024/1478.png",
}

TEAM_COLORS = {
    "RCB": {"primary": "#EC1C24", "secondary": "#000000", "accent": "#FFD700"},
    "MI": {"primary": "#004BA0", "secondary": "#D1AB3E", "accent": "#FFFFFF"},
    "LSG": {"primary": "#A4C8E0", "secondary": "#004B8D", "accent": "#FFD700"},
    "CSK": {"primary": "#F9CD05", "secondary": "#0081E9", "accent": "#FFFFFF"},
    "KKR": {"primary": "#3A225D", "secondary": "#B3A123", "accent": "#FFFFFF"},
}
DEFAULT_TEAM = {"primary": "#1a1a2e", "secondary": "#e94560", "accent": "#FFD700"}


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


def build_player_dict(p):
    return {
        "id": p[0], "name": p[1], "team": p[2], "role": p[3],
        "strike_rate": p[4], "base_price": p[5], "auction_price": p[6],
        "image": PLAYER_IMAGES.get(p[1], ""),
        "team_color": TEAM_COLORS.get(p[2], DEFAULT_TEAM)
    }


@app.route("/")
def index():
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def login():
    role = request.form.get("role")
    name = request.form.get("name", "Guest")
    session["role"] = role
    session["name"] = name
    if role == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("client_dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/admin")
def admin_dashboard():
    if session.get("role") != "admin":
        return redirect(url_for("index"))
    players = [build_player_dict(p) for p in get_players()]
    return render_template("admin.html", players=players, auction_state=auction_state)


@app.route("/client")
def client_dashboard():
    if session.get("role") != "client":
        return redirect(url_for("index"))
    players = [build_player_dict(p) for p in get_players()]
    return render_template("client.html", players=players, auction_state=auction_state)


@app.route("/admin/start_auction/<int:player_id>", methods=["POST"])
def start_auction(player_id):
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    player = get_player(player_id)
    if not player:
        return jsonify({"error": "Player not found"}), 404
    with auction_lock:
        auction_state["active_player_id"] = player_id
        auction_state["end_time"] = (datetime.now() + timedelta(seconds=60)).isoformat()
        auction_state["current_bid"] = player[5]
        auction_state["current_bidder"] = None
        auction_state["status"] = "live"
    return jsonify({"success": True})


@app.route("/admin/end_auction", methods=["POST"])
def end_auction():
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    with auction_lock:
        if auction_state["status"] == "live" and auction_state["active_player_id"]:
            try:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("UPDATE players SET auction_price=%s WHERE id=%s",
                            (auction_state["current_bid"], auction_state["active_player_id"]))
                conn.commit()
                conn.close()
            except:
                pass
        auction_state["status"] = "ended"
    return jsonify({"success": True})


@app.route("/admin/reset_auction", methods=["POST"])
def reset_auction():
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    with auction_lock:
        auction_state.update({"active_player_id": None, "end_time": None,
                               "current_bid": 0, "current_bidder": None, "status": "idle"})
    return jsonify({"success": True})


@app.route("/bid", methods=["POST"])
def bid():
    if session.get("role") != "client":
        return jsonify({"error": "Unauthorized"}), 403
    data = request.get_json()
    bid_amount = int(data.get("bid_amount", 0))
    bidder_name = session.get("name", "Anonymous")
    with auction_lock:
        if auction_state["status"] != "live":
            return jsonify({"error": "No active auction"}), 400
        if auction_state["end_time"]:
            end_time = datetime.fromisoformat(auction_state["end_time"])
            if datetime.now() > end_time:
                auction_state["status"] = "ended"
                return jsonify({"error": "Auction has ended"}), 400
        if bid_amount <= auction_state["current_bid"]:
            return jsonify({"error": f"Bid must exceed current ₹{auction_state['current_bid']}L"}), 400
        auction_state["current_bid"] = bid_amount
        auction_state["current_bidder"] = bidder_name
        current_end = datetime.fromisoformat(auction_state["end_time"])
        new_end = max(current_end, datetime.now() + timedelta(seconds=10))
        auction_state["end_time"] = new_end.isoformat()
    return jsonify({"success": True, "new_bid": bid_amount, "bidder": bidder_name})


@app.route("/auction_status")
def auction_status():
    with auction_lock:
        state = dict(auction_state)
    seconds_remaining = 0
    if state["end_time"] and state["status"] == "live":
        end_time = datetime.fromisoformat(state["end_time"])
        remaining = (end_time - datetime.now()).total_seconds()
        seconds_remaining = max(0, int(remaining))
        if seconds_remaining == 0:
            with auction_lock:
                if auction_state["status"] == "live":
                    auction_state["status"] = "ended"
                    if auction_state["active_player_id"]:
                        try:
                            conn = get_conn()
                            cur = conn.cursor()
                            cur.execute("UPDATE players SET auction_price=%s WHERE id=%s",
                                        (auction_state["current_bid"], auction_state["active_player_id"]))
                            conn.commit()
                            conn.close()
                        except:
                            pass
            state["status"] = "ended"

    player_data = None
    if state["active_player_id"]:
        player = get_player(state["active_player_id"])
        if player:
            player_data = build_player_dict(player)

    return jsonify({
        "status": state["status"],
        "current_bid": state["current_bid"],
        "current_bidder": state["current_bidder"],
        "seconds_remaining": seconds_remaining,
        "player": player_data
    })


@app.route("/players_data")
def players_data():
    players = get_players()
    return jsonify([build_player_dict(p) for p in players])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
