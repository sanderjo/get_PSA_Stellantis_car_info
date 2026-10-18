#!/usr/bin/env python3
"""Start or stop charging of a Peugeot EV through the PSA/Stellantis remote-control service (MQTT).

Usage:   ./charging.py start     charge now
         ./charging.py stop      stop charging; the car waits for its delayed-charge time
                                 (nextDelayedTime in the status, e.g. PT22H = 22:00)

The first run asks for an SMS code (sent to the phone number of your MyPeugeot account) and your
MyPeugeot app PIN, to activate remote control. That is saved in psa_otp.pickle. Run it in a terminal.
Needs psa_tokens.json from get_PSA_info.py, and: pip install requests paho-mqtt pycryptodome
"""
import getpass
import json
import os
import pickle
import re
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt
import requests

from get_PSA_info import CLIENT_ID, LOCALE, REALM, TIMEOUT, api_get, get_access_token
from psa_otp.otp import Otp

BASE_URL = "https://api.groupe-psa.com"
CAR_ASSOCIATIONS_URL = BASE_URL + "/applications/cvs/v4/mauv/car-associations"
SMS_URL = BASE_URL + "/applications/cvs/v4/mobile/smsCode"
REMOTE_TOKEN_URL = BASE_URL + "/connectedcar/v4/virtualkey/remoteaccess/token"
INWEBO_ACCESS_ID = "bb8e981582b0f31353108fb020bead1c"

MQTT_SERVER = "mwa.mpsa.com"
MQTT_PORT = 8885
MQTT_REQ_TOPIC = "psa/RemoteServices/from/cid/"
MQTT_RESP_TOPIC = "psa/RemoteServices/to/cid/"
MQTT_REFRESH_TOKEN_DAYS = 3  # assumed lifetime of the MQTT refresh token (same as Home Assistant)
RESPONSE_TIMEOUT = 120  # seconds to wait for the car's final answer

# process_code / return_code of the MQTT responses
STILL_IN_PROGRESS = ("900", "901", "903")  # 901: car is asleep, its answer may come later
SUCCESS = ("0", "1")
CHARGING_ERRORS = {0: "discarded, car in standby", 1: "discarded, already charging",
                   2: "discarded, communication problem", 3: "discarded, car not stopped"}

HERE = os.path.dirname(os.path.abspath(__file__))
REMOTE_FILE = os.path.join(HERE, "psa_remote.json")  # customer id + MQTT tokens
OTP_FILE = os.path.join(HERE, "psa_otp.pickle")  # activated OTP device (secret)


def headers(access_token):
    return {"Authorization": "Bearer " + access_token, "x-introspect-realm": REALM,
            "Accept": "application/hal+json", "User-Agent": "okhttp/4.8.0"}


def api_call(method, url, access_token, extra_headers=None, **kwargs):
    res = requests.request(method, url, timeout=TIMEOUT, headers={**headers(access_token), **(extra_headers or {})},
                           params={"client_id": CLIENT_ID, "locale": LOCALE}, **kwargs)
    if not res.ok:
        sys.exit(f"{method} {url} failed ({res.status_code}): {res.text}")
    return res.json() if res.text else {}


def save_private(path, write):
    with open(path, "wb") as f:
        os.chmod(path, 0o600)
        write(f)


def load_remote():
    if not os.path.exists(REMOTE_FILE):
        return {}
    with open(REMOTE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_remote(remote):
    save_private(REMOTE_FILE, lambda f: f.write(json.dumps(remote, indent=2).encode()))


def activate_otp(access_token):
    print("Remote control is not activated yet for this script. Requesting an SMS code...", flush=True)
    api_call("POST", SMS_URL, access_token)
    print("SMS requested; it can take a minute to arrive.")
    sms_code = input("SMS code just sent to your phone: ").strip()
    pin = getpass.getpass("MyPeugeot app PIN: ").strip()
    otp = Otp(INWEBO_ACCESS_ID, device_id=access_token[:16])
    otp.smsCode = sms_code
    otp.codepin = pin
    otp.activation_start()
    result = otp.activation_finalyze()
    if result != Otp.OK:
        sys.exit(f"OTP activation failed: {result}")
    save_private(OTP_FILE, lambda f: pickle.dump(otp, f))
    print("Remote control activated.")
    return otp


def otp_code(access_token):
    """One-time password for a new MQTT token. Stellantis allows only about 6 per day."""
    if os.path.exists(OTP_FILE):
        with open(OTP_FILE, "rb") as f:
            otp = pickle.load(f)
    else:
        otp = activate_otp(access_token)
    code = otp.get_otp_code()
    save_private(OTP_FILE, lambda f: pickle.dump(otp, f))  # the OTP state changes on each code
    if not code:
        sys.exit(f"Could not get an OTP code. Delete {OTP_FILE} to activate again.")
    return code


def mqtt_access_token(access_token, remote):
    now = datetime.now(timezone.utc)
    mqtt_tokens = remote.get("mqtt", {})
    if mqtt_tokens.get("expires_at") and datetime.fromisoformat(mqtt_tokens["expires_at"]) > now + timedelta(minutes=1):
        return mqtt_tokens["access_token"]
    res = None
    if mqtt_tokens.get("refresh_expires_at") and datetime.fromisoformat(mqtt_tokens["refresh_expires_at"]) > now:
        res = requests.post(REMOTE_TOKEN_URL, timeout=TIMEOUT, headers=headers(access_token),
                            params={"client_id": CLIENT_ID, "locale": LOCALE},
                            json={"grant_type": "refresh_token", "refresh_token": mqtt_tokens["refresh_token"]})
    if res is None or not res.ok or "access_token" not in res.json():
        # The first OTP code right after activation is rejected with invalid_grant; the next one works
        for _ in range(2):
            res = requests.post(REMOTE_TOKEN_URL, timeout=TIMEOUT, headers=headers(access_token),
                                params={"client_id": CLIENT_ID, "locale": LOCALE},
                                json={"grant_type": "password", "password": otp_code(access_token)})
            if res.ok or "invalid_grant" not in res.text:
                break
        if not res.ok or "access_token" not in res.json():
            sys.exit(f"Getting the remote-control token failed ({res.status_code}): {res.text}")
        mqtt_tokens["refresh_expires_at"] = (now + timedelta(days=MQTT_REFRESH_TOKEN_DAYS)).isoformat()
    tokens = res.json()
    mqtt_tokens["access_token"] = tokens["access_token"]
    mqtt_tokens["expires_at"] = (now + timedelta(seconds=int(tokens["expires_in"]))).isoformat()
    if "refresh_token" in tokens:
        mqtt_tokens["refresh_token"] = tokens["refresh_token"]
    remote["mqtt"] = mqtt_tokens
    save_remote(remote)
    return mqtt_tokens["access_token"]


def delayed_time(status):
    """The car's delayed-charge start time, e.g. 'PT22H30M' -> (22, 30)."""
    for energy in status.get("energies", []):
        charging = energy.get("extension", {}).get("electric", {}).get("charging", {})
        match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", charging.get("nextDelayedTime") or "")
        if match:
            return int(match.group(1) or 0), int(match.group(2) or 0)
    sys.exit("No nextDelayedTime in the vehicle status, can't send a charge command.")


def send_command(customer_id, token, vin, service, parameters):
    """Publish one remote command and print the responses until the car gives a final answer."""
    now = datetime.now(timezone.utc)
    correlation_id = uuid.uuid4().hex + now.strftime("%Y%m%d%H%M%S%f")[:-3]
    message = json.dumps({"access_token": token, "customer_id": customer_id,
                          "correlation_id": correlation_id, "req_date": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "vin": vin, "req_parameters": parameters})
    done = threading.Event()
    result = {}

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            result["error"] = f"MQTT connection refused: {reason_code}"
            done.set()
            return
        client.subscribe(MQTT_RESP_TOPIC + customer_id + "/#")
        client.publish(MQTT_REQ_TOPIC + customer_id + service, message)
        print(f"Sent {service} {json.dumps(parameters)}, waiting for the car...")

    def on_message(client, userdata, msg):
        data = json.loads(msg.payload)
        if data.get("correlation_id") != correlation_id:
            return
        code = str(data.get("return_code", data.get("process_code", "")))
        print(f"  response {code}: {json.dumps({k: v for k, v in data.items() if k != 'correlation_id'})}")
        if code not in STILL_IN_PROGRESS:
            result["code"] = code
            result["data"] = data
            done.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set("IMA_OAUTH_ACCESS_TOKEN", token)
    client.tls_set()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_SERVER, MQTT_PORT, 120)
    client.loop_start()
    finished = done.wait(RESPONSE_TIMEOUT)
    client.disconnect()
    client.loop_stop()

    if "error" in result:
        sys.exit(result["error"])
    if not finished:
        sys.exit(f"No final answer within {RESPONSE_TIMEOUT} s. The car may be asleep; check the status later.")
    if result["code"] in SUCCESS:
        print("OK")
        return
    error = (result["data"].get("resp_data") or {}).get("error")
    sys.exit(f"Failed (code {result['code']}): {CHARGING_ERRORS.get(error, result['data'].get('reason', ''))}")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("start", "stop"):
        sys.exit(__doc__)
    charge_type = "immediate" if sys.argv[1] == "start" else "delayed"

    access_token = get_access_token()
    remote = load_remote()
    if "customer_id" not in remote:
        associations = api_call("GET", CAR_ASSOCIATIONS_URL, access_token, {"x-transaction-id": "1234"})
        if not isinstance(associations, list) or not associations:
            sys.exit(f"Could not get the customer id: {associations}")
        remote["customer_id"] = associations[0]["customer"]
        save_remote(remote)

    vehicles = api_get(access_token, "/vehicles")["_embedded"]["vehicles"]
    token = mqtt_access_token(access_token, remote)
    for vehicle in vehicles:
        status = api_get(access_token, f"/vehicles/{vehicle['id']}/status")
        hour, minute = delayed_time(status)
        print(f"VIN: {vehicle['vin']}  (delayed-charge time {hour:02d}:{minute:02d})")
        send_command(remote["customer_id"], token, vehicle["vin"], "/VehCharge",
                     {"program": {"hour": hour, "minute": minute}, "type": charge_type})


if __name__ == "__main__":
    main()
