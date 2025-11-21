# serve.py
import os
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP
from starlette.responses import JSONResponse

# --- Weaviate client imports (v4) ---
import weaviate
from weaviate.classes.init import Auth
from weaviate.classes.query import MetadataQuery

# In-memory stato Vertex
_VERTEX_HEADERS: Dict[str, str] = {}
_VERTEX_REFRESH_THREAD_STARTED = False
_VERTEX_USER_PROJECT: Optional[str] = None

# In-memory storage per immagini caricate (temporaneo, scade dopo 1 ora)
_UPLOADED_IMAGES: Dict[str, Dict[str, Any]] = {}

_BASE_DIR = Path(__file__).resolve().parent
_DEFAULT_PROMPT_PATH = _BASE_DIR / "prompts" / "instructions.md"
_DEFAULT_DESCRIPTION_PATH = _BASE_DIR / "prompts" / "description.txt"


def _build_vertex_header_map(token: str) -> Dict[str, str]:
    """
    Costruisce l'insieme minimo di header necessari per Vertex.
    Evitiamo alias multipli (X-Goog-Api-Key, X-Palm-Api-Key, ...), che possono
    essere interpretati come API key tradizionali e provocare errori.
    """
    headers: Dict[str, str] = {
        "X-Goog-Vertex-Api-Key": token,
    }
    if _VERTEX_USER_PROJECT:
        headers["X-Goog-User-Project"] = _VERTEX_USER_PROJECT
    return headers

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
        _load_vertex_user_project(gac_path)
        return gac_path

    candidates = [
        os.environ.get("VERTEX_SA_PATH"),
        "/etc/secrets/weaviate-sa.json",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = candidate
            _load_vertex_user_project(candidate)
            return candidate
    return None


def _load_vertex_user_project(path: str) -> None:
    global _VERTEX_USER_PROJECT
    if _VERTEX_USER_PROJECT:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _VERTEX_USER_PROJECT = data.get("project_id")
        if not _VERTEX_USER_PROJECT and data.get("quota_project_id"):
            _VERTEX_USER_PROJECT = data["quota_project_id"]
        if _VERTEX_USER_PROJECT:
            print(f"[vertex-oauth] detected service account project: {_VERTEX_USER_PROJECT}")
        else:
            print("[vertex-oauth] warning: project_id not found in service account JSON")
    except Exception as exc:
        print(f"[vertex-oauth] unable to read project id from SA: {exc}")


def _sync_refresh_vertex_token() -> bool:
    """
    Ottiene un token Vertex immediato usando le credenziali di servizio.
    Restituisce True se il token è stato aggiornato con successo.
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
    _VERTEX_HEADERS = _build_vertex_header_map(token)
    print(f"[vertex-oauth] sync token refresh (prefix: {token[:10]}...)")
    if os.environ.get("GOOGLE_APIKEY") == token:
        os.environ.pop("GOOGLE_APIKEY", None)
    if os.environ.get("PALM_APIKEY") == token:
        os.environ.pop("PALM_APIKEY", None)
    return True


def _connect():
    url = _get_weaviate_url()
    key = _get_weaviate_api_key()
    _resolve_service_account_path()  # garantisce il caricamento del project id se disponibile

    # ----- Costruisci headers (REST) -----
    headers = {}
    openai_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_APIKEY")
    if openai_key:
        headers["X-OpenAI-Api-Key"] = openai_key

    vertex_key = os.environ.get("VERTEX_APIKEY")
    vertex_bearer = os.environ.get("VERTEX_BEARER_TOKEN")

    # A) API key statica
    # Token bearer for REST/grpc (può essere passato via env se già ottenuto esternamente)
    if vertex_bearer:
        headers.update(_build_vertex_header_map(vertex_bearer))
        print("[vertex-oauth] using bearer token from VERTEX_BEARER_TOKEN env")

    if vertex_key and not headers:
        for k in ["X-Goog-Vertex-Api-Key", "X-Goog-Api-Key", "X-Palm-Api-Key", "X-Goog-Studio-Api-Key"]:
            headers[k] = vertex_key
        print("[vertex-oauth] using static Vertex API key from VERTEX_APIKEY")

    # B) OAuth bearer dal refresher (se attivo): già include Authorization e X-Goog-Vertex-Api-Key
    if not headers and "_VERTEX_HEADERS" in globals() and _VERTEX_HEADERS:
        headers.update(_VERTEX_HEADERS)
    elif not headers:
        # prova un refresh sincrono se possibile
        if _sync_refresh_vertex_token():
            headers.update(_VERTEX_HEADERS)
            # assicurati che nessuna env residuale confonda l'autenticazione
            token = _VERTEX_HEADERS.get("X-Goog-Vertex-Api-Key")
            if token:
                if os.environ.get("GOOGLE_APIKEY") == token:
                    os.environ.pop("GOOGLE_APIKEY", None)
                if os.environ.get("PALM_APIKEY") == token:
                    os.environ.pop("PALM_APIKEY", None)
        else:
            print("[vertex-oauth] unable to obtain Vertex token synchronously")

    # ----- Crea client (headers per REST) -----
    vertex_token = headers.get("X-Goog-Vertex-Api-Key")
    if vertex_token:
        token_preview = vertex_token[:10]
        project_debug = headers.get("X-Goog-User-Project")
        if project_debug:
            print(f"[vertex-oauth] using Vertex header token prefix: {token_preview}... project: {project_debug}")
        else:
            print(f"[vertex-oauth] using Vertex header token prefix: {token_preview}... (no x-goog-user-project)")
    elif headers:
        print("[vertex-oauth] custom headers configured (non-Vertex)")
    else:
        print("[vertex-oauth] WARNING: no Vertex headers available for connection")
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
        # Evita alias non necessari in gRPC: manteniamo solo x-goog-vertex-api-key e user-project
        if kk not in {"x-goog-vertex-api-key", "x-goog-user-project"}:
            continue
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
    
    # 2) OpenAI per text2vec-openai in Weaviate
    if openai_key:
        grpc_meta["x-openai-api-key"] = openai_key

    # Scrivi nei campi interni compatibili con le varie minor del client (forza assegnazione)
    try:
        conn = getattr(client, "_connection", None)
        if conn is not None:
            meta_list = list(grpc_meta.items())
            try:
                setattr(conn, "grpc_metadata", meta_list)
            except Exception:
                pass
            try:
                setattr(conn, "_grpc_metadata", meta_list)
            except Exception:
                pass
            # Metodo helper (se presente nelle ultime versioni)
            if hasattr(conn, "set_grpc_metadata"):
                try:
                    conn.set_grpc_metadata(meta_list)
                except Exception:
                    pass
            debug_meta = getattr(conn, "grpc_metadata", None)
            print(f"[vertex-oauth] grpc metadata now: {debug_meta}")
    except Exception as e:
        print("[weaviate] warning: cannot set gRPC metadata headers:", e)

    return client




def _load_text_source(env_keys, file_path):
    """
    Legge un testo da una lista di variabili d'ambiente o da un file.
    Priorità: file > prima variabile non vuota.
    """
    if isinstance(env_keys, str):
        env_keys = [env_keys]
    path = Path(file_path) if file_path else None
    if path and path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as exc:
            print(f"[mcp] warning: cannot read instructions file '{path}': {exc}")
    for key in env_keys:
        val = os.environ.get(key)
        if val:
            return val.strip()
    return None


_MCP_SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "weaviate-mcp-http")
_MCP_INSTRUCTIONS_FILE = os.environ.get("MCP_PROMPT_FILE") or os.environ.get("MCP_INSTRUCTIONS_FILE")
if not _MCP_INSTRUCTIONS_FILE and _DEFAULT_PROMPT_PATH.exists():
    _MCP_INSTRUCTIONS_FILE = str(_DEFAULT_PROMPT_PATH)
_MCP_DESCRIPTION_FILE = os.environ.get("MCP_DESCRIPTION_FILE")
if not _MCP_DESCRIPTION_FILE and _DEFAULT_DESCRIPTION_PATH.exists():
    _MCP_DESCRIPTION_FILE = str(_DEFAULT_DESCRIPTION_PATH)

_MCP_INSTRUCTIONS = _load_text_source(["MCP_PROMPT", "MCP_INSTRUCTIONS"], _MCP_INSTRUCTIONS_FILE)
_MCP_DESCRIPTION = _load_text_source("MCP_DESCRIPTION", _MCP_DESCRIPTION_FILE)

mcp = FastMCP(_MCP_SERVER_NAME)

def _apply_mcp_metadata():
    try:
        info = getattr(mcp, "server_info", None)
        if isinstance(info, dict):
            if _MCP_DESCRIPTION:
                info["description"] = _MCP_DESCRIPTION
            if _MCP_INSTRUCTIONS:
                info["instructions"] = _MCP_INSTRUCTIONS
        elif _MCP_DESCRIPTION or _MCP_INSTRUCTIONS:
            if _MCP_DESCRIPTION:
                setattr(mcp, "description", _MCP_DESCRIPTION)
            if _MCP_INSTRUCTIONS:
                setattr(mcp, "instructions", _MCP_INSTRUCTIONS)
    except Exception as _info_err:
        print("[mcp] warning: cannot set server info metadata:", _info_err)


_apply_mcp_metadata()

@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok", "service": "weaviate-mcp-http"})


@mcp.custom_route("/upload-image", methods=["POST"])
async def upload_image_endpoint(request):
    """
    Endpoint HTTP per upload diretto di immagini.
    Accetta multipart/form-data con campo 'image' o JSON con 'image_b64'.
    Restituisce image_id da usare in hybrid_search o image_search_vertex.
    """
    try:
        content_type = request.headers.get("content-type", "")
        image_b64 = None
        
        if "multipart/form-data" in content_type:
            # Upload multipart (file diretto)
            form = await request.form()
            if "image" not in form:
                return JSONResponse({"error": "Missing 'image' field in form data"}, status_code=400)
            
            file = form["image"]
            if hasattr(file, "read"):
                # File upload
                import base64
                file_bytes = await file.read()
                image_b64 = base64.b64encode(file_bytes).decode('utf-8')
            else:
                return JSONResponse({"error": "Invalid file upload"}, status_code=400)
        else:
            # JSON con base64
            try:
                data = await request.json()
                image_b64 = data.get("image_b64")
                if not image_b64:
                    return JSONResponse({"error": "Missing 'image_b64' in JSON body"}, status_code=400)
            except Exception:
                return JSONResponse({"error": "Invalid request format. Use multipart/form-data with 'image' field or JSON with 'image_b64'"}, status_code=400)
        
        if not image_b64:
            return JSONResponse({"error": "No image data provided"}, status_code=400)
        
        # Pulisci e valida il base64
        cleaned_b64 = _clean_base64(image_b64)
        if not cleaned_b64:
            return JSONResponse({"error": "Invalid base64 image string"}, status_code=400)
        
        # Genera un ID univoco
        image_id = str(uuid.uuid4())
        
        # Salva l'immagine con timestamp di scadenza (1 ora)
        _UPLOADED_IMAGES[image_id] = {
            "image_b64": cleaned_b64,
            "expires_at": time.time() + 3600,  # 1 ora
        }
        
        # Pulisci immagini scadute
        current_time = time.time()
        expired_ids = [img_id for img_id, data in _UPLOADED_IMAGES.items() if data["expires_at"] < current_time]
        for img_id in expired_ids:
            _UPLOADED_IMAGES.pop(img_id, None)
        
        return JSONResponse({"image_id": image_id, "expires_in": 3600})
    except Exception as e:
        print(f"[upload-image] error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.tool
def get_instructions() -> Dict[str, Any]:
    """
    Restituisce le istruzioni/prompt configurati per questo server MCP.
    """
    return {
        "instructions": _MCP_INSTRUCTIONS,
        "description": _MCP_DESCRIPTION,
        "server_name": _MCP_SERVER_NAME,
        "prompt_file": _MCP_INSTRUCTIONS_FILE,
        "description_file": _MCP_DESCRIPTION_FILE,
    }


@mcp.tool
def reload_instructions() -> Dict[str, Any]:
    """
    Ricarica descrizione/prompt da variabili d'ambiente o file associati.
    """
    global _MCP_INSTRUCTIONS, _MCP_DESCRIPTION, _MCP_INSTRUCTIONS_FILE, _MCP_DESCRIPTION_FILE
    _MCP_INSTRUCTIONS_FILE = os.environ.get("MCP_PROMPT_FILE") or os.environ.get("MCP_INSTRUCTIONS_FILE")
    if not _MCP_INSTRUCTIONS_FILE and _DEFAULT_PROMPT_PATH.exists():
        _MCP_INSTRUCTIONS_FILE = str(_DEFAULT_PROMPT_PATH)
    _MCP_DESCRIPTION_FILE = os.environ.get("MCP_DESCRIPTION_FILE")
    if not _MCP_DESCRIPTION_FILE and _DEFAULT_DESCRIPTION_PATH.exists():
        _MCP_DESCRIPTION_FILE = str(_DEFAULT_DESCRIPTION_PATH)
    _MCP_INSTRUCTIONS = _load_text_source(["MCP_PROMPT", "MCP_INSTRUCTIONS"], _MCP_INSTRUCTIONS_FILE)
    _MCP_DESCRIPTION = _load_text_source("MCP_DESCRIPTION", _MCP_DESCRIPTION_FILE)
    _apply_mcp_metadata()
    return get_instructions()


@mcp.tool
def get_config() -> Dict[str, Any]:
    return {
        "weaviate_url": os.environ.get("WEAVIATE_CLUSTER_URL") or os.environ.get("WEAVIATE_URL"),
        "weaviate_api_key_set": bool(os.environ.get("WEAVIATE_API_KEY")),
        "openai_api_key_set": bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_APIKEY")),
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
def upload_image(image_url: Optional[str] = None, image_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Carica un'immagine da URL o file path locale e restituisce un ID temporaneo da usare in hybrid_search o image_search_vertex.
    
    IMPORTANTE: 
    - Se hai un URL dell'immagine, passa image_url - il server scaricherà e convertirà automaticamente in base64.
    - Se hai un file path locale sul server, passa image_path - il server leggerà il file e lo convertirà in base64.
    
    L'immagine viene validata e pulita automaticamente. L'ID restituito è valido per 1 ora.
    La conversione in base64 viene gestita automaticamente dal server - non devi convertire manualmente.
    
    Esempi:
    - upload_image(image_url="https://example.com/image.jpg") -> {"image_id": "uuid-here"}
    - upload_image(image_path="/path/to/image.jpg") -> {"image_id": "uuid-here"}
    
    NOTA: Se hai un file sul client (non sul server), usa l'endpoint HTTP POST /upload-image con multipart/form-data.
    """
    global _UPLOADED_IMAGES
    
    cleaned_b64 = None
    
    if image_path:
        # Carica l'immagine da file path locale e converte in base64
        print(f"[upload_image] Loading image from path: {image_path}")
        try:
            import os
            if not os.path.exists(image_path):
                return {"error": f"File not found: {image_path}"}
            with open(image_path, "rb") as f:
                import base64
                file_bytes = f.read()
                image_b64_raw = base64.b64encode(file_bytes).decode('utf-8')
                cleaned_b64 = _clean_base64(image_b64_raw)
        except Exception as e:
            return {"error": f"Failed to load image from path {image_path}: {str(e)}"}
        if not cleaned_b64:
            return {"error": f"Invalid image file: {image_path}"}
    elif image_url:
        # Carica l'immagine dall'URL e converte in base64
        print(f"[upload_image] Loading image from URL: {image_url}")
        cleaned_b64 = _load_image_from_url(image_url)
        if not cleaned_b64:
            return {"error": f"Failed to load image from URL: {image_url}"}
    else:
        return {"error": "Either image_url or image_path must be provided"}
    
    # Genera un ID univoco
    image_id = str(uuid.uuid4())
    
    # Salva l'immagine con timestamp di scadenza (1 ora)
    _UPLOADED_IMAGES[image_id] = {
        "image_b64": cleaned_b64,
        "expires_at": time.time() + 3600,  # 1 ora
    }
    
    # Pulisci immagini scadute
    current_time = time.time()
    expired_ids = [img_id for img_id, data in _UPLOADED_IMAGES.items() if data["expires_at"] < current_time]
    for img_id in expired_ids:
        _UPLOADED_IMAGES.pop(img_id, None)
    
    return {"image_id": image_id, "expires_in": 3600}


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
    alpha: float = 0.8,
    query_properties: Optional[Any] = None,  # Accetta sia lista che stringa JSON
    image_id: Optional[str] = None,
    image_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Hybrid search che supporta sia testo che immagini.
    Se viene fornita image_id (da upload_image) o image_url, genera l'embedding e lo usa per la parte vettoriale.
    La conversione in base64 viene gestita automaticamente dal server.
    Preferisci image_id (più efficiente) > image_url.
    """
    # Forza l'uso della collection "Sinde" (come specificato nel prompt)
    if collection and collection != "Sinde":
        print(f"[hybrid_search] warning: collection '{collection}' requested, but using 'Sinde' as per instructions")
        collection = "Sinde"
    
    # Gestisci query_properties se arriva come stringa JSON invece di lista
    if query_properties and isinstance(query_properties, str):
        try:
            query_properties = json.loads(query_properties)
        except (json.JSONDecodeError, TypeError):
            pass  # Se non è JSON valido, ignora
    
    # Variabile interna per base64 (non esposta nello schema MCP)
    image_b64 = None
    
    # Recupera immagine da image_id se fornito (metodo preferito)
    if image_id:
        if image_id in _UPLOADED_IMAGES:
            img_data = _UPLOADED_IMAGES[image_id]
            if img_data["expires_at"] > time.time():
                image_b64 = img_data["image_b64"]
            else:
                _UPLOADED_IMAGES.pop(image_id, None)
                return {"error": f"Image ID {image_id} has expired. Please upload the image again."}
        else:
            return {"error": f"Image ID {image_id} not found. Please upload the image first using upload_image."}
    
    # Carica immagine da URL se fornita e converte in base64 internamente
    if image_url and not image_b64:
        image_b64 = _load_image_from_url(image_url)
        if not image_b64:
            return {"error": f"Failed to load image from URL: {image_url}"}
        # Pulisci e valida il base64 caricato da URL
        image_b64 = _clean_base64(image_b64)
        if not image_b64:
            return {"error": f"Invalid image format from URL: {image_url}"}
    
    client = _connect()
    try:
        coll = client.collections.get(collection)
        if coll is None:
            return {"error": f"Collection '{collection}' not found"}
        kwargs = {"alpha": alpha, "limit": limit}
        
        # Se c'è un'immagine, genera embedding e usa near_vector per la parte vettoriale
        if image_b64:
            vec = _vertex_embed(image_b64=image_b64, text=query if query else None)
            # Usa near_vector con multi_vector per la parte vettoriale
            kwargs["near_vector"] = vec
            kwargs["target_vector"] = "multi_vector"
            if query:
                kwargs["query"] = query  # BM25 parte testuale
        else:
            kwargs["query"] = query  # Solo testo
        
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
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        _load_vertex_user_project(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])

def _load_image_from_url(image_url: str) -> Optional[str]:
    """
    Carica un'immagine da URL pubblico e la converte in base64.
    Valida che sia un formato immagine supportato.
    """
    try:
        import requests
        import base64
        response = requests.get(image_url, timeout=30, stream=True)
        response.raise_for_status()
        
        # Verifica content-type
        content_type = response.headers.get('content-type', '').lower()
        if not content_type.startswith('image/'):
            print(f"[image] warning: URL {image_url} does not return an image (content-type: {content_type})")
            # Non fallire subito, potrebbe essere un'immagine comunque
        
        # Limita la dimensione a 10MB per evitare problemi
        content = response.content
        if len(content) > 10 * 1024 * 1024:
            print(f"[image] warning: image from {image_url} is too large ({len(content)} bytes)")
            return None
        
        # Verifica dimensione minima
        if len(content) < 100:
            print(f"[image] warning: image from {image_url} is too small ({len(content)} bytes)")
            return None
        
        # Verifica che sia un formato immagine valido (controlla magic bytes)
        valid_formats = {
            b'\xff\xd8\xff': 'JPEG',
            b'\x89PNG\r\n\x1a\n': 'PNG',
            b'GIF87a': 'GIF',
            b'GIF89a': 'GIF',
            b'RIFF': 'WEBP',  # WEBP inizia con RIFF
        }
        is_valid = False
        for magic, fmt in valid_formats.items():
            if content.startswith(magic):
                is_valid = True
                print(f"[image] detected format: {fmt} from {image_url}")
                break
        
        if not is_valid:
            print(f"[image] warning: {image_url} may not be a valid image format")
            # Non fallire, prova comunque
        
        return base64.b64encode(content).decode('utf-8')
    except Exception as e:
        print(f"[image] error loading from URL {image_url}: {e}")
        return None

def _clean_base64(image_b64: str) -> Optional[str]:
    """
    Pulisce e valida un base64 string.
    Rimuove eventuali prefissi data URL e verifica che sia valido.
    """
    import base64
    import re
    
    # Rimuovi eventuali prefissi data URL (data:image/...;base64,)
    if image_b64.startswith('data:'):
        match = re.match(r'data:image/[^;]+;base64,(.+)', image_b64)
        if match:
            image_b64 = match.group(1)
        else:
            return None
    
    # Rimuovi spazi bianchi
    image_b64 = image_b64.strip()
    
    # Valida che sia base64 valido
    try:
        # Verifica che contenga solo caratteri base64
        if not re.match(r'^[A-Za-z0-9+/=]+$', image_b64):
            print(f"[image] invalid base64 characters")
            return None
        
        # Prova a decodificare
        decoded = base64.b64decode(image_b64, validate=True)
        
        # Verifica che non sia vuoto
        if len(decoded) == 0:
            print(f"[image] empty image data")
            return None
        
        # Verifica dimensione minima (almeno un byte di header)
        if len(decoded) < 10:
            print(f"[image] image too small ({len(decoded)} bytes)")
            return None
        
        return image_b64
    except Exception as e:
        print(f"[image] base64 validation error: {e}")
        return None


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
    # Image accetta bytes nel costruttore
    image = None
    if image_b64:
        image_bytes = base64.b64decode(image_b64)
        image = Image(image_bytes)
    resp = mdl.get_embeddings(image=image, contextual_text=text)
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
def image_search_vertex(collection: str, image_id: Optional[str] = None, image_url: Optional[str] = None, caption: Optional[str] = None, limit: int = 10) -> Dict[str, Any]:
    """
    Ricerca vettoriale per immagini usando near_image() (come su Colab).
    Weaviate gestisce automaticamente l'embedding usando il multi2vec configurato.
    La conversione in base64 viene gestita automaticamente dal server.
    Preferisci image_id (da upload_image) > image_url.
    """
    # Forza l'uso della collection "Sinde" (come specificato nel prompt)
    if collection and collection != "Sinde":
        print(f"[image_search_vertex] warning: collection '{collection}' requested, but using 'Sinde' as per instructions")
        collection = "Sinde"
    
    # Variabile interna per base64 (non esposta nello schema MCP)
    image_b64 = None
    
    # Recupera immagine da image_id se fornito (metodo preferito)
    if image_id:
        if image_id in _UPLOADED_IMAGES:
            img_data = _UPLOADED_IMAGES[image_id]
            if img_data["expires_at"] > time.time():
                image_b64 = img_data["image_b64"]
            else:
                _UPLOADED_IMAGES.pop(image_id, None)
                return {"error": f"Image ID {image_id} has expired. Please upload the image again."}
        else:
            return {"error": f"Image ID {image_id} not found. Please upload the image first using upload_image."}
    
    # Carica immagine da URL se fornita e converte in base64 internamente
    if image_url and not image_b64:
        image_b64 = _load_image_from_url(image_url)
        if not image_b64:
            return {"error": f"Failed to load image from URL: {image_url}"}
        # Pulisci e valida il base64 caricato da URL
        image_b64 = _clean_base64(image_b64)
        if not image_b64:
            return {"error": f"Invalid image format from URL: {image_url}"}
    
    if not image_b64:
        return {"error": "Either image_id or image_url must be provided"}
    
    client = _connect()
    try:
        coll = client.collections.get(collection)
        if coll is None:
            return {"error": f"Collection '{collection}' not found"}
        # Usa near_image() come su Colab - Weaviate gestisce tutto internamente
        # Il primo parametro è posizionale (base64 string), non keyword
        resp = coll.query.near_image(
            image_b64,
            limit=limit,
            return_properties=["name", "source_pdf", "page_index", "mediaType"],
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    raw_path = os.environ.get("MCP_PATH", "/mcp")
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path
    path = raw_path.rstrip("/")
    if not path:
        path = "/"
    mcp.run(transport="http", host="0.0.0.0", port=port, path=path)


# ==== Vertex OAuth Token Refresher (optional) ====
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
            _VERTEX_HEADERS = _build_vertex_header_map(token)
            if os.environ.get("GOOGLE_APIKEY") == token:
                os.environ.pop("GOOGLE_APIKEY", None)
            if os.environ.get("PALM_APIKEY") == token:
                os.environ.pop("PALM_APIKEY", None)
            token_preview = token[:10] if token else None
            print(f"[vertex-oauth] 🔄 Vertex token refreshed (prefix: {token_preview}...)")
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

# --- Allinea gli endpoint MCP sia con slash che senza ---
try:
    from starlette.routing import Route

    _starlette_app = getattr(mcp, "app", None) or getattr(mcp, "_app", None)

    if _starlette_app is not None:
        async def _mcp_alias(request):
            scope = dict(request.scope)
            scope["path"] = "/mcp/"
            scope["raw_path"] = b"/mcp/"
            return await _starlette_app(scope, request.receive, request.send)

        _starlette_app.router.routes.insert(
            0,
            Route("/mcp", endpoint=_mcp_alias, methods=["GET", "HEAD", "POST", "OPTIONS"])
        )
except Exception as _route_err:
    print("[mcp] warning: cannot register MCP alias route:", _route_err)
