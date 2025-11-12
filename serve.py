# serve.py
import os
import json
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP
from starlette.responses import JSONResponse

# --- Weaviate client imports (v4) ---
import weaviate
from weaviate.classes.init import Auth
from weaviate.classes.query import MetadataQuery

# ---- GCP Project discovery from Service Account or ADC ----
def _discover_gcp_project() -> Optional[str]:
    # Priority: GOOGLE_APPLICATION_CREDENTIALS_JSON -> GOOGLE_APPLICATION_CREDENTIALS -> ADC default project
    gac_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if gac_json:
        try:
            data = json.loads(gac_json)
            if isinstance(data, dict) and data.get("project_id"):
                return data["project_id"]
        except Exception:
            pass
    gac_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac_path and os.path.exists(gac_path):
        try:
            with open(gac_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("project_id"):
                return data["project_id"]
        except Exception:
            pass
    try:
        import google.auth
        creds, proj = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if proj:
            return proj
    except Exception:
        pass
    return None


def _get_weaviate_url() -> str:
    url = os.environ.get("WEAVIATE_CLUSTER_URL") or os.environ.get("WEAVIATE_URL")
    if not url:
        raise RuntimeError("Please set WEAVIATE_URL or WEAVIATE_CLUSTER_URL.")
    return url


def _get_weaviate_api_key() -> str:
    api_key = os.environ.get("WEAVIATE_API_KEY")
    if not api_key:
        raise RuntimeError("Please set WEAVIATE_API_KEY.")
    return api_key


def _resolve_service_account_path() -> Optional[str]:
    """
    Determine and set GOOGLE_APPLICATION_CREDENTIALS if possible.
    Priority:
      1. Existing GOOGLE_APPLICATION_CREDENTIALS (if file exists)
      2. Explicit VERTEX_SA_PATH environment variable
      3. Default Render secret path /etc/secrets/weaviate-sa.json
    """
    gac_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac_path and os.path.exists(gac_path):
        return gac_path

    candidates = [
        os.environ.get("VERTEX_SA_PATH"),
        "/etc/secrets/weaviate-sa.json",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = candidate
            return candidate
    return None


def _connect():
    url = _get_weaviate_url()
    key = _get_weaviate_api_key()

    # ----- Costruisci headers (REST) -----
    headers = {}
    vertex_key = os.environ.get("VERTEX_APIKEY")

    # A) API key statica
    if vertex_key:
        for k in ["X-Goog-Vertex-Api-Key", "X-Goog-Api-Key", "X-Palm-Api-Key", "X-Goog-Studio-Api-Key"]:
            headers[k] = vertex_key
        # Allinea anche le variabili d'ambiente richieste lato Weaviate
        os.environ.setdefault("GOOGLE_APIKEY", vertex_key)
        os.environ.setdefault("PALM_APIKEY", vertex_key)

    # B) OAuth bearer dal refresher (se attivo): già include Authorization e X-Goog-Vertex-Api-Key
    if not vertex_key and "_VERTEX_HEADERS" in globals() and _VERTEX_HEADERS:
        headers.update(_VERTEX_HEADERS)
    elif not vertex_key:
        # prova un refresh sincrono se possibile
        if _sync_refresh_vertex_token():
            headers.update(_VERTEX_HEADERS)

    # ----- Crea client (headers per REST) -----
    client = weaviate.connect_to_weaviate_cloud(
        cluster_url=url,
        auth_credentials=Auth.api_key(key),
        headers=headers or None,
    )

    # ----- Inietta metadata gRPC (chiavi *minuscole*) -----
    # gRPC richiede lower-case ASCII per i metadata header
    grpc_meta = {}
    for k, v in (headers or {}).items():
        kk = k.lower()
        grpc_meta[kk] = v

    # Safety: assicurati che almeno una di queste chiavi sia presente in minuscolo
    if vertex_key:
        for kk in ["x-goog-vertex-api-key", "x-goog-api-key", "x-palm-api-key", "x-goog-studio-api-key"]:
            grpc_meta.setdefault(kk, vertex_key)
    else:
        # se stai usando OAuth, assicurati che 'authorization' sia presente
        if "authorization" not in grpc_meta and "_VERTEX_HEADERS" in globals() and _VERTEX_HEADERS:
            auth = _VERTEX_HEADERS.get("Authorization") or _VERTEX_HEADERS.get("authorization")
            if auth:
                grpc_meta["authorization"] = auth

    # Scrivi nei campi interni compatibili con le varie minor del client
    try:
        conn = getattr(client, "_connection", None)
        if conn is not None:
            if hasattr(conn, "grpc_metadata") and isinstance(conn.grpc_metadata, dict):
                conn.grpc_metadata.update(grpc_meta)
            elif hasattr(conn, "_grpc_metadata") and isinstance(conn._grpc_metadata, dict):
                conn._grpc_metadata.update(grpc_meta)
    except Exception as e:
        print("[weaviate] warning: cannot set gRPC metadata headers:", e)

    return client




mcp = FastMCP("weaviate-mcp-http")

@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok", "service": "weaviate-mcp-http"})


@mcp.tool
def get_config() -> Dict[str, Any]:
    return {
        "weaviate_url": os.environ.get("WEAVIATE_CLUSTER_URL") or os.environ.get("WEAVIATE_URL"),
        "weaviate_api_key_set": bool(os.environ.get("WEAVIATE_API_KEY")),
        "openai_api_key_set": bool(os.environ.get("OPENAI_API_KEY")),
        "cohere_api_key_set": bool(os.environ.get("COHERE_API_KEY")),
    }


@mcp.tool
def check_connection() -> Dict[str, Any]:
    client = _connect()
    try:
        ready = client.is_ready()
        return {"ready": bool(ready)}
    finally:
        client.close()


@mcp.tool
def list_collections() -> List[str]:
    client = _connect()
    try:
        colls = client.collections.list_all()
        if isinstance(colls, dict):
            names = list(colls.keys())
        else:
            try:
                names = [getattr(c, "name", str(c)) for c in colls]
            except Exception:
                names = list(colls)
        return sorted(set(names))
    finally:
        client.close()


@mcp.tool
def get_schema(collection: str) -> Dict[str, Any]:
    client = _connect()
    try:
        coll = client.collections.get(collection)
        if coll is None:
            return {"error": f"Collection '{collection}' not found"}
        try:
            cfg = coll.config.get()
        except Exception:
            try:
                cfg = coll.config.get_class()
            except Exception:
                cfg = {"info": "config API not available in this client version"}
        return {"collection": collection, "config": cfg}
    finally:
        client.close()


@mcp.tool
def keyword_search(collection: str, query: str, limit: int = 10) -> Dict[str, Any]:
    client = _connect()
    try:
        coll = client.collections.get(collection)
        if coll is None:
            return {"error": f"Collection '{collection}' not found"}
        resp = coll.query.bm25(
            query=query,
            return_metadata=MetadataQuery(score=True),
            limit=limit,
        )
        out = []
        for o in getattr(resp, "objects", []) or []:
            out.append(
                {
                    "uuid": str(getattr(o, "uuid", "")),
                    "properties": getattr(o, "properties", {}),
                    "bm25_score": getattr(getattr(o, "metadata", None), "score", None),
                }
            )
        return {"count": len(out), "results": out}
    finally:
        client.close()


@mcp.tool
def semantic_search(collection: str, query: str, limit: int = 10) -> Dict[str, Any]:
    client = _connect()
    try:
        coll = client.collections.get(collection)
        if coll is None:
            return {"error": f"Collection '{collection}' not found"}
        resp = coll.query.near_text(
            query=query,
            limit=limit,
            return_metadata=MetadataQuery(distance=True),
        )
        out = []
        for o in getattr(resp, "objects", []) or []:
            out.append(
                {
                    "uuid": str(getattr(o, "uuid", "")),
                    "properties": getattr(o, "properties", {}),
                    "distance": getattr(getattr(o, "metadata", None), "distance", None),
                }
            )
        return {"count": len(out), "results": out}
    finally:
        client.close()


@mcp.tool
def hybrid_search(
    collection: str,
    query: str,
    limit: int = 10,
    alpha: float = 0.5,
    query_properties: Optional[List[str]] = None,
) -> Dict[str, Any]:
    client = _connect()
    try:
        coll = client.collections.get(collection)
        if coll is None:
            return {"error": f"Collection '{collection}' not found"}
        kwargs = {"query": query, "alpha": alpha, "limit": limit}
        if query_properties:
            kwargs["query_properties"] = query_properties
        resp = coll.query.hybrid(**kwargs)
        out = []
        for o in getattr(resp, "objects", []) or []:
            md = getattr(o, "metadata", None)
            score = getattr(md, "score", None)
            distance = getattr(md, "distance", None)
            out.append(
                {
                    "uuid": str(getattr(o, "uuid", "")),
                    "properties": getattr(o, "properties", {}),
                    "bm25_score": score,
                    "distance": distance,
                }
            )
        return {"count": len(out), "results": out}
    finally:
        client.close()


# ---- Optional: Vertex AI Multimodal Embeddings (client-side) ----
try:
    from google.cloud import aiplatform
    _VERTEX_AVAILABLE = True
except Exception:
    _VERTEX_AVAILABLE = False

def _ensure_gcp_adc():
    gac_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    gac_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac_json and not gac_path:
        tmp_path = "/app/gcp_credentials.json"
        with open(tmp_path, "w", encoding="utf-8") as f2:
            f2.write(gac_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp_path
    _resolve_service_account_path()

def _vertex_embed(image_b64: Optional[str] = None, text: Optional[str] = None, model: str = "multimodalembedding@001"):
    if not _VERTEX_AVAILABLE:
        raise RuntimeError("google-cloud-aiplatform not installed")
    project = _discover_gcp_project()
    location = os.environ.get("VERTEX_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("Cannot determine GCP project_id from credentials; set GOOGLE_APPLICATION_CREDENTIALS(_JSON).")
    _ensure_gcp_adc()
    aiplatform.init(project=project, location=location)
    from vertexai.vision_models import MultiModalEmbeddingModel, Image
    mdl = MultiModalEmbeddingModel.from_pretrained(model)
    import base64
    image = Image.from_bytes(bytes() if not image_b64 else base64.b64decode(image_b64))
    resp = mdl.get_embeddings(image=image if image_b64 else None, contextual_text=text)
    if getattr(resp, "image_embedding", None):
        return list(resp.image_embedding)
    if getattr(resp, "text_embedding", None):
        return list(resp.text_embedding)
    if getattr(resp, "embedding", None):
        return list(resp.embedding)
    raise RuntimeError("No embedding returned from Vertex AI")

@mcp.tool
def insert_image_vertex(collection: str, image_b64: str, caption: Optional[str] = None, id: Optional[str] = None) -> Dict[str, Any]:
    vec = _vertex_embed(image_b64=image_b64, text=caption)
    client = _connect()
    try:
        coll = client.collections.get(collection)
        if coll is None:
            return {"error": f"Collection '{collection}' not found"}
        obj = coll.data.insert(
            properties={"caption": caption, "image_b64": image_b64},
            vectors={"image": vec}
        )
        return {"uuid": str(getattr(obj, "uuid", "")), "named_vector": "image"}
    finally:
        client.close()

@mcp.tool
def image_search_vertex(collection: str, image_b64: str, caption: Optional[str] = None, limit: int = 10) -> Dict[str, Any]:
    vec = _vertex_embed(image_b64=image_b64, text=caption)
    client = _connect()
    try:
        coll = client.collections.get(collection)
        if coll is None:
            return {"error": f"Collection '{collection}' not found"}
        resp = coll.query.near_vector(
            near_vector=vec,
            limit=limit,
            target_vector="image",
            return_metadata=MetadataQuery(distance=True),
        )
        out = []
        for o in getattr(resp, "objects", []) or []:
            out.append({
                "uuid": str(getattr(o, "uuid", "")),
                "properties": getattr(o, "properties", {}),
                "distance": getattr(getattr(o, "metadata", None), "distance", None),
            })
        return {"count": len(out), "results": out}
    finally:
        client.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    path = os.environ.get("MCP_PATH", "/mcp/")
    mcp.run(transport="http", host="0.0.0.0", port=port, path=path)


# ==== Vertex OAuth Token Refresher (optional) ====
_VERTEX_HEADERS = {}
_VERTEX_REFRESH_THREAD_STARTED = False

def _sync_refresh_vertex_token() -> bool:
    """
    Fallback per ottenere un token Vertex immediato nel thread chiamante,
    utile se la prima richiesta arriva prima che il refresher asincrono produca il token.
    """
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
    except Exception as exc:
        print(f"[vertex-oauth] sync refresh unavailable: {exc}")
        return False
    cred_path = _resolve_service_account_path()
    if not cred_path or not os.path.exists(cred_path):
        return False
    try:
        creds = service_account.Credentials.from_service_account_file(
            cred_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        creds.refresh(Request())
    except Exception as exc:
        print(f"[vertex-oauth] sync refresh error: {exc}")
        return False
    token = creds.token
    if not token:
        return False
    global _VERTEX_HEADERS
    _VERTEX_HEADERS = {
        "X-Goog-Vertex-Api-Key": token,
        "x-goog-vertex-api-key": token,
        "Authorization": f"Bearer {token}",
        "authorization": f"Bearer {token}",
    }
    os.environ["GOOGLE_APIKEY"] = token
    os.environ["PALM_APIKEY"] = token
    return True

def _write_adc_from_json_env():
    gac_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    gac_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac_json and not gac_path:
        tmp_path = "/app/gcp_credentials.json"
        with open(tmp_path, "w", encoding="utf-8") as f2:
            f2.write(gac_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp_path
    _resolve_service_account_path()

def _refresh_vertex_oauth_loop():
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    import datetime, time
    SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
    cred_path = _resolve_service_account_path()
    if not cred_path or not os.path.exists(cred_path):
        print("[vertex-oauth] GOOGLE_APPLICATION_CREDENTIALS missing; token refresher disabled")
        return
    creds = service_account.Credentials.from_service_account_file(cred_path, scopes=SCOPES)
    global _VERTEX_HEADERS
    while True:
        try:
            creds.refresh(Request())
            token = creds.token
            _VERTEX_HEADERS = {
                "X-Goog-Vertex-Api-Key": token,
                "x-goog-vertex-api-key": token,
                "Authorization": f"Bearer {token}",
                "authorization": f"Bearer {token}",
            }
            os.environ["GOOGLE_APIKEY"] = token
            os.environ["PALM_APIKEY"] = token
            print("[vertex-oauth] 🔄 Vertex token refreshed")
            sleep_s = 55 * 60
            if creds.expiry:
                now = datetime.datetime.utcnow().replace(tzinfo=creds.expiry.tzinfo)
                delta = (creds.expiry - now).total_seconds() - 300
                if delta > 300:
                    sleep_s = int(delta)
            time.sleep(sleep_s)
        except Exception as e:
            print(f"[vertex-oauth] refresh error: {e}")
            time.sleep(60)

def _maybe_start_vertex_oauth_refresher():
    global _VERTEX_REFRESH_THREAD_STARTED
    if _VERTEX_REFRESH_THREAD_STARTED:
        return
    if os.environ.get("VERTEX_USE_OAUTH", "").lower() not in ("1", "true", "yes"):
        return
    _write_adc_from_json_env()
    sa_path = _resolve_service_account_path()
    if not sa_path:
        print("[vertex-oauth] service account path not found; refresher not started")
        return
    import threading
    t = threading.Thread(target=_refresh_vertex_oauth_loop, daemon=True)
    t.start()
    _VERTEX_REFRESH_THREAD_STARTED = True

_maybe_start_vertex_oauth_refresher()

@mcp.tool
def diagnose_vertex() -> Dict[str, Any]:
    """
    Report Vertex auth status: project id, whether OAuth refresher is on, header presence, and token expiry sample.
    """
    info: Dict[str, Any] = {}
    info["project_id"] = _discover_gcp_project()
    info["oauth_enabled"] = os.environ.get("VERTEX_USE_OAUTH", "").lower() in ("1", "true", "yes")
    info["headers_active"] = bool(_VERTEX_HEADERS) if "_VERTEX_HEADERS" in globals() else False
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
        gac_path = _resolve_service_account_path()
        token_preview = None
        expiry = None
        if gac_path and os.path.exists(gac_path):
            creds = service_account.Credentials.from_service_account_file(gac_path, scopes=SCOPES)
            creds.refresh(Request())
            token_preview = (creds.token[:12] + "...") if creds.token else None
            expiry = getattr(creds, "expiry", None)
        info["token_sample"] = token_preview
        info["token_expiry"] = str(expiry) if expiry else None
    except Exception as e:
        info["token_error"] = str(e)
    return info
