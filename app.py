import json
import os

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

APP_ID = os.environ["META_APP_ID"]
APP_SECRET = os.environ["META_APP_SECRET"]
CONFIG_ID = os.environ["META_CONFIG_ID"]
GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v21.0")
VERIFY_TOKEN = os.environ["WEBHOOK_VERIFY_TOKEN"]

GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

app = Flask(__name__)

# In-memory store for the MVP. Replace with a DB before production.
CONNECTIONS = {}


@app.get("/")
def index():
    return render_template("index.html", app_id=APP_ID, config_id=CONFIG_ID, graph_version=GRAPH_VERSION)


@app.post("/api/exchange-token")
def exchange_token():
    """Frontend posts the short-lived `code` from the Embedded Signup popup.
    We swap it for a business-scoped access token and subscribe our app to the WABA."""
    body = request.get_json(force=True)
    code = body.get("code")
    redirect_uri = body.get("redirect_uri", "")
    waba_id = body.get("waba_id")
    phone_number_id = body.get("phone_number_id")

    print("EXCHANGE REQUEST:", json.dumps({
        "code_len": len(code) if code else 0,
        "redirect_uri": redirect_uri,
        "waba_id": waba_id,
        "phone_number_id": phone_number_id,
    }))

    if not code:
        return jsonify({"error": "missing code"}), 400

    token_res = requests.get(
        f"{GRAPH}/oauth/access_token",
        params={
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=15,
    )
    print("EXCHANGE RESPONSE STATUS:", token_res.status_code)
    print("EXCHANGE RESPONSE BODY:", token_res.text)
    if token_res.status_code != 200:
        return jsonify({"error": "token exchange failed", "detail": token_res.json()}), 400

    access_token = token_res.json()["access_token"]

    # Subscribe our app to receive webhooks for this WABA.
    if waba_id:
        sub = requests.post(
            f"{GRAPH}/{waba_id}/subscribed_apps",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        if sub.status_code >= 400:
            return jsonify({"error": "subscribe_apps failed", "detail": sub.json()}), 400

    CONNECTIONS[phone_number_id] = {
        "waba_id": waba_id,
        "access_token": access_token,
    }

    return jsonify({"ok": True, "phone_number_id": phone_number_id, "waba_id": waba_id})


@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "forbidden", 403


@app.post("/webhook")
def webhook_event():
    payload = request.get_json(force=True, silent=True) or {}
    print("INCOMING:", json.dumps(payload, indent=2))
    # TODO: route to your handler. payload["entry"][*]["changes"][*]["value"]["messages"] etc.
    return "ok", 200


@app.post("/api/send")
def send_message():
    """Quick test endpoint: { phone_number_id, to, text }"""
    body = request.get_json(force=True)
    pnid = body["phone_number_id"]
    conn = CONNECTIONS.get(pnid)
    if not conn:
        return jsonify({"error": "unknown phone_number_id"}), 404

    res = requests.post(
        f"{GRAPH}/{pnid}/messages",
        headers={"Authorization": f"Bearer {conn['access_token']}"},
        json={
            "messaging_product": "whatsapp",
            "to": body["to"],
            "type": "text",
            "text": {"body": body["text"]},
        },
        timeout=15,
    )
    return jsonify(res.json()), res.status_code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
