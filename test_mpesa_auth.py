
import os
import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

load_dotenv()

consumer_key = os.getenv("MPESA_CONSUMER_KEY")
consumer_secret = os.getenv("MPESA_CONSUMER_SECRET")

if not consumer_key or not consumer_secret:
    raise SystemExit(
        "Missing credentials. Check your .env file."
    )

url = (
    "https://sandbox.safaricom.co.ke"
    "/oauth/v1/generate?grant_type=client_credentials"
)

try:
    response = requests.get(
        url,
        auth=HTTPBasicAuth(consumer_key, consumer_secret),
        timeout=20,
    )

    print("HTTP status:", response.status_code)

    if response.ok:
        data = response.json()
        if data.get("access_token"):
            print("SUCCESS: Daraja authentication works.")
            print("Token type:", data.get("token_type", "Bearer"))
        else:
            print("Response did not contain an access token.")
            print(response.text)
    else:
        print("Authentication failed.")
        print(response.text)

except requests.RequestException as error:
    print("Connection error:", error)
