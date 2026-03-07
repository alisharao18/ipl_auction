from flask import Flask, render_template, request, redirect
import psycopg2
import os

app = Flask(__name__)

# Database connection
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_conn():
    return psycopg2.connect(DATABASE_URL)


# HOME PAGE (LOGIN + SHOW PLAYERS)
@app.route("/", methods=["GET", "POST"])
def home():

    role = None
    players = []

    if request.method == "POST":
        role = request.form.get("role")

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM players ORDER BY id")
    players = cur.fetchall()

    conn.close()

    return render_template("app.html", role=role, players=players)


# BIDDING ROUTE
@app.route("/bid", methods=["POST"])
def bid():

    player_id = request.form.get("player_id")
    bid_price = request.form.get("bid_price")

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

        # allow bid only if higher
        if int(bid_price) > current_price:
            cur.execute(
                "UPDATE players SET auction_price=%s WHERE id=%s",
                (bid_price, player_id)
            )
            conn.commit()

    conn.close()

    return redirect("/")


# RUN APP (Render needs this)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
