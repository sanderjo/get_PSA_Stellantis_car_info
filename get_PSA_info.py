#!/usr/bin/env python3
"""Minimal script to read battery info of a Peugeot EV from the PSA/Stellantis API.

First run:   python3 get_PSA_info.py <OAUTH_CODE>
             (get a fresh code at https://stelloauth.tollet.me/ - it expires within minutes
              and can be used only once)
Later runs:  python3 get_PSA_info.py
             (uses the refresh token saved in psa_tokens.json)
"""
import base64
import csv
import json
import os
import sys
from datetime import datetime, timezone

import requests

# MyPeugeot app, Netherlands (same values as the Home Assistant stellantis_vehicles integration)
CLIENT_ID = "1eebc2d5-5df3-459b-a624-20abfcf82530"
CLIENT_SECRET = "T5tP7iS0cO8sC0lA2iE2aR7gK6uE5rF3lJ8pC3nO1pR7tL8vU1"
REALM = "clientsB2CPeugeot"
LOCALE = "nl-NL"
REDIRECT_URI = "mymap://oauth2redirect/nl"
TOKEN_URL = "https://idpcvs.peugeot.com/am/oauth2/access_token"
API_URL = "https://api.groupe-psa.com/connectedcar/v4/user"

# Usable battery capacity in kWh: 46 for the 2019-2022 e-208 (50 kWh gross), 51 for the 2023+ model
BATTERY_KWH = float(os.environ.get("BATTERY_KWH", 46))

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "psa_tokens.json")
TIMEOUT = 30

# Every run adds one row per car to VIN<vin>.csv; timestamps are UTC as reported by the API
CSV_FIELDS = ["retrieved", "updated", "battery_pct", "battery_kwh", "range_km", "plugged", "charge_state",
              "charging_rate_kmh", "odometer_km", "outside_temp_c", "latitude", "longitude", "position_updated"]


def request_token(params):
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    res = requests.post(TOKEN_URL, params=params, timeout=TIMEOUT,
                        headers={"Authorization": "Basic " + basic,
                                 "Content-Type": "application/x-www-form-urlencoded"})
    if res.status_code != 200:
        sys.exit(f"Token request failed ({res.status_code}): {res.text}")
    tokens = res.json()
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    os.chmod(TOKEN_FILE, 0o600)
    return tokens["access_token"]


def get_access_token(code=None):
    if code:
        return request_token({"grant_type": "authorization_code", "code": code,
                              "redirect_uri": REDIRECT_URI})
    if not os.path.exists(TOKEN_FILE):
        sys.exit(__doc__)
    with open(TOKEN_FILE, encoding="utf-8") as f:
        refresh_token = json.load(f)["refresh_token"]
    return request_token({"grant_type": "refresh_token", "refresh_token": refresh_token})


def api_get(access_token, path):
    res = requests.get(API_URL + path, timeout=TIMEOUT,
                       params={"client_id": CLIENT_ID, "locale": LOCALE},
                       headers={"Authorization": "Bearer " + access_token,
                                "x-introspect-realm": REALM,
                                "Accept": "application/hal+json"})
    if res.status_code != 200:
        sys.exit(f"GET {path} failed ({res.status_code}): {res.text}")
    return res.json()


def local_time(timestamp):
    if not timestamp:
        return None
    utc = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    minutes = int((datetime.now(timezone.utc) - utc).total_seconds() // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    age = f"{days}d {hours}h ago" if days else f"{hours}h {minutes}m ago"
    return f"{utc.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')} ({age})"


def append_csv(vin, row):
    csv_file = os.path.join(HERE, f"VIN{vin}.csv")
    new_file = not os.path.exists(csv_file)
    with open(csv_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def main():
    codes = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    access_token = get_access_token(codes[0] if codes else None)
    vehicles = api_get(access_token, "/vehicles")["_embedded"]["vehicles"]
    for vehicle in vehicles:
        status = api_get(access_token, f"/vehicles/{vehicle['id']}/status")
        print(f"VIN: {vehicle['vin']}")
        row = {"retrieved": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        for energy in status.get("energies", []):
            if energy.get("type") != "Electric":
                continue
            level = energy.get("level")
            charging = energy.get("extension", {}).get("electric", {}).get("charging", {})
            print(f"  updated:      {local_time(energy.get('createdAt'))}")
            print(f"  battery:      {level} %  (~{level * BATTERY_KWH / 100:.1f} kWh of {BATTERY_KWH:g} kWh)")
            print(f"  range:        {energy.get('autonomy')} km")
            print(f"  plugged in:   {charging.get('plugged')}")
            print(f"  charge state: {charging.get('status')}  ({charging.get('chargingRate')} km/h)")
            row.update(updated=energy.get("createdAt"), battery_pct=level, battery_kwh=round(level * BATTERY_KWH / 100, 1),
                       range_km=energy.get("autonomy"), plugged=charging.get("plugged"),
                       charge_state=charging.get("status"), charging_rate_kmh=charging.get("chargingRate"))
        row["odometer_km"] = status.get("odometer", {}).get("mileage")
        row["outside_temp_c"] = status.get("environment", {}).get("air", {}).get("temp")
        print(f"  odometer:     {row['odometer_km']} km")
        print(f"  outside temp: {row['outside_temp_c']} °C")
        position = status.get("lastPosition", {})
        coordinates = position.get("geometry", {}).get("coordinates")
        if coordinates:
            lon, lat = coordinates[:2]  # GeoJSON order is longitude, latitude
            print(f"  location:     {lat}, {lon}  (as of {local_time(position.get('properties', {}).get('createdAt'))})")
            print(f"                https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=17/{lat}/{lon}")
            row.update(latitude=lat, longitude=lon, position_updated=position.get("properties", {}).get("createdAt"))
        append_csv(vehicle["vin"], row)
        if "--raw" in sys.argv:
            print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()
