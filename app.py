from flask import Flask, render_template, request
import psycopg2
import os

app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

@app.route("/", methods=["GET", "POST"])
def home():
    student = None

    if request.method == "POST":
        name = request.form["name"]

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT * FROM students WHERE name=%s", (name,))
        student = cur.fetchone()

        cur.close()
        conn.close()

    return render_template("app.html", student=student)

if __name__ == "__main__":
    app.run()