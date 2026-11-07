# Deploying the Dec 9 build on Lightsail (Morgan runs these; Claude never touches the box)

Three processes share one checkout, one venv and one SQLite file (`data/wimz.db`, WAL):

| unit | entry | port | nginx locations |
|---|---|---|---|
| `wimz-relay` (existing) | `python run.py` | 8000 | everything else |
| `wimz-mcp` | `python run_mcp.py` | 127.0.0.1:8001 | `/mcp`, `/authorize`, `/token`, `/register`, `/revoke`, `/.well-known/oauth-*` |
| `wimz-agent` | `python run_agent.py` | 127.0.0.1:8002 | `/agent/` |

## 1. Pull + deps

```bash
cd ~/wimzrelay && git pull
venv/bin/pip install -r requirements.txt     # adds mcp>=2.3.0, anthropic>=2.6.0
venv/bin/python -c "import app.main, app.mcp_server.server, app.voice_agent.app; print('ok')"
```

## 2. `.env` additions (values are yours; never committed)

```
INTERNAL_API_TOKEN=<openssl rand -hex 32>     # shared by relay, wimz-mcp, wimz-agent
PUBLIC_BASE_URL=https://api.wimzai.com
RELAY_BASE_URL=http://127.0.0.1:8000
AGENT_BASE_URL=http://127.0.0.1:8002
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...                            # Whisper STT
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...                       # the voice picked in the ElevenLabs dashboard (sets the accent)
```

Optional: `MCP_ALLOWED_HOSTS` (default `api.wimzai.com,api.wimzai.com:*,127.0.0.1:8001,localhost:8001`),
`AGENT_MODEL` (default `claude-opus-5`), `DEMO_DEVICE_ID` (stage robot id for the replayer).

## 3. systemd

```bash
sudo cp deploy/wimz-mcp.service deploy/wimz-agent.service /etc/systemd/system/
# edit User= / WorkingDirectory= / ExecStart= paths to match wimz-relay.service
sudo systemctl daemon-reload
sudo systemctl enable --now wimz-mcp wimz-agent
sudo systemctl restart wimz-relay
journalctl -u wimz-mcp -f        # expect "[MCP] issuer=https://api.wimzai.com ... internal_token=set"
journalctl -u wimz-agent -f      # expect "[AGENT] model=... stt=gpt-4o-transcribe tts=ok"
```

All three units must run as the same Unix user so the SQLite `-wal`/`-shm` files and
`data/agent_audio`, `data/sounds` stay writable.

## 4. nginx

```bash
sudo cp deploy/nginx-wimz-mcp-proxy.conf /etc/nginx/snippets/wimz-mcp-proxy.conf
# paste the location blocks from deploy/nginx-wimz-mcp.conf into the existing
# server { listen 443 ssl; server_name api.wimzai.com; ... } block, ABOVE the generic location /
sudo nginx -t && sudo systemctl reload nginx
```

## 5. External checks (from anywhere)

```bash
curl -s https://api.wimzai.com/.well-known/oauth-authorization-server | jq .registration_endpoint
curl -si https://api.wimzai.com/mcp | head -5          # 401 + WWW-Authenticate resource_metadata=...
curl -s https://api.wimzai.com/mcp/health
curl -s https://api.wimzai.com/agent/health
curl -s https://api.wimzai.com/health
```

Then add the connector in Claude (Settings → Connectors → Add custom connector →
`https://api.wimzai.com/mcp`), ChatGPT (Settings → Connectors → Advanced → Developer mode →
Create), Grok (Settings → Connectors) and Perplexity (Settings → Connectors). Watch
`journalctl -u wimz-mcp -f` for `[OAUTH] registered client ...`, the consent, then
`tools/list`. `sqlite3 data/wimz.db "select ts,actor,tool,outcome from mcp_audit order by id desc limit 20"`.

## Demo day runbook

```bash
# admin JWT: J=$(curl -s -X POST https://api.wimzai.com/api/auth/login -H 'content-type: application/json' -d '{"email":"morgan@wimzai.com","password":"..."}' | jq -r .token)
curl -s -X POST https://api.wimzai.com/admin/replay/load  -H "Authorization: Bearer $J" -H 'content-type: application/json' -d '{"seed":"demo_day.json","device_id":"<stage robot id>"}'
curl -s -X POST https://api.wimzai.com/admin/replay/start -H "Authorization: Bearer $J" -H 'content-type: application/json' -d '{"speed":60}'
curl -s -X POST https://api.wimzai.com/admin/replay/jump  -H "Authorization: Bearer $J" -H 'content-type: application/json' -d '{"to":"15:10"}'   # straight to the big episode
curl -s https://api.wimzai.com/admin/replay -H "Authorization: Bearer $J"
curl -s -X POST https://api.wimzai.com/admin/replay/reset -H "Authorization: Bearer $J"
```
