# CloudDrop — AWS Deployment Guide (Console / CLI)

> Complete step-by-step instructions to deploy CloudDrop's **3-Tier Architecture** on AWS.
> All steps use the **AWS Console** unless a CLI command is shown.

---

## Table of Contents

1. [VPC & Networking](#1-vpc--networking)
2. [S3 Bucket + Lifecycle](#2-s3-bucket--lifecycle-policy)
3. [KMS Key](#3-kms-key)
4. [Security Groups](#4-security-groups)
5. [RDS PostgreSQL](#5-rds-postgresql)
6. [ElastiCache Redis](#6-elasticache-redis)
7. [IAM Role & Instance Profile](#7-iam-role--instance-profile)
8. [Cognito User Pool](#8-cognito-user-pool)
9. [EC2 Instances (App Tier + Web Tier)](#9-ec2-instances)
10. [Internal Application Load Balancer](#10-internal-application-load-balancer)
11. [Internet-Facing Application Load Balancer](#11-internet-facing-application-load-balancer)
12. [CloudFront CDN](#12-cloudfront-cdn)
13. [Route 53 DNS](#13-route-53-dns)

---

## 1. VPC & Networking

### 1.1 Create the VPC

1. Open **VPC Console** → **Create VPC**
2. Select **VPC and more** (auto-creates subnets, IGW, route tables)
3. Configure:

| Setting | Value |
|---|---|
| Name tag | `clouddrop-vpc` |
| IPv4 CIDR | `10.0.0.0/16` |
| Number of AZs | **2** |
| Public subnets | **2** (`10.0.0.0/24` in AZ-a, `10.0.1.0/24` in AZ-b) |
| Private subnets | **4** (see below) |
| NAT gateways | **In 2 AZs** (one per AZ for high availability) |
| VPC endpoints | None |

4. Click **Create VPC**.

> **Note:** The VPC wizard may only create 2 private subnets by default. After creation, manually add the remaining private subnets.

### 1.2 Create Additional Private Subnets (if needed)

If the wizard only created 2 private subnets, manually create the rest:

1. **VPC Console** → **Subnets** → **Create subnet**
2. Create the following subnets in `clouddrop-vpc`:

| Subnet Name | CIDR | AZ | Tier | Route Table |
|---|---|---|---|---|
| `clouddrop-public-1` | `10.0.0.0/24` | AZ-a | Web | Public RT → IGW |
| `clouddrop-public-2` | `10.0.1.0/24` | AZ-b | Web | Public RT → IGW |
| `clouddrop-private-app-1` | `10.0.2.0/24` | AZ-a | Application | Private RT → NAT-a |
| `clouddrop-private-app-2` | `10.0.3.0/24` | AZ-b | Application | Private RT → NAT-b |
| `clouddrop-private-data-1` | `10.0.4.0/24` | AZ-a | Database | Private RT → NAT-a |
| `clouddrop-private-data-2` | `10.0.5.0/24` | AZ-b | Database | Private RT → NAT-b |

> **Why 6 subnets?** The 3-tier architecture isolates each tier into its own subnet pair across 2 AZs. This provides fault tolerance and ensures security groups can restrict traffic at the subnet level.

### 1.3 Verify Route Tables

- **Public RT:** `0.0.0.0/0 → igw-xxxxx`
- **Private RT (AZ-a):** `0.0.0.0/0 → nat-xxxxx-a`
- **Private RT (AZ-b):** `0.0.0.0/0 → nat-xxxxx-b`

Associate each subnet with the correct route table.

> **Why 2 NAT Gateways?** If a single NAT Gateway fails (or its AZ goes down), all private instances lose internet access. Having one NAT per AZ ensures the App and Database tiers remain operational independently.

---

## 2. S3 Bucket + Lifecycle Policy

### 2.1 Create the Bucket

1. Open **S3 Console** → **Create bucket**
2. Configure:

| Setting | Value |
|---|---|
| Bucket name | `clouddrop-files` (must be globally unique — append your ID) |
| Region | `ap-south-1` (or your region) |
| Block all public access | ✅ **Enabled** |
| Versioning | Disabled |
| Encryption | SSE-S3 (default) — actual encryption is handled by the app via KMS |

3. Click **Create bucket**.

### 2.2 Add 24-Hour Lifecycle Policy (Ephemeral Cleanup)

1. Go to the bucket → **Management** tab → **Create lifecycle rule**
2. Configure:

| Setting | Value |
|---|---|
| Rule name | `auto-delete-24h` |
| Prefix filter | `uploads/` |
| Rule actions | ✅ Expire current versions of objects |
| Days after creation | `1` |

3. Click **Create rule**.

> **This is the core ephemeral mechanism** — S3 automatically deletes all uploaded files after 24 hours, even if PostgreSQL/Redis fail to clean up.

---

## 3. KMS Key

1. Open **KMS Console** → **Create key**
2. Configure:

| Setting | Value |
|---|---|
| Key type | Symmetric |
| Key usage | Encrypt and decrypt |
| Alias | `clouddrop-key` |
| Key administrators | Your IAM user |
| Key users | `CloudDropEC2Role` (create this first in Step 7, or come back to add it) |

3. Click **Finish**.
4. **Note the Key ARN** — you'll need it for the IAM policy.

---

## 4. Security Groups

Create **six** Security Groups in the `clouddrop-vpc`. These form a **chain of trust** — each tier only accepts traffic from the tier directly above it.

### SG 1: Internet-Facing ALB (`clouddrop-alb-sg`)

| Type | Protocol | Port | Source | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 80 | `0.0.0.0/0` | HTTP from internet |
| Inbound | TCP | 443 | `0.0.0.0/0` | HTTPS from internet |
| Outbound | All | All | `0.0.0.0/0` | |

### SG 2: Web Tier — Nginx (`clouddrop-web-sg`)

| Type | Protocol | Port | Source | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 80 | `clouddrop-alb-sg` | Traffic from ALB only |
| Inbound | TCP | 22 | Your IP | SSH debug (optional) |
| Outbound | All | All | `0.0.0.0/0` | |

### SG 3: Internal ALB (`clouddrop-internal-alb-sg`)

| Type | Protocol | Port | Source | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 8000 | `clouddrop-web-sg` | Traffic from Nginx only |
| Outbound | All | All | `0.0.0.0/0` | |

### SG 4: App Tier — FastAPI (`clouddrop-app-sg`)

| Type | Protocol | Port | Source | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 8000 | `clouddrop-internal-alb-sg` | Traffic from Internal ALB only |
| Inbound | TCP | 22 | Your IP | SSH via bastion (optional) |
| Outbound | All | All | `0.0.0.0/0` | |

### SG 5: Database — PostgreSQL RDS (`clouddrop-db-sg`)

| Type | Protocol | Port | Source | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 5432 | `clouddrop-app-sg` | Traffic from App Tier only |
| Outbound | All | All | `0.0.0.0/0` | |

### SG 6: Cache — Redis (`clouddrop-redis-sg`)

| Type | Protocol | Port | Source | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 6379 | `clouddrop-app-sg` | Traffic from App Tier only |
| Outbound | All | All | `0.0.0.0/0` | |

> **Defense in Depth:** Internet → ALB → Nginx (Web) → Internal ALB → FastAPI (App) → PostgreSQL/Redis (Data). No tier can be accessed by skipping a layer. The Database Tier is **two hops away** from the internet.

---

## 5. RDS PostgreSQL

### 5.1 Create DB Subnet Group

1. Open **RDS Console** → **Subnet groups** → **Create DB subnet group**
2. Configure:

| Setting | Value |
|---|---|
| Name | `clouddrop-db-subnet-group` |
| Description | Database tier subnets for CloudDrop |
| VPC | `clouddrop-vpc` |
| Subnets | Select `clouddrop-private-data-1` (AZ-a) and `clouddrop-private-data-2` (AZ-b) |

3. Click **Create**.

### 5.2 Create the Primary PostgreSQL Instance

1. **RDS Console** → **Create database**
2. Configure:

| Setting | Value |
|---|---|
| Creation method | Standard create |
| Engine | PostgreSQL |
| Engine version | Latest (e.g., 15.x) |
| Templates | **Free tier** |
| DB instance identifier | `clouddrop-db` |
| Master username | `clouddrop_admin` |
| Master password | (choose a strong password, note it down) |
| DB instance class | `db.t3.micro` (Free Tier eligible) |
| Storage type | gp3 |
| Allocated storage | 20 GB |
| Storage autoscaling | ✅ Enable (max 50 GB) |
| VPC | `clouddrop-vpc` |
| DB subnet group | `clouddrop-db-subnet-group` |
| Public access | ❌ **No** |
| VPC security group | Select `clouddrop-db-sg` |
| AZ | Select AZ-a (for the primary) |
| Database name | `clouddrop` |
| Backup retention | 7 days |
| Enable encryption | ✅ Yes (use default KMS key or `clouddrop-key`) |

3. Click **Create database**. Wait ~5-10 minutes for provisioning.
4. **Note the Endpoint** (e.g., `clouddrop-db.xxxxx.ap-south-1.rds.amazonaws.com`) — this is `DATABASE_URL`.

### 5.3 Create a Read Replica (AZ2)

1. Select the `clouddrop-db` instance → **Actions** → **Create read replica**
2. Configure:

| Setting | Value |
|---|---|
| DB instance identifier | `clouddrop-db-replica` |
| DB instance class | `db.t3.micro` |
| AZ | Select **AZ-b** (different from primary) |
| Public access | ❌ No |
| VPC security group | `clouddrop-db-sg` |

3. Click **Create read replica**.
4. **Note the Replica Endpoint** — this is `DATABASE_READ_URL`.

> **Replication:** RDS uses PostgreSQL's native streaming replication (WAL-based, asynchronous). The replica typically lags < 10ms behind the primary. If the primary fails, you can manually promote the replica or enable Multi-AZ for automatic failover.

---

## 6. ElastiCache Redis

1. Open **ElastiCache Console** → **Create cluster** → **Redis OSS**
2. Configure:

| Setting | Value |
|---|---|
| Cluster mode | Disabled |
| Name | `clouddrop-redis` |
| Node type | `cache.t3.micro` (Free Tier eligible) |
| Number of replicas | 0 |
| Subnet group | Create new → select `clouddrop-vpc`, choose `clouddrop-private-data-1` and `clouddrop-private-data-2` |
| Security group | `clouddrop-redis-sg` |

3. Click **Create**.
4. **Note the Primary Endpoint** (e.g., `clouddrop-redis.xxxxx.apse1.cache.amazonaws.com`) — set as `REDIS_HOST`.

> **Redis Role in the 3-Tier Architecture:** Redis is a **cache layer**, not the source of truth. It accelerates short-link lookups and session validation. The app uses Cache-Aside: check Redis first → miss → query PostgreSQL → populate Redis. See [ARCHITECTURE.md](file:///c:/Users/dhanu/OneDrive/Documents/CloudDrop/ARCHITECTURE.md) for the full caching strategy.

---

## 7. IAM Role & Instance Profile

### 7.1 Create the IAM Policy

1. Open **IAM Console** → **Policies** → **Create policy**
2. Switch to **JSON** tab and paste the contents of [`iam_policies.json`](file:///c:/Users/dhanu/OneDrive/Documents/CloudDrop/iam_policies.json)
3. Replace `<ACCOUNT_ID>` and `<KMS_KEY_ID>` with your values.
4. Name the policy: `CloudDropS3KMSPolicy`

### 7.2 Create the IAM Role

1. **IAM Console** → **Roles** → **Create role**
2. Trusted entity: **AWS service** → **EC2**
3. Attach policy: `CloudDropS3KMSPolicy`
4. Role name: `CloudDropEC2Role`
5. Click **Create role**.

> **No hardcoded credentials!** The EC2 instances assume this role via the instance profile, and `boto3` automatically uses the role credentials.

---

## 8. Cognito User Pool

1. Open **Cognito Console** → **Create user pool**
2. Configure:

| Setting | Value |
|---|---|
| Sign-in options | Email |
| Password policy | Default (8+ chars, mixed case, digit, symbol) |
| MFA | Optional (or None for lab simplicity) |
| User pool name | `clouddrop-users` |
| App client name | `clouddrop-web` |
| Client secret | Generate a client secret ✅ |
| Allowed callback URLs | `http://<YOUR-ALB-DNS>/auth/callback` (update after ALB is created) |
| Allowed sign-out URLs | `http://<YOUR-ALB-DNS>` (update after ALB is created) |
| Hosted UI | ✅ Enable — configure a **Cognito domain** (e.g., `clouddrop-auth`) |

3. Click **Create user pool**.
4. **Note these values:**
   - User Pool ID (e.g., `ap-south-1_AbCdEfGhI`)
   - App Client ID
   - App Client Secret
   - Cognito Domain (e.g., `clouddrop-auth.auth.ap-south-1.amazoncognito.com`)

---

## 9. EC2 Instances

### 9.1 App Tier — FastAPI Server (Private Subnet, AZ-a)

1. **EC2 Console** → **Launch instances**
2. Configure:

| Setting | Value |
|---|---|
| Name | `clouddrop-app-server-1` |
| AMI | Amazon Linux 2023 (latest) |
| Instance type | `t3.micro` (Free Tier eligible) |
| Key pair | Your SSH key |
| VPC | `clouddrop-vpc` |
| Subnet | `clouddrop-private-app-1` (AZ-a) |
| Auto-assign public IP | **Disabled** |
| Security group | `clouddrop-app-sg` |
| IAM instance profile | `CloudDropEC2Role` |

3. **Advanced details** → **User data** → paste [`deploy_userdata.sh`](file:///c:/Users/dhanu/OneDrive/Documents/CloudDrop/deploy_userdata.sh)
4. Click **Launch instance**.

> The App Servers are in private subnets — no public IP. They reach the internet via NAT Gateway (for pip installs, S3 API calls, KMS, etc.). Access for debugging via **EC2 Instance Connect** or a bastion host.

### 9.2 App Tier — FastAPI Server (Private Subnet, AZ-b)

1. **EC2 Console** → **Launch instances**
2. Configure:

| Setting | Value |
|---|---|
| Name | `clouddrop-app-server-2` |
| AMI | Amazon Linux 2023 (latest) |
| Instance type | `t3.micro` |
| Key pair | Your SSH key |
| VPC | `clouddrop-vpc` |
| Subnet | `clouddrop-private-app-2` (AZ-b) |
| Auto-assign public IP | **Disabled** |
| Security group | `clouddrop-app-sg` |
| IAM instance profile | `CloudDropEC2Role` |

3. **Advanced details** → **User data** → paste the same [`deploy_userdata.sh`](file:///c:/Users/dhanu/OneDrive/Documents/CloudDrop/deploy_userdata.sh)
4. Click **Launch instance**.

### 9.3 Web Tier — Nginx Server (Public Subnet, AZ-a)

1. **EC2 Console** → **Launch instances**
2. Configure:

| Setting | Value |
|---|---|
| Name | `clouddrop-web-1` |
| AMI | Amazon Linux 2023 (latest) |
| Instance type | `t3.micro` |
| Key pair | Your SSH key |
| VPC | `clouddrop-vpc` |
| Subnet | `clouddrop-public-1` (AZ-a) |
| Auto-assign public IP | **Enabled** |
| Security group | `clouddrop-web-sg` |

3. **User data** — paste the following Nginx setup script:

```bash
#!/bin/bash
dnf install -y nginx

# Configure Nginx as a reverse proxy to the Internal ALB
cat > /etc/nginx/conf.d/clouddrop.conf << 'EOF'
server {
    listen 80;
    server_name _;

    client_max_body_size 55M;

    # Static assets — serve locally or proxy
    location /static/ {
        proxy_pass http://internal-clouddrop-internal-alb-1945531104.ap-south-1.elb.amazonaws.com:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        expires 1d;
        add_header Cache-Control "public, immutable";
    }

    # All other requests — proxy to Internal ALB
    location / {
        proxy_pass http://internal-clouddrop-internal-alb-1945531104.ap-south-1.elb.amazonaws.com:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }

    # Local health check for ALB (doesn't proxy — responds directly)
    location /nginx-health {
        return 200 "OK";
        add_header Content-Type text/plain;
    }
}
EOF

# Remove default server block
rm -f /etc/nginx/conf.d/default.conf

systemctl enable nginx
systemctl start nginx
```

4. Click **Launch instance**.

> **Note:** The Internal ALB DNS (`clouddrop-internal-alb-865449504.ap-south-1.elb.amazonaws.com`) is already configured in the Nginx proxy_pass directives above.

### 9.4 Web Tier — Nginx Server (Public Subnet, AZ-b)

Repeat Step 9.3 but select:
- **Name:** `clouddrop-web-2`
- **Subnet:** `clouddrop-public-2` (AZ-b)

Use the same user data script and security group.

---

## 10. Internal Application Load Balancer

This is a **private** ALB that sits between the Web Tier and App Tier. It is not accessible from the internet.

### 10.1 Create Target Group (App Tier)

1. **EC2 Console** → **Target Groups** → **Create target group**
2. Configure:

| Setting | Value |
|---|---|
| Target type | Instances |
| Name | `clouddrop-app-tg` |
| Protocol/Port | HTTP / 8000 |
| VPC | `clouddrop-vpc` |
| Health check path | `/health` |
| Healthy threshold | 2 |
| Interval | 30s |

3. **Register targets** → select `clouddrop-app-server-1` and `clouddrop-app-server-2` → port `8000` → **Include as pending** → **Register**.

### 10.2 Create Internal ALB

1. **EC2 Console** → **Load Balancers** → **Create** → **Application Load Balancer**
2. Configure:

| Setting | Value |
|---|---|
| Name | `clouddrop-internal-alb` |
| Scheme | **Internal** |
| IP address type | IPv4 |
| VPC | `clouddrop-vpc` |
| Subnets | `clouddrop-private-app-1` (AZ-a) and `clouddrop-private-app-2` (AZ-b) |
| Security group | `clouddrop-internal-alb-sg` |

3. **Listener:**

| Setting | Value |
|---|---|
| Protocol | HTTP |
| Port | 8000 |
| Default action | Forward to `clouddrop-app-tg` |

4. Click **Create load balancer**.
5. **Note the Internal ALB DNS name** — update the Nginx config in your Web Tier instances to point to this DNS.

> **Why an Internal ALB?** It decouples the Web Tier from the App Tier. The Nginx servers don't need to know the App Server's private IP — they just proxy to the Internal ALB's DNS. This also makes it trivial to add more App Servers later (just register them in the target group).

---

## 11. Internet-Facing Application Load Balancer

This is the public entry point for all user traffic.

### 11.1 Create Target Group (Web Tier)

1. **EC2 Console** → **Target Groups** → **Create target group**
2. Configure:

| Setting | Value |
|---|---|
| Target type | Instances |
| Name | `clouddrop-web-tg` |
| Protocol/Port | HTTP / 80 |
| VPC | `clouddrop-vpc` |
| Health check path | `/nginx-health` |
| Healthy threshold | 2 |
| Interval | 30s |

3. **Register targets** → select `clouddrop-web-1` and `clouddrop-web-2` → port `80` → **Register**.

### 11.2 Create Internet-Facing ALB

1. **EC2 Console** → **Load Balancers** → **Create** → **Application Load Balancer**
2. Configure:

| Setting | Value |
|---|---|
| Name | `clouddrop-alb` |
| Scheme | **Internet-facing** |
| IP address type | IPv4 |
| VPC | `clouddrop-vpc` |
| Subnets | `clouddrop-public-1` (AZ-a) and `clouddrop-public-2` (AZ-b) |
| Security group | `clouddrop-alb-sg` |

3. **Listener:**

| Setting | Value |
|---|---|
| Protocol | HTTP |
| Port | 80 |
| Default action | Forward to `clouddrop-web-tg` |

4. Click **Create load balancer**.
5. **Note the ALB DNS name** (e.g., `clouddrop-alb-xxxxx.ap-south-1.elb.amazonaws.com`) — this is the app's public URL.

---

## 12. CloudFront CDN

1. Open **CloudFront Console** → **Create distribution**
2. Configure:

| Setting | Value |
|---|---|
| Origin domain | `clouddrop-alb-xxxxx.ap-south-1.elb.amazonaws.com` |
| Protocol | HTTP only |
| Origin path | (blank) |
| Name | `clouddrop-origin` |

3. **Cache behavior — Default:**

| Setting | Value |
|---|---|
| Path pattern | Default (`*`) |
| Viewer protocol policy | Redirect HTTP to HTTPS |
| Allowed HTTP methods | GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE |
| Cache policy | `CachingDisabled` (dynamic API content) |
| Origin request policy | `AllViewer` |

4. **Cache behavior — Static assets (add second behavior):**

| Setting | Value |
|---|---|
| Path pattern | `/static/*` |
| Cache policy | `CachingOptimized` (TTL 86400) |
| Compress objects | Yes |

5. **Settings:**  
   - Alternate domain names (CNAMEs): `your-domain.com`
   - Custom SSL certificate: Select your ACM cert (must be in `us-east-1`)
   - Default root object: (blank)

6. Click **Create distribution**.

> CloudFront provides free HTTPS via the `d1234abcd.cloudfront.net` domain. Update `BASE_URL` to use this HTTPS URL.

---

## 13. Route 53 DNS

1. Open **Route 53** → your **Hosted Zone**
2. **Create Record:**

| Setting | Value |
|---|---|
| Record name | `clouddrop` (or blank for apex) |
| Record type | A |
| Alias | ✅ Yes |
| Route traffic to | **CloudFront distribution** |
| Routing policy | Simple |

3. Click **Create records**.

---

## Architecture Summary

```
                                    ┌── STORAGE LAYER ──────────────┐
                                    │  Amazon S3                    │
                                    │  Encrypted File Blobs         │
                                    │  24hr Auto-Delete Lifecycle   │
                                    │         │                     │
                                    │    expire objects             │
                                    │         ▼                     │
                                    │  CloudFront CDN               │
                                    │  (pre-signed URLs)            │
                                    └───────────────────────────────┘

Users ──► CloudFront ──► Internet-Facing ALB
                              │
              ┌───── WEB TIER (Public Subnets) ─────────┐
              │                                         │
              │  AZ1: Nginx (clouddrop-web-1)           │
              │  AZ2: Nginx (clouddrop-web-2)           │
              │                                         │
              └────────────────┬────────────────────────┘
                               │
                       Internal ALB
                               │
              ┌── APPLICATION TIER (Private Subnets) ───┐
              │                                         │
              │  AZ1: FastAPI App Server                │
              │  (clouddrop-app-server)                 │
              │       │                │                │
              └───────┼────────────────┼────────────────┘
                      │                │
              ┌── DATABASE TIER (Private Subnets) ──────┐
              │       ▼                ▼                │
              │  Redis Cache     PostgreSQL Primary     │
              │  (ElastiCache)    (AZ1)                 │
              │  Short-Link ──►        │                │
              │  File Mapping     replication           │
              │                        │                │
              │                  PostgreSQL Replica     │
              │                   (AZ2)                 │
              └─────────────────────────────────────────┘
```

**Traffic Flow:**
```
Internet → ALB (public) → Nginx → Internal ALB (private) → FastAPI → PostgreSQL / Redis
```

**Security Group Chain:**
```
ALB SG (80/443 from 0.0.0.0/0)
  → Web SG (80 from ALB SG)
    → Internal ALB SG (8000 from Web SG)
      → App SG (8000 from Internal ALB SG)
        → DB SG (5432 from App SG)
        → Redis SG (6379 from App SG)
```

---

## Quick Reference — Environment Variables

| Variable | Value |
|---|---|
| `AWS_REGION` | `ap-south-1` |
| `S3_BUCKET` | `clouddrop-files-556684850112` |
| `KMS_KEY_ID` | `alias/clouddrop-key` |
| `REDIS_HOST` | `master.clouddrop-redis.ezyqzx.aps1.cache.amazonaws.com` |
| `REDIS_PORT` | `6379` |
| `REDIS_USE_TLS` | `true` |
| `DATABASE_URL` | `postgresql://clouddrop_admin:clouddrop123@clouddrop-db.cnuk6w4yq4ob.ap-south-1.rds.amazonaws.com:5432/clouddrop` |
| `DATABASE_READ_URL` | `postgresql://clouddrop_admin:clouddrop123@clouddrop-db-replica.cnuk6w4yq4ob.ap-south-1.rds.amazonaws.com:5432/clouddrop` |
| `DB_SYNC_INTERVAL_SECONDS` | `10` |
| `MAX_FILE_SIZE_MB` | `50` |
| `MAX_DOWNLOADS` | `5` |
| `BASE_URL` | `https://d2vkvqf4pmsk1k.cloudfront.net` |
| `COGNITO_CLIENT_ID` | `qgrs9s76h2f8cvili0vajhb1t` |
| `COGNITO_CLIENT_SECRET` | `19jia4ukfrq0e16peofknb8acrevkstetnn3b0bosg87n62v07ii` |
| `COGNITO_DOMAIN` | `ap-south-1bnqedrrir.auth.ap-south-1.amazoncognito.com` |
| `COGNITO_USER_POOL_ID` | `ap-south-1_bNqedrrir` |

