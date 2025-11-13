# Weaviate MCP Server (HTTP) — Render-ready

Server MCP HTTP (Streamable HTTP) per collegare **Weaviate Cloud** a client MCP remoti (es. Claude).

## Deploy rapido su Render

1. Crea un nuovo **Web Service** su Render da questo repo/cartella.
2. (Con `Dockerfile`) Render userà il container già pronto.
3. Imposta le variabili d'ambiente:
   - `WEAVIATE_URL` (oppure `WEAVIATE_CLUSTER_URL`)
   - `WEAVIATE_API_KEY`
   - (opz) `MCP_PATH` (default `/mcp/`)
4. Deploy.
5. Verifica: `GET https://<service>.onrender.com/health` → `{"status":"ok",...}`.

## Collegamento da Claude (Remote MCP)

Aggiungi un **Custom/Remote MCP server** con URL:
```
https://<service>.onrender.com/mcp/
```
e usa gli strumenti:
- `get_config`
- `check_connection`
- `list_collections`
- `get_schema(collection)`
- `keyword_search(collection, query, limit)`
- `semantic_search(collection, query, limit)`
- `hybrid_search(collection, query, alpha, limit, query_properties)`

## Note

- Per Weaviate Cloud bastano **URL + API key**.
- Il server ascolta su `0.0.0.0:$PORT` (compatibile Render).
- Health-check disponibile su `/health`.
- Puoi personalizzare nome/descrizione/prompt del server con:
  - `MCP_SERVER_NAME` (default `weaviate-mcp-http`)
  - `MCP_DESCRIPTION` per una descrizione breve
  - `MCP_PROMPT` / `MCP_INSTRUCTIONS` per un prompt testuale condiviso con il client
- In alternativa puoi versionare i messaggi in file e puntarli con:
  - `MCP_PROMPT_FILE` o `MCP_INSTRUCTIONS_FILE` (es. `prompts/instructions.md`)
  - `MCP_DESCRIPTION_FILE` (es. `prompts/description.txt`)
- Se non configuri nulla, il server carica automaticamente il prompt predefinito in `prompts/instructions.md`.
- Lo strumento `get_instructions` restituisce in ogni momento il prompt attivo.
- Usa `reload_instructions` per rileggere i file senza riavviare il server.

## Autenticazione Vertex AI

- Imposta `VERTEX_APIKEY` se vuoi usare una chiave statica (senza refresh).
- In alternativa puoi passare un bearer già ottenuto via OAuth impostando `VERTEX_BEARER_TOKEN`.
- Per OAuth con refresh automatico imposta `VERTEX_USE_OAUTH=true` e fornisci un **service account**:
  - `GOOGLE_APPLICATION_CREDENTIALS_JSON` con il JSON in chiaro **oppure**
  - `GOOGLE_APPLICATION_CREDENTIALS` con il path del file **oppure**
  - `VERTEX_SA_PATH` (default `/etc/secrets/weaviate-sa.json`, ideale su Render).
- Il token Vertex viene rigenerato ogni ~55 minuti e inserito sia negli header REST (`X-Goog-Vertex-Api-Key`, `Authorization`) sia nei metadata gRPC.
