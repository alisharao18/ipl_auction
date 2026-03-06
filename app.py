from flask import Flask, render_template, request, redirect
import psycopg2
import os

app = Flask(__name__)

# Database connection
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_conn():
    return psycopg2.connect(DATABASE_URL)


# HOME PAGE
@app.route("/", methods=["GET", "POST"])
def home():

    role = None

    if request.method == "POST":
        role = request.form.get("role")

    conn = get_conn()
    cur = conn.cursor()

    # get all players
    cur.execute("SELECT id, name, team, role, strike_rate, base_price, auction_price FROM players ORDER BY id")
    players = cur.fetchall()

    # current live player (first player by default)
    current_player = None
    current_bid = 0

    if players:
        current_player = players[0][1]
        current_bid = players[0][5]

    conn.close()

    return render_template(
        "app.html",
        role=role,
        players=players,
        current_player=current_player,
        current_bid=current_bid
    )


# BIDDING ROUTE
@app.route("/bid", methods=["POST"])
def bid():

    player_id = request.form.get("player_id")
    bid_price = request.form.get("bid_price")

    if not player_id or not bid_price:
        return redirect("/")

    conn = get_conn()
    cur = conn.cursor()

    # get current price
    cur.execute(
        "SELECT auction_price FROM players WHERE id=%s",
        (player_id,)
    )

    result = cur.fetchone()

    if result:
        current_price = result[0]

        try:
            bid_price = int(bid_price)

            # allow bid only if higher
            if bid_price > current_price:
                cur.execute(
                    "UPDATE players SET auction_price=%s WHERE id=%s",
                    (bid_price, player_id)
                )
                conn.commit()

        except:
            pass

    conn.close()

    return redirect("/")


# RUN APP (needed for Render)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

