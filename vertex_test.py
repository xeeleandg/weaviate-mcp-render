import os
import json
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request

SA_PATH = os.environ.get("VERTEX_SA_PATH", "/etc/secrets/weaviate-sa.json")
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
PROJECT = os.environ.get("VERTEX_PROJECT_ID", "weaviate-sa")
LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")


def main() -> None:
    print(f"Using service account path: {SA_PATH}")
    if not os.path.exists(SA_PATH):
        raise FileNotFoundError(f"Service account file not found: {SA_PATH}")

    creds = service_account.Credentials.from_service_account_file(
        SA_PATH,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    token_preview = creds.token[:12] + "..."
    print(f"Access token obtained (preview): {token_preview}")

    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }

    url = (
        f"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/"
        f"{PROJECT}/locations/{LOCATION}/publishers/google/models/"
        "multimodalembedding@001:predict"
    )
    payload = {"instances": [{"text": "prova flange"}]}

    print(f"POST {url}")
    response = requests.post(url, headers=headers, json=payload, timeout=60)
    print("Status:", response.status_code)
    try:
        data = response.json()
        print(json.dumps(data, indent=2))
    except Exception:
        print(response.text)


if __name__ == "__main__":
    main()