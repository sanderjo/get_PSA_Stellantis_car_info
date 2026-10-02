# How to use get_PSA_info

`get_PSA_info.py` is a small python3 script that reads basic info about your PSA / Stellantis car
(battery, range, charging, odometer, temperature, location) from the PSA cloud. That's the same
data the MyPeugeot app shows.

It is set up for the **MyPeugeot app in the Netherlands**. For another brand or country, change the
constants at the top of the script (client ID/secret, realm, locale, redirect URI, token URL).

## Requirements

- python3 with the `requests` package (`pip install requests`)
- a working MyPeugeot app account with your car linked to it

## First run

1. Go to https://stelloauth.tollet.me/, choose MyPeugeot / NL, log in with your MyPeugeot account
   and copy the OAuth code.
2. Run the script **right away** with that code:
   ```
   ./get_PSA_info.py <OAUTH_CODE>
   ```
   The code expires within minutes and works only once. If you get `invalid_grant`, get a new code.

The script saves a login token in `psa_tokens.json`, next to the script.

## Later runs

```
./get_PSA_info.py          # normal output
./get_PSA_info.py --raw    # also print the full API response (JSON)
```

No new OAuth code is needed: the script logs in again with the saved token. You only need a new
code if that token stops working, for example after a password change.

**Keep `psa_tokens.json` private.** It gives access to your car account. It is in `.gitignore`.

## Battery kWh

The API reports the battery level as a percentage, not in kWh. The script calculates kWh as
percentage × usable capacity, which is 46 kWh by default (e-208 2019-2022). For the 2023+ e-208, use 51:

```
BATTERY_KWH=51 ./get_PSA_info.py
```

## How fresh is the data?

The car only sends a status report now and then: when it is switched on, plugged in, or while
charging, for example. When parked, it stays quiet to save its 12V battery. The script (and the app)
show the **last report**, not the live state. This means:

- Check the `updated` time and its age before trusting fields that change quickly, like
  `plugged in` and `charge state`. For example: unplugging the cable often isn't reported until
  the car wakes up again.
- The location has its own timestamp (`as of`). If it is very old, the car stopped sending its
  position; the MyPeugeot app will then show the same old position. Check the car's privacy setting
  ("Sharing data and vehicle position") or have a dealer check the connection module.

## Example output

```
$ ./get_PSA_info.py
VIN: VR3UHZKXZMT1234567
  updated:      2026-10-02 19:37:42 CEST (0h 1m ago)
  battery:      96.0 %  (~44.2 kWh of 46 kWh)
  range:        278 km
  plugged in:   False
  charge state: Disconnected  (0 km/h)
  odometer:     41329.9 km
  outside temp: 18.5 °C
  location:     52.00000, 4.30000  (as of 2026-04-10 11:43:44 CEST (175d 7h ago))
                https://www.openstreetmap.org/?mlat=52.00000&mlon=4.30000#map=17/52.00000/4.30000
```

## Credits

The OAuth client ID/secret and the API endpoints come from the Home Assistant
[stellantis_vehicles](https://github.com/andreadegiovine/homeassistant-stellantis-vehicles)
integration and from the psa_car_controller code in this repository.
