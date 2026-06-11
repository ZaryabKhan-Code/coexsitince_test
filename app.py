import base64
import hashlib
import hmac
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


def _b64url_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def parse_signed_request(signed_request, app_secret):
    """Decode FB signed_request and verify HMAC. Returns payload dict or None."""
    try:
        encoded_sig, payload = signed_request.split(".", 1)
        sig = _b64url_decode(encoded_sig)
        data = json.loads(_b64url_decode(payload))
        expected = hmac.new(app_secret.encode(), payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            print("signed_request signature mismatch")
            return None
        return data
    except Exception as exc:
        print("signed_request parse error:", exc)
        return None


@app.get("/")
def index():
    return render_template("index.html", app_id=APP_ID, config_id=CONFIG_ID, graph_version=GRAPH_VERSION)


@app.post("/api/exchange-token")
def exchange_token():
    """Frontend posts the short-lived `code` from the Embedded Signup popup.
    We swap it for a business-scoped access token and subscribe our app to the WABA."""
    body = request.get_json(force=True)
    auth = body.get("authResponse") or {}
    code = body.get("code") or auth.get("code")
    signed_request = auth.get("signedRequest")
    fallback_token = auth.get("accessToken")
    waba_id = body.get("waba_id") or (body.get("signupData") or {}).get("waba_id")
    phone_number_id = body.get("phone_number_id") or (body.get("signupData") or {}).get("phone_number_id")

    # If FB didn't return a top-level code, dig it out of signed_request.
    if not code and signed_request:
        parsed = parse_signed_request(signed_request, APP_SECRET)
        if parsed:
            code = parsed.get("code")

    print("EXCHANGE INPUTS:", json.dumps({
        "has_code": bool(code),
        "has_signed_request": bool(signed_request),
        "has_fallback_token": bool(fallback_token),
        "waba_id": waba_id,
        "phone_number_id": phone_number_id,
    }))

    access_token = None

    if code:
        token_res = requests.get(
            f"{GRAPH}/oauth/access_token",
            params={
                "client_id": APP_ID,
                "client_secret": APP_SECRET,
                "code": code,
            },
            timeout=15,
        )
        print("EXCHANGE STATUS:", token_res.status_code, "BODY:", token_res.text)
        if token_res.status_code == 200:
            access_token = token_res.json().get("access_token")

    # Fallback: use the user access token FB.login returned directly.
    if not access_token and fallback_token:
        print("Using fallback accessToken from FB.login response")
        access_token = fallback_token

    if not access_token:
        return jsonify({"error": "could not obtain access token"}), 400

    # If frontend didn't capture waba_id, discover it from Graph API.
    if not waba_id:
        biz_res = requests.get(
            f"{GRAPH}/me/businesses",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "id,name,owned_whatsapp_business_accounts{id,name,phone_numbers{id,display_phone_number,verified_name}}"},
            timeout=15,
        )
        print("BUSINESSES LOOKUP:", biz_res.status_code, biz_res.text)
        try:
            for biz in biz_res.json().get("data", []):
                wabas = (biz.get("owned_whatsapp_business_accounts") or {}).get("data", [])
                if wabas:
                    waba_id = wabas[0]["id"]
                    phones = (wabas[0].get("phone_numbers") or {}).get("data", [])
                    if phones and not phone_number_id:
                        phone_number_id = phones[0]["id"]
                    break
        except Exception as exc:
            print("WABA discovery failed:", exc)

    if waba_id:
        sub = requests.post(
            f"{GRAPH}/{waba_id}/subscribed_apps",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        print("SUBSCRIBE STATUS:", sub.status_code, "BODY:", sub.text)

    if phone_number_id:
        CONNECTIONS[phone_number_id] = {
            "waba_id": waba_id,
            "access_token": access_token,
        }

    return jsonify({
        "ok": True,
        "phone_number_id": phone_number_id,
        "waba_id": waba_id,
        "token_preview": (access_token[:25] + "...") if access_token else None,
    })


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
