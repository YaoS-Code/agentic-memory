# Docker → systemd Migration Guide

**Date:** 2026-04-02  
**System:** NexAgent / OpenClaw infrastructure (AMD Strix Halo, Ubuntu 24.04, 128GB RAM)

---

## Background

The original deployment ran all infrastructure services inside Docker containers managed by `docker-compose.yml`. While convenient for initial setup, this created several problems in production:

- **Port mapping overhead**: PostgreSQL exposed on `5434` (Docker) instead of native `5432`, Redis on `6380` instead of `6379`. Every service config required non-standard ports.
- **Startup dependency complexity**: `depends_on` in docker-compose is not reliable for readiness — services would start before dependencies were actually ready.
- **Resource overhead**: Docker daemon, container runtime, virtual network layers add latency and memory overhead on a system running concurrent LLM inference.
- **Storage bloat**: Docker images + volumes consumed ~2GB of disk space that could be freed.
- **Debugging friction**: Logs, connections, and process management all require Docker intermediaries instead of standard `systemctl` / `journalctl`.

Since the host machine is powerful and dedicated (not a shared cloud server), native services are strictly better.

---

## Architecture: Before vs After

| Component     | Before (Docker)                        | After (systemd)                        |
|---------------|----------------------------------------|----------------------------------------|
| PostgreSQL    | `docker run postgres:17`, port `5434`  | Native `postgresql@17-main`, port `5432` |
| pgvector      | Extension in Docker container          | Extension in native PG 17             |
| Redis         | `docker run redis:7`, port `6380`      | Native `redis-server`, port `6379`    |
| MinIO         | `docker run minio/minio`, port `9000`  | Native binary, `minio.service`        |
| Flask API     | `docker run oc-api`, port `18800`      | Native uvicorn, `oc-api.service`      |
| Discord bot   | `docker run oc-monitor`                | Native Python, `oc-monitor.service`   |
| Orchestration | `docker-compose up -d`                 | `systemctl start/enable <service>`    |
| Logs          | `docker logs <container>`              | `journalctl -u <service>`             |

---

## Migration Steps

### Step 1: Install PostgreSQL 17 + pgvector (native)

```bash
# Add PGDG apt repository
sudo sh -c 'echo "deb https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo apt-key add -
sudo apt update

# Install PostgreSQL 17
sudo apt install -y postgresql-17 postgresql-server-dev-17

# Install pgvector from source (requires pg17 dev headers)
git clone https://github.com/pgvector/pgvector.git /tmp/pgvector
cd /tmp/pgvector
make PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config
sudo make install PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config

# Start and enable PostgreSQL
sudo systemctl enable --now postgresql@17-main

# Create database and user
sudo -u postgres psql -c "CREATE USER ocuser WITH PASSWORD 'yourpassword';"
sudo -u postgres psql -c "CREATE DATABASE agentic_memory OWNER ocuser;"
sudo -u postgres psql -d agentic_memory -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

### Step 2: Migrate Data

```bash
# Dump from Docker PostgreSQL (14MB, 17 tables)
docker exec oc-postgres pg_dump -U ocuser -d agentic_memory > /tmp/agentic_memory_backup.sql

# Restore into native PostgreSQL
psql -h localhost -p 5432 -U ocuser -d agentic_memory < /tmp/agentic_memory_backup.sql

# Verify table count
psql -h localhost -p 5432 -U ocuser -d agentic_memory -c "\dt" | wc -l
# Expected: ~19 lines (17 tables + header/footer)
```

### Step 3: Create systemd Service Files

#### `/etc/systemd/system/oc-api.service`
```ini
[Unit]
Description=OpenClaw Memory API (uvicorn)
After=network.target postgresql@17-main.service redis.service
Requires=postgresql@17-main.service

[Service]
Type=simple
User=borui
WorkingDirectory=/path/to/memory-service
ExecStart=/usr/bin/python3 -m uvicorn api:app --host 0.0.0.0 --port 18800
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

#### `/etc/systemd/system/oc-monitor.service`
```ini
[Unit]
Description=OpenClaw Discord Monitor Bot
After=network.target oc-api.service
Requires=oc-api.service

[Service]
Type=simple
User=borui
WorkingDirectory=/path/to/monitor
ExecStart=/usr/bin/python3 bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

#### `/etc/systemd/system/minio.service`
```ini
[Unit]
Description=MinIO Object Storage
After=network.target

[Service]
Type=simple
User=borui
ExecStart=/usr/local/bin/minio server /data/minio --console-address :9001
Restart=always
RestartSec=5
Environment=MINIO_ROOT_USER=minioadmin
Environment=MINIO_ROOT_PASSWORD=minioadmin

[Install]
WantedBy=multi-user.target
```

Enable and start all services:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now oc-api.service
sudo systemctl enable --now oc-monitor.service
sudo systemctl enable --now minio.service
```

### Step 4: Update Connection Configs

6 locations required updating after port changes:

| File | Setting | Old Value | New Value |
|------|---------|-----------|-----------|
| `memory-service/config.py` | `PG_PORT` | `5434` | `5432` |
| `memory-service/config.py` | `REDIS_PORT` | `6380` | `6379` |
| `openclaw-plugin/memory-api/index.js` | DB connection port | `5434` | `5432` |
| `openclaw-plugin/memory-api/index.js` | Redis port | `6380` | `6379` |
| `config/openclaw.json` | memory service DB port | `5434` | `5432` |
| `config/openclaw.json` | memory service Redis port | `6380` | `6379` |

Also update `PG_HOST` from `localhost` with Docker network to `localhost` (same, but verify no Docker network alias was used).

### Step 5: Cutover — Stop Docker, Start Native

```bash
# 1. Stop Docker services
docker-compose down

# 2. Verify native services are running
systemctl status postgresql@17-main
systemctl status redis
systemctl status oc-api
systemctl status oc-monitor

# 3. Test API
curl -s http://localhost:18800/health
# Expected: {"status": "ok", ...}

# 4. Test vector search
curl -s -X POST http://localhost:18800/recall \
  -H "Content-Type: application/json" \
  -d '{"context": "test"}'
```

### Step 6: Clean Up Docker

After confirming all services work natively:

```bash
# Remove stopped containers
docker rm $(docker ps -aq)

# Remove images (reclaim ~2GB)
docker rmi $(docker images -q)

# Remove volumes
docker volume prune -f

# Optionally remove docker-compose stack definition
# (keep docker-compose.yml in git for reference)

# Verify space freed
df -h /
```

---

## Service Status Verification

```bash
# All services at once
systemctl status postgresql@17-main redis oc-api oc-monitor minio

# Check API health
curl -s http://localhost:18800/health | python3 -m json.tool

# Check DB connection + table count
psql -h localhost -U ocuser -d agentic_memory -c "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';"

# Check vector search works
curl -s -X POST http://localhost:18800/recall \
  -H "Content-Type: application/json" \
  -d '{"context": "memory system"}' | python3 -m json.tool

# View logs
journalctl -u oc-api -f
journalctl -u oc-monitor -f
```

---

## Port Change Summary

| Service    | Old Port (Docker) | New Port (Native) |
|------------|-------------------|-------------------|
| PostgreSQL | 5434              | 5432              |
| Redis      | 6380              | 6379              |
| MinIO API  | 9000              | 9000 (unchanged)  |
| MinIO UI   | 9001              | 9001 (unchanged)  |
| Memory API | 18800             | 18800 (unchanged) |

---

## Important Notes

### OpenClaw Upgrade Required Patch Re-application

After upgrading OpenClaw (3.28 → 3.31 → 4.1), the custom PostgreSQL task store patch (`task-store-pg.mjs`) must be **re-applied**. OpenClaw ships with SQLite for task storage; the patch replaces this with PostgreSQL to avoid SQLite file locking issues under concurrent load.

```bash
# After any OpenClaw upgrade:
cp /path/to/custom/task-store-pg.mjs /path/to/openclaw/node_modules/...
# Or use your patching script if you have one
```

### PostgreSQL Authentication

The native PostgreSQL installation uses `peer` auth by default for local connections. Update `/etc/postgresql/17/main/pg_hba.conf` to use `md5` or `scram-sha-256` for the app user:

```
# Change this line:
local   all   all   peer
# To:
local   all   all   scram-sha-256
```

Then `sudo systemctl reload postgresql@17-main`.

### Redis Configuration

Native Redis may have different defaults than the Docker image. Check `/etc/redis/redis.conf`:
- `bind 127.0.0.1` — correct for local-only access
- `maxmemory` — consider setting to a reasonable limit (e.g., `2gb`)
- `maxmemory-policy allkeys-lru` — recommended for cache workloads

### Rollback Plan

If migration fails, Docker containers can be restarted immediately:
```bash
docker-compose up -d
# Then revert the 6 config files to old port values
```

Keep the Docker Compose file and images available until native services have been running stably for at least 48 hours.

---

## Result

- ✅ All 5 services migrated to native systemd
- ✅ 17 tables, 14MB data migrated with zero loss
- ✅ ~2GB Docker storage freed
- ✅ Port numbers normalized to standard values
- ✅ `systemctl` / `journalctl` for management (no Docker intermediary)
- ✅ Faster startup, lower memory overhead
