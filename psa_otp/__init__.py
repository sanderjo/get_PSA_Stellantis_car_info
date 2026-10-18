"""inWebo OTP client used to get the PSA remote-control (MQTT) token.

Copied unchanged (except cElementTree -> ElementTree, a local bord() in oaep.py,
and a fallback to the Cryptodome package name) from the otp/ folder of
https://github.com/andreadegiovine/homeassistant-stellantis-vehicles
(commit 199ae93, MIT license, see LICENSE in this folder), which took it from psa_car_controller.
"""
