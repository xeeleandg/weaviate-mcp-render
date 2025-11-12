# serve.py
import os
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
    
    
def _mint_vertex_token_now() -> str:
    """
    Ritorna un access token OAuth Vertex fresco dalla Service Account.
    Usa GOOGLE_APPLICATION_CREDENTIALS (o ..._JSON scritto su disco).
    """
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    import os, json
    # Se hai GOOGLE_APPLICATION_CREDENTIALS_JSON, scrivilo su file (se non già fatto)
    gac_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    gac_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac_json and not gac_path:
        tmp = "/app/gcp_credentials.json"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(gac_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp
        gac_path = tmp
    if not gac_path:
        raise RuntimeError("Missing GOOGLE_APPLICATION_CREDENTIALS or GOOGLE_APPLICATION_CREDENTIALS_JSON")
    SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
    creds = service_account.Credentials.from_service_account_file(gac_path, scopes=SCOPES)
    creds.refresh(Request())
    if not creds.token:
        raise RuntimeError("Failed to mint Vertex OAuth token")
    return creds.token

def _build_vertex_headers() -> tuple[dict, dict]:
    """
    Costruisce gli header per REST (camel-case) e per gRPC (lowercase).
    Priorità:
      1) VERTEX_APIKEY (chiave statica) -> X-Goog-Vertex-Api-Key
      2) OAuth token sincrono -> X-Goog-Vertex-Api-Key = <token oauth 'nudo'>
    """
    import os
    rest = {}
    grpc = {}
    # A) API key statica
    vertex_api_key = os.environ.get("VERTEX_APIKEY")
    if vertex_api_key:
        for k in ["X-Goog-Vertex-Api-Key", "X-Goog-Api-Key", "X-Palm-Api-Key", "X-Goog-Studio-Api-Key"]:
            rest[k] = vertex_api_key
        for k in ["x-goog-vertex-api-key", "x-goog-api-key", "x-palm-api-key", "x-goog-studio-api-key"]:
            grpc[k] = vertex_api_key
        return rest, grpc
    # B) OAuth “nudo” come fai in Colab
    token = _mint_vertex_token_now()
    rest["X-Goog-Vertex-Api-Key"] = token
    grpc["x-goog-vertex-api-key"] = token
    # (opzionale ma utile): passa anche Authorization per compatibilità
    rest["Authorization"] = f"Bearer {token}"
    grpc["authorization"] = f"Bearer {token}"
    return rest, grpc



def _connect():
    # ⚠️ Usa lo stesso formato di Colab: in Colab passi SOLO l'host (senza https://).
    # Se nella tua env hai l’URL completo, puoi fare un normalizzatore semplice:
    url = _get_weaviate_url()
    url = url.replace("https://", "").replace("http://", "")  # allinea a Colab

    key = _get_weaviate_api_key()

    # Costruisci gli header “alla Colab”
    rest_headers, grpc_meta = _build_vertex_headers()

    # Client: headers per REST
    client = weaviate.connect_to_weaviate_cloud(
        cluster_url=url,
        auth_credentials=Auth.api_key(key),
        headers=rest_headers or None,
    )

    # Inietta metadata gRPC (chiavi minuscole)
    try:
        conn = getattr(client, "_connection", None)
        if conn is not None:
            if hasattr(conn, "grpc_metadata") and isinstance(conn.grpc_metadata, dict):
                conn.grpc_metadata.update(grpc_meta)
            elif hasattr(conn, "_grpc_metadata") and isinstance(conn._grpc_metadata, dict):
                conn._grpc_metadata.update(grpc_meta)
    except Exception as e:
        print("[weaviate] warn: cannot set gRPC metadata headers:", e)

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
    return_properties: Optional[List[str]] = None,
    include_metadata: bool = True,
) -> Dict[str, Any]:
    client = _connect()
    try:
        coll = client.collections.get(collection)
        if coll is None:
            return {"error": f"Collection '{collection}' non trovata"}
        kwargs = {"query": query, "alpha": alpha, "limit": limit}
        if query_properties:
            kwargs["query_properties"] = query_properties
        if return_properties:
            kwargs["return_properties"] = return_properties
        if include_metadata:
            kwargs["return_metadata"] = MetadataQuery(score=True, distance=True)
        resp = coll.query.hybrid(**kwargs)
        out = []
        for o in getattr(resp, "objects", []) or []:
            item = {"uuid": str(getattr(o, "uuid", "")),
                    "properties": getattr(o, "properties", {})}
            md = getattr(o, "metadata", None)
            if md:
                item["score"] = getattr(md, "score", None)
                item["distance"] = getattr(md, "distance", None)
            out.append(item)
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

def _write_adc_from_json_env():
    gac_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    gac_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac_json and not gac_path:
        tmp_path = "/app/gcp_credentials.json"
        with open(tmp_path, "w", encoding="utf-8") as f2:
            f2.write(gac_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp_path

def _refresh_vertex_oauth_loop():
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    import datetime, time
    SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
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
                "Authorization": f"Bearer {token}",
            }
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
    import threading
    t = threading.Thread(target=_refresh_vertex_oauth_loop, daemon=True)
    t.start()
    _VERTEX_REFRESH_THREAD_STARTED = True

_maybe_start_vertex_oauth_refresher()

# Patch _connect to inject headers
_old_connect = _connect
def _connect():
    url = _get_weaviate_url()
    key = _get_weaviate_api_key()
    headers = {}
    # Prefer static API key header if provided; else use OAuth refreshed headers
    vertex_key = os.environ.get("VERTEX_APIKEY")
    if vertex_key:
        headers["X-Goog-Vertex-Api-Key"] = vertex_key
    if not vertex_key and _VERTEX_HEADERS:
        headers.update(_VERTEX_HEADERS)
    client = weaviate.connect_to_weaviate_cloud(
        cluster_url=url,
        auth_credentials=Auth.api_key(key),
        headers=headers or None,
    )
    return client


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
        gac_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
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
