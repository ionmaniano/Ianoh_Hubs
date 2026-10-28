
"""Register C2B callback URLs with Safaricom Daraja."""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

env = os.getenv("MPESA_ENV", "sandbox").lower()

if env == "production":
    base = "https://api.safaricom.co.ke"
    version = "v2"
else:
    base = "https://sandbox.safaricom.co.ke"
    version = "v1"

app_url = os.environ["APP_URL"].rstrip("/")
consumer_key = os.environ["MPESA_CONSUMER_KEY"]
consumer_secret = os.environ["MPESA_CONSUMER_SECRET"]
shortcode = os.environ["MPESA_SHORTCODE"]

# 1. Get an access token
token_response = requests.get(
    f"{base}/oauth/v1/generate?grant_type=client_credentials",
    auth=(consumer_key, consumer_secret),
    timeout=15,
)
token_response.raise_for_status()
token = token_response.json()["access_token"]

# 2. Register the callback URLs
payload = {
    "ShortCode": shortcode,
    "ResponseType": "Completed",
    "ConfirmationURL": f"{app_url}/hooks/c2b/confirmation",
    "ValidationURL": f"{app_url}/hooks/c2b/validation",
}

response = requests.post(
    f"{base}/mpesa/c2b/{version}/registerurl",
    headers={"Authorization": f"Bearer {token}"},
    json=payload,
    timeout=20,
)

print("HTTP status:", response.status_code)
print("Safaricom response:", response.text)
response.raise_for_status()
