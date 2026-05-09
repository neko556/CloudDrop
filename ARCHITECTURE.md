# CloudDrop Architecture

CloudDrop is a secure, ephemeral file-sharing service built with a focus on privacy, limited-time access, and end-to-end security. The system uses a **3-Tier Architecture** on AWS to separate concerns, enforce defense-in-depth network isolation, and provide high availability across multiple Availability Zones.

---

## Three-Tier Architecture Overview

```
                           ┌─────────────────────────────────────────────────────────────┐
                           │                         VPC (10.0.0.0/16)                   │
                           │                                                             │
Users ──► Internet ──►  ┌──┴──────────── WEB TIER (Public Subnets) ──────────────────┐   │
          Gateway    │  │   Internet-Facing ALB                                      │   │
                     │  │     ├── AZ1: Nginx Web Server (10.0.0.0/24)                │   │
                     │  │     └── AZ2: Nginx Web Server (10.0.1.0/24)                │   │
                     │  └────────────────────────┬───────────────────────────────────┘   │
                     │                           │                                       │
                     │                   Internal ALB                                    │
                     │                           │                                       │
                     │  ┌────────── APPLICATION TIER (Private Subnets) ─────────────┐   │
                     │  │     ├── AZ1: FastAPI App Server (10.0.2.0/24)             │   │
                     │  │     └── AZ2: FastAPI App Server (10.0.3.0/24)             │   │
                     │  │                  │               │                         │   │
                     │  │          ┌───────┘               └──────────┐              │   │
                     │  └──────────┼──────────────────────────────────┼──────────────┘   │
                     │             │                                  │                   │
                     │  ┌──────── DATABASE TIER (Private Subnets) ───┼──────────────┐   │
                     │  │         ▼                                  ▼              │   │
                     │  │   Redis Cache                   PostgreSQL Primary        │   │
                     │  │   (ElastiCache)                  (AZ1: 10.0.4.0/24)      │   │
                     │  │   Short-Link ──► File Mapping          │                  │   │
                     │  │                                   replication             │   │
                     │  │                                        │                  │   │
                     │  │                                PostgreSQL Replica         │   │
                     │  │                                 (AZ2: 10.0.5.0/24)       │   │
                     │  └──────────────────────────────────────────────────────────┘   │
                     │                                                                   │
                     └───────────────────────────────────────────────────────────────────┘

              STORAGE LAYER (outside VPC):
              ├── Amazon S3 — Encrypted file blobs, 24hr auto-delete lifecycle
              ├── AWS KMS — Envelope encryption key management
              ├── CloudFront CDN — HTTPS termination, edge caching, pre-signed URLs
              └── Amazon Cognito — User identity & authentication
```

### Tier 1 — Web Tier (Public Subnets)

| Component | Details |
|-----------|---------|
| **Internet-Facing ALB** | Receives all inbound HTTP/HTTPS traffic from the internet. Distributes requests across Nginx servers in 2 AZs. |
| **Nginx Web Servers** | Lightweight reverse proxies deployed in public subnets (one per AZ). They terminate client connections, serve static assets, and forward dynamic requests to the Internal ALB. |
| **Why Nginx?** | Provides an additional security layer between the public internet and the application logic. Handles SSL offloading, request buffering, rate limiting at the edge, and static file serving — all before any request touches the app tier. |

### Tier 2 — Application Tier (Private Subnets)

| Component | Details |
|-----------|---------|
| **Internal ALB** | A private, non-internet-facing load balancer that distributes traffic from the Web Tier to the App Servers. Cannot be reached directly from the internet. |
| **FastAPI App Servers** | The core application logic — handles uploads, downloads, encryption/decryption, authentication, and business rules. Deployed in private subnets with no public IP; outbound internet access is via NAT Gateway. |
| **Why private?** | The app servers contain sensitive logic (KMS calls, database credentials, encryption keys in memory). Placing them in private subnets means they are **unreachable from the internet** — only the Internal ALB can talk to them. |

### Tier 3 — Database Tier (Private Subnets)

| Component | Details |
|-----------|---------|
| **PostgreSQL (RDS)** | Persistent relational database for durable data — user accounts, file metadata, encrypted DEKs, audit logs. Primary in AZ1, Read Replica in AZ2. |
| **Redis (ElastiCache)** | In-memory cache for hot-path data — short-link lookups, session tokens, rate-limit counters. Provides sub-millisecond reads for the most frequent operations. |
| **Why separate tier?** | Database and cache instances have different scaling, backup, and security requirements. Isolating them in their own subnets with dedicated security groups ensures only the App Tier can access them — not the Web Tier, not the internet. |

---

## What Goes in PostgreSQL vs Redis

A critical design decision in any caching architecture is **what data belongs in the persistent database vs the in-memory cache**. The guiding principle: **PostgreSQL is the source of truth; Redis is a performance accelerator.**

### PostgreSQL (Persistent Store)

| Data | Why PostgreSQL |
|------|---------------|
| **User accounts & profiles** | Relational data requiring ACID guarantees. Must survive restarts, deployments, and cache flushes. |
| **File metadata** (filename, size, content_type, upload timestamp, uploader email) | Persistent record of every upload. Needed for audit trails, analytics, and admin dashboards. |
| **Encrypted DEK per file** | The encrypted Data Encryption Key is critical for decryption. If lost (e.g., Redis eviction), the file becomes permanently inaccessible. Must be durably stored. |
| **Short-link → S3 key mapping** | The authoritative mapping. Even if Redis cache is flushed, the app can reconstruct the link from PostgreSQL. |
| **Download count tracking** | Authoritative counter requiring ACID transactions. Must not be lost or double-counted due to cache inconsistency. |
| **Upload audit log / history** | Long-lived, queryable records. Users can view their upload history days later. |

### Redis (In-Memory Cache)

| Data | Why Redis |
|------|-----------|
| **Short-link metadata cache** | The hottest read path in the system. Every download hits this. Sub-ms Redis lookups prevent a database round-trip on every download request. TTL matches the link's 24hr expiry. |
| **Active session tokens** | Ephemeral by nature (24hr expiry). High-frequency reads on every authenticated request. Natural fit for Redis TTL-based expiry. |
| **Rate limiting counters** | Extremely high-frequency writes (every request). Sliding-window counters with 60s TTL. Would overwhelm PostgreSQL with write I/O. |
| **Recently uploaded file list (per user dashboard)** | Pre-computed list for the "My Uploads" UI. Can be rebuilt from PostgreSQL if evicted. |

### What Changed from the Original Design

In the original single-tier architecture, **Redis was the only metadata store** — short-link mappings, encrypted DEKs, and download counters all lived exclusively in Redis. This was acceptable for a prototype but had critical weaknesses:

- **Data loss on Redis restart**: All active links would be lost, making uploaded files permanently inaccessible (crypto-shredding by accident).
- **No audit trail**: Once a Redis key expired, there was no record the file ever existed.
- **No queryable history**: Upload history was stored as a Redis list with a 7-day TTL — no SQL queries, no analytics.

The 3-tier architecture fixes this by making PostgreSQL the **source of truth** and Redis a **read-through cache** for performance.

---

## Cache Loading Strategies

### Write Strategy: Write-Back (Write-Behind)

CloudDrop uses a **Write-Back** pattern for the write path: data is written to Redis first (instant, ~0.5ms), then a background thread flushes to PostgreSQL every **10 seconds**.

```
   ┌──────────────────────────────────────────────────────┐
   │                   WRITE PATH                         │
   │                                                      │
   │  App ──► Redis SET "link:{id}" with TTL              │
   │           + SET "dirty:{id}" marker                  │
   │           → Return to user immediately (~0.5ms)      │
   │                                                      │
   │  Background thread (every 10s):                      │
   │           → SCAN for all "dirty:*" keys              │
   │           → Batch INSERT/UPDATE into PostgreSQL      │
   │           → DELETE "dirty:*" markers on success      │
   └──────────────────────────────────────────────────────┘
```

**How dirty markers work:**
- When a link is created or a download counter changes, the app writes to Redis and also sets a `dirty:{short_id}` key containing the action type (`create`, `decrement`, `exhaust`).
- The `db_sync` background thread wakes every 10 seconds, scans for all `dirty:*` keys, reads the corresponding `link:{short_id}` data, and batch-writes to PostgreSQL.
- On successful flush, the dirty marker is removed. On failure, it stays for retry on the next cycle.
- Dirty markers have a 1-hour safety TTL — if the sync thread crashes repeatedly, they don't accumulate forever.

### Read Strategy: Cache-Aside (Lazy Loading)

The read path uses **Cache-Aside**: check Redis first, fall back to PostgreSQL on a cache miss.

```
   ┌──────────────────────────────────────────────────────┐
   │                    READ PATH                         │
   │                                                      │
   │  App ──► Redis GET "link:{id}"                       │
   │           │                                          │
   │           ├── HIT  → return cached data              │
   │           │                                          │
   │           └── MISS → PostgreSQL SELECT ... WHERE     │
   │                       id = {id}                      │
   │                         │                            │
   │                         └── SET Redis "link:{id}"    │
   │                             with TTL = link_expiry   │
   │                             return data              │
   └──────────────────────────────────────────────────────┘
```

**Why this combination works well for CloudDrop:**

1. **Fast writes** — Uploads return instantly after writing to Redis. The PostgreSQL INSERT happens asynchronously in the background.
2. **Resilient reads** — If Redis is down or a key expired, the app falls back to PostgreSQL. Downloads are slower but still work.
3. **Natural TTL alignment** — Cache entries expire at the same time as the link itself (24 hours), so stale data is never served.
4. **Short flush interval** — With a 10-second flush cycle, the maximum data loss window in a Redis failure is only 10 seconds of uploads.

**TTL Policies:**

| Cache Entry | TTL | Rationale |
|-------------|-----|-----------|
| Short-link metadata | Same as link expiry (default 24h) | Cache lifetime matches business logic |
| Session tokens | 24 hours | Match session cookie `max_age` |
| Rate limit counters | 60 seconds (sliding window) | Short-lived by design |
| User upload list | 7 days | Dashboard cache; rebuilt from PostgreSQL on miss |
| Dirty markers | 1 hour (safety net) | Prevents accumulation if sync thread stalls |

### Alternative Strategies (Not Used, but Worth Understanding)

| Strategy | How It Works | Trade-off vs Write-Back |
|----------|--------------|------------------------|
| **Write-Through** | Every write goes to both cache and DB synchronously. | Higher write latency (~5ms extra per upload). Guarantees durability but slows the user-facing response. |
| **Cache-Aside (write)** | Write to DB first, then populate cache. | Similar to Write-Through but cache is populated lazily. Slightly slower writes. |
| **Read-Through** | Cache itself fetches from DB on miss (requires cache-aware proxy). | Over-engineered for this use case. App-managed Cache-Aside is simpler. |

---

## Database Replication Strategy

### RDS Multi-AZ with Read Replica

CloudDrop uses **Amazon RDS for PostgreSQL** with a Multi-AZ deployment:

```
                    ┌─────────────────────────┐
                    │    App Tier (writes)     │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │   PostgreSQL Primary     │
                    │   (AZ1 — 10.0.4.0/24)   │
                    │                         │
                    │   • All WRITE operations │
                    │   • Critical READS       │
                    │   • Daily auto-backups   │
                    └───────────┬─────────────┘
                                │
                     PostgreSQL Streaming
                       Replication (async)
                                │
                    ┌───────────▼─────────────┐
                    │  PostgreSQL Read Replica  │
                    │   (AZ2 — 10.0.5.0/24)   │
                    │                         │
                    │   • READ-only queries    │
                    │   • Upload history       │
                    │   • Analytics / reports  │
                    │   • Failover candidate   │
                    └─────────────────────────┘
```

### Replication Details

| Aspect | Configuration |
|--------|---------------|
| **Replication Type** | PostgreSQL streaming replication (WAL-based) |
| **Replication Mode** | Asynchronous (default) — minimal impact on write latency |
| **Replication Lag** | Typically < 10ms under normal load |
| **Failover** | Automatic via RDS Multi-AZ — if Primary fails, AWS promotes the replica within ~60-120 seconds |
| **Data Consistency** | Writes are always strongly consistent (go to Primary). Reads from the Replica are eventually consistent (ms-level lag). |
| **Backup** | Automated daily snapshots with 7-day retention + continuous WAL archiving for point-in-time recovery |

### Read/Write Splitting

The application uses **two database connection strings**:

```
DATABASE_URL      = postgresql://primary-endpoint:5432/clouddrop   ← All writes + critical reads
DATABASE_READ_URL = postgresql://replica-endpoint:5432/clouddrop   ← Analytics, upload history, dashboards
```

**Which queries go where:**

| Query | Target | Why |
|-------|--------|-----|
| `INSERT` new file metadata | Primary | Write operation |
| `UPDATE` download counter | Primary | Write + needs strong consistency |
| `SELECT` link by short_id (cache miss) | Primary | Needs latest data — a just-uploaded link must be immediately available |
| `SELECT` user upload history | Read Replica | Tolerates slight delay; reduces Primary load |
| `SELECT` analytics / reports | Read Replica | Heavy queries shouldn't impact write performance |

### Why Not Synchronous Replication?

Synchronous replication guarantees **zero data loss** on failover but adds latency to every write (the Primary waits for the Replica to acknowledge). For CloudDrop's use case:

- Files are ephemeral (24hr lifetime) — losing the last few milliseconds of writes during a rare AZ failure is acceptable.
- Upload latency is user-facing — adding synchronous replication overhead to every upload degrades the user experience.
- RDS automated backups + WAL archiving already provide point-in-time recovery for disaster scenarios.

---

## System Components

### 1. Frontend & Client Interface
* **Technologies:** HTML5, CSS3 (Modern, dark-themed vanilla CSS), JavaScript.
* **Role:** User interface for uploading and downloading files. The web interface provides an intuitive experience for generating shareable links and downloading files securely. All rendering is managed server-side using Jinja2 templates via FastAPI, while maintaining dynamic client-side interactions.

### 2. Backend API Service
* **Technologies:** Python, FastAPI.
* **Role:** The core orchestrator. It handles incoming HTTP requests, manages streaming uploads and downloads, interfaces with PostgreSQL and Redis, coordinates encryption/decryption, and streams data to/from AWS S3. FastAPI's asynchronous capabilities allow efficient handling of file streams without blocking.

### 3. Identity and Access Management
* **Technologies:** Amazon Cognito.
* **Role:** Manages user registration, email verification, and login/logout flows. The FastAPI application integrates directly with Cognito for authentication. Logged-in users can track their upload history.

### 4. Edge Networking & CDN
* **Technologies:** Amazon CloudFront, AWS Certificate Manager (ACM), Route 53.
* **Role:** Ensures HTTPS termination, edge caching for static assets, and fast, low-latency routing to the ALB from anywhere in the world. CloudFront provides secure HTTPS (SSL/TLS) for both mobile and desktop clients.

### 5. Persistent Metadata Storage (Database Tier)
* **Technologies:** PostgreSQL (Amazon RDS).
* **Role:** Stores all durable data — user accounts, file metadata, encrypted DEKs, download counters, and audit logs. Deployed as a Multi-AZ Primary + Read Replica for high availability and read scaling.

### 6. In-Memory Cache (Database Tier)
* **Technologies:** Redis (Amazon ElastiCache).
* **Role:** Caches hot-path data for sub-millisecond reads — short-link metadata, session tokens, rate-limit counters. Uses Cache-Aside pattern with TTL-based expiry. Acts as a performance layer over PostgreSQL, not a source of truth.

### 7. File Storage
* **Technologies:** Amazon S3.
* **Role:** Stores the encrypted file blobs. S3 lifecycle policies are configured to delete objects after 24 hours, ensuring storage remains ephemeral.

### 8. Cryptographic Key Management
* **Technologies:** AWS KMS (Key Management Service).
* **Role:** Facilitates envelope encryption. KMS generates and decrypts unique Data Encryption Keys (DEKs) for each uploaded file.

---

## How the System Works

### A. The Upload Workflow

```
User ──► CloudFront ──► Internet-Facing ALB ──► Nginx (Web Tier)
              ──► Internal ALB ──► FastAPI App Server (App Tier)
                    │
                    ├── 1. Generate DEK via AWS KMS
                    ├── 2. Encrypt file with plaintext DEK
                    ├── 3. Upload encrypted blob to S3
                    ├── 4. INSERT metadata + encrypted DEK into PostgreSQL
                    ├── 5. SET short-link cache in Redis (with 24h TTL)
                    ├── 6. Discard plaintext DEK from memory
                    └── 7. Return short-link URL to user
```

1. **Request Routing:** The user submits a file via the frontend. The request flows through CloudFront → Internet-Facing ALB → Nginx Web Server → Internal ALB → FastAPI App Server.
2. **Data Key Generation:** The App Server contacts AWS KMS and requests a new Data Encryption Key (DEK). KMS returns both a **Plaintext DEK** and an **Encrypted DEK**.
3. **Encryption:** The App Server encrypts the file using the **Plaintext DEK** (AES-256-GCM).
4. **S3 Upload:** The encrypted blob is uploaded to S3 with metadata (encrypted DEK, IV) stored as S3 object metadata.
5. **Database Write:** The App Server inserts the file metadata, encrypted DEK, and short-link mapping into **PostgreSQL** (source of truth).
6. **Cache Population:** The same short-link data is also written to **Redis** with a TTL matching the link expiry (Cache-Aside write path — populate cache on write).
7. **Cleanup:** The **Plaintext DEK** is immediately discarded from memory. The short-link URL is returned to the user.

### B. The Download Workflow

```
User clicks short-link ──► CloudFront ──► ALB ──► Nginx ──► Internal ALB ──► App Server
                    │
                    ├── 1. Redis GET "link:{id}" (cache lookup)
                    │       ├── HIT  → use cached metadata
                    │       └── MISS → PostgreSQL SELECT → populate Redis cache
                    ├── 2. Validate download count > 0
                    ├── 3. Send encrypted DEK to KMS → get plaintext DEK
                    ├── 4. Fetch encrypted blob from S3
                    ├── 5. Decrypt with plaintext DEK
                    ├── 6. Stream decrypted file to user
                    ├── 7. UPDATE PostgreSQL download counter (decrement)
                    ├── 8. UPDATE/DELETE Redis cache entry
                    └── 9. Discard plaintext DEK from memory
```

1. **Link Access:** A user clicks a short-link. The request is routed through all 3 tiers to reach the App Server.
2. **Cache-Aside Lookup:** The App Server first checks **Redis** for the short-link metadata. On a cache hit, it uses the cached data directly. On a cache miss, it queries **PostgreSQL**, then populates Redis with the result.
3. **Validation:** If the link doesn't exist (expired or invalid), return 404. If downloads remaining ≤ 0, return 410.
4. **DEK Decryption:** The App Server sends the **Encrypted DEK** to KMS, which returns the **Plaintext DEK**.
5. **File Retrieval & Decryption:** The App Server fetches the encrypted blob from S3 and decrypts it using the Plaintext DEK.
6. **Streaming Response:** The decrypted file is streamed directly to the user's browser.
7. **Counter Update:** The download counter is decremented in **PostgreSQL** (authoritative) and the Redis cache is updated or deleted accordingly.
8. **Cleanup:** If downloads remaining = 0, the S3 object and Redis cache entry are deleted. The Plaintext DEK is discarded from memory.

### C. The Ephemeral Lifecycle

CloudDrop achieves ephemerality through multiple layers:

* **PostgreSQL TTL Records:** Each file record in PostgreSQL includes an `expires_at` timestamp. A scheduled cleanup job (or application-level check) marks expired files as inactive.
* **Redis TTL:** Cache entries automatically expire via Redis's built-in TTL mechanism, ensuring stale links are never served from cache.
* **S3 Lifecycle Policy:** An S3 lifecycle rule automatically deletes objects in the `uploads/` prefix after 24 hours — the ultimate safety net. Even if both PostgreSQL and Redis fail to clean up, S3 guarantees file deletion.
* **Crypto-Shredding:** Once the encrypted DEK is removed from PostgreSQL (either by expiry or download exhaustion), the encrypted blob in S3 is **mathematically useless** — it cannot be decrypted without the DEK.

---

## Network Security & Subnet Isolation

### Defense in Depth

Each tier is isolated in its own subnet(s) with dedicated security groups. Traffic can only flow in one direction through the chain:

```
Internet → ALB SG (80/443) → Web SG (80) → Internal ALB SG (8000) → App SG (8000) → DB SG (5432) / Redis SG (6379)
```

### Security Group Chain

| Security Group | Allows Inbound From | Port | Purpose |
|----------------|---------------------|------|---------|
| `clouddrop-alb-sg` | `0.0.0.0/0` (internet) | 80, 443 | Public entry point |
| `clouddrop-web-sg` | `clouddrop-alb-sg` | 80 | ALB → Nginx only |
| `clouddrop-internal-alb-sg` | `clouddrop-web-sg` | 8000 | Nginx → Internal ALB only |
| `clouddrop-app-sg` | `clouddrop-internal-alb-sg` | 8000 | Internal ALB → App only |
| `clouddrop-db-sg` | `clouddrop-app-sg` | 5432 | App → PostgreSQL only |
| `clouddrop-redis-sg` | `clouddrop-app-sg` | 6379 | App → Redis only |

### Subnet Layout

| Subnet | CIDR | AZ | Tier | Access |
|--------|------|----|------|--------|
| `clouddrop-public-1` | `10.0.0.0/24` | AZ-a | Web | Public (IGW) |
| `clouddrop-public-2` | `10.0.1.0/24` | AZ-b | Web | Public (IGW) |
| `clouddrop-private-app-1` | `10.0.2.0/24` | AZ-a | Application | Private (NAT) |
| `clouddrop-private-app-2` | `10.0.3.0/24` | AZ-b | Application | Private (NAT) |
| `clouddrop-private-data-1` | `10.0.4.0/24` | AZ-a | Database | Private (NAT) |
| `clouddrop-private-data-2` | `10.0.5.0/24` | AZ-b | Database | Private (NAT) |

**Key isolation properties:**
- The **Web Tier** cannot directly access the Database Tier — it must go through the App Tier.
- The **Database Tier** has no route to the internet (even via NAT, this can be restricted).
- **SSH access** to private instances requires a bastion host in the public subnet or EC2 Instance Connect.
