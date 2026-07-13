# Complete CI/CD & Cluster Operations Manual

> **Scope:** Flask app → Docker → GitHub Actions → Kind cluster → Helm → ArgoCD → Live endpoint  
> **Repo:** `ahmed-shereif/python-test` | **Docker Hub:** `asherif310/python-test`

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [The Application — src/app.py](#2-the-application--srcapppy)
3. [Docker](#3-docker)
4. [Kind — Local Kubernetes Cluster](#4-kind--local-kubernetes-cluster)
5. [Kubernetes Manifests (k8s/)](#5-kubernetes-manifests-k8s)
6. [Helm Chart](#6-helm-chart)
7. [ArgoCD](#7-argocd)
8. [Self-Hosted GitHub Actions Runner](#8-self-hosted-github-actions-runner)
9. [GitHub Actions CI/CD Pipeline](#9-github-actions-cicd-pipeline)
10. [Secrets Reference](#10-secrets-reference)
11. [Day-to-Day Command Reference](#11-day-to-day-command-reference)
12. [End-to-End Flow Walkthrough](#12-end-to-end-flow-walkthrough)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Architecture Overview

```
Developer (git push)
        │
        ▼
GitHub Actions ── CI job (ubuntu-latest)
        │              └─ Build Docker image
        │              └─ Push to Docker Hub  :short-sha tag
        │
        ├── update-manifest job (ubuntu-latest)
        │              └─ Patch charts/python-test/values.yaml  tag: "<sha>"
        │              └─ git commit & push  [skip ci]
        │
        └── cd job (self-hosted runner inside Kind cluster)
                       └─ argocd app sync python-test
                                │
                                ▼
                         ArgoCD (in-cluster)
                                │
                                ▼
                    Helm chart deployed to Kind
                                │
                                ▼
                    Flask pod  ──  Service  ──  nginx Ingress
                                                      │
                                                      ▼
                                           python-test.example.com
```

**Component roles:**

| Component | Role |
|---|---|
| Flask (Python) | The actual web app serving REST endpoints |
| Docker | Packages the app into a portable image |
| Kind | Lightweight Kubernetes cluster running in Docker containers on your laptop |
| Helm | Kubernetes package manager — templates the manifests so one chart deploys anywhere |
| ArgoCD | GitOps CD controller — watches the Git repo and syncs the cluster to match it |
| GitHub Actions | CI/CD automation platform — builds, tags, updates, and triggers sync |
| Self-hosted runner | A pod inside Kind that runs the `cd` job so it can reach ArgoCD without exposing it to the internet |
| nginx Ingress | Routes external HTTP traffic into the cluster by hostname/path |

---

## 2. The Application — `src/app.py`

```python
from flask import Flask, jsonify
import socket

app = Flask(__name__)
```
Creates a Flask web application instance.

```python
@app.route('/api/v1/details', methods=['GET'])
def get_details():
    details = {
        'name': 'Sample API',
        'version': '1.0',
        'hostname': socket.gethostname(),   # ← returns the pod name inside K8s
        'message': 'Hello from the API!',
        ...
    }
    return jsonify(details)
```
Returns a JSON payload. The `hostname` field is useful because `socket.gethostname()` inside a Kubernetes pod returns the **pod name**, letting you confirm which replica answered.

```python
@app.route('/api/v1/health', methods=['GET'])
def get_health():
    return jsonify({'status': 'healthy'})
```
Health endpoint. Kubernetes liveness/readiness probes can call this to know if the pod is alive.

```python
app.run(host='0.0.0.0', port=5000, debug=True)
```
`host='0.0.0.0'` makes Flask listen on all network interfaces inside the container — required so traffic from outside the container can reach it. Port `5000` is the internal container port.

---

## 3. Docker

### `Dockerfile` explained

```dockerfile
FROM python:3.13-slim
```
Base image. `slim` = smaller size, only what Python needs, no dev tools.

```dockerfile
COPY requirements.txt /temp/requirements.txt
RUN pip install --no-cache-dir -r /temp/requirements.txt
```
Copies the dependency list first and installs it. Putting this **before** copying source code is intentional: Docker caches each layer. If you only change `app.py`, Docker reuses the already-built pip layer and doesn't re-install packages.

```dockerfile
COPY ./src /src
WORKDIR /src
CMD ["python", "app.py"]
```
Copies the source code into `/src`, sets it as the working directory, then runs the app. Using `CMD` as an array (exec form) is recommended because it runs `python` directly without a shell wrapper.

### `requirements.txt`

```
flask
```
The only dependency. Flask brings in everything else (Werkzeug, Jinja2, etc.) transitively.

---

### Essential Docker commands

```powershell
# Build image locally (for testing before pushing)
docker build -t asherif310/python-test:local .

# Run the container locally to test it
docker run -p 5000:5000 asherif310/python-test:local

# Hit the endpoints from your host machine
curl http://localhost:5000/api/v1/health
curl http://localhost:5000/api/v1/details

# Log in to Docker Hub (required before push)
docker login -u asherif310

# Push a specific tag
docker push asherif310/python-test:<tag>

# List locally built images
docker images | Select-String python-test

# Remove a local image
docker rmi asherif310/python-test:local

# Inspect an image's layers
docker history asherif310/python-test:local

# Pull a specific tag from Docker Hub
docker pull asherif310/python-test:<sha>

# See running containers
docker ps

# Get logs from a running container
docker logs <container-id>

# Interactive shell into a running container (debugging)
docker exec -it <container-id> /bin/sh
```

---

## 4. Kind — Local Kubernetes Cluster

Kind (Kubernetes IN Docker) runs a full K8s cluster where each node is a Docker container. It is the cluster where everything runs locally.

### `kind-config.yaml` explained

```yaml
apiVersion: kind.x-k8s.io/v1alpha4
kind: Cluster
nodes:
- role: control-plane
  extraPortMappings:
  - containerPort: 80
    hostPort: 80
    protocol: TCP
  - containerPort: 443
    hostPort: 443
    protocol: TCP
```
Maps ports 80 and 443 from your laptop (`hostPort`) into the Kind node container (`containerPort`). This is what makes `http://python-test.example.com` reachable on your machine — traffic enters port 80 on `localhost`, gets forwarded into the Kind node, and nginx Ingress picks it up.

```yaml
  extraMounts:
  - hostPath: ./corp-ca.crt
    containerPath: /usr/local/share/ca-certificates/corp-ca.crt
```
Mounts your corporate CA certificate into the node at creation time. Required if your company uses a private CA so that containerd can pull images from internal registries without TLS errors.

```yaml
containerdConfigPatches:
- |-
  [plugins."io.containerd.grpc.v1.cri".registry]
    config_path = "/etc/containerd/certs.d"
```
Tells containerd (the container runtime inside Kind) where to look for per-registry certificate/mirror configs. Needed for corporate proxy environments.

### Kind cluster commands

```powershell
# Create cluster from config file
kind create cluster --config kind-config.yaml --name <cluster-name>

# List clusters
kind get clusters

# Delete a cluster
kind delete cluster --name <cluster-name>

# Load a locally built image into Kind (avoids needing to push to registry)
kind load docker-image asherif310/python-test:<tag> --name <cluster-name>

# Export cluster kubeconfig
kind export kubeconfig --name <cluster-name>

# Get the kubeconfig merged into ~/.kube/config
kind get kubeconfig --name <cluster-name> > ~/.kube/kind-config
```

### kubectl — General cluster commands

```powershell
# View cluster info
kubectl cluster-info

# List all nodes
kubectl get nodes

# List all pods (default namespace)
kubectl get pods

# List pods in a specific namespace
kubectl get pods -n argocd
kubectl get pods -n actions-runner-system

# List all pods across all namespaces
kubectl get pods -A

# Describe a pod (events, mounts, status detail)
kubectl describe pod <pod-name>

# Get logs from a pod
kubectl logs <pod-name>
kubectl logs <pod-name> -f          # follow/stream logs
kubectl logs <pod-name> --previous  # logs from crashed previous container

# Execute a command inside a pod
kubectl exec -it <pod-name> -- /bin/sh

# Watch pods in real time
kubectl get pods -w

# Get all resources in a namespace
kubectl get all -n <namespace>

# Delete a pod (it will be recreated by the Deployment)
kubectl delete pod <pod-name>

# Port-forward a service to localhost
kubectl port-forward svc/<service-name> <local-port>:<service-port>
kubectl port-forward svc/python-test 5000:5000
kubectl port-forward svc/argocd-server -n argocd 8080:443

# Apply manifests
kubectl apply -f k8s/
kubectl apply -f k8s/deploy.yaml

# Delete resources from manifests
kubectl delete -f k8s/

# Get resource YAML
kubectl get deployment python-test -o yaml

# Check rollout status
kubectl rollout status deployment <deployment-name>

# Rollback to previous version
kubectl rollout undo deployment <deployment-name>

# View rollout history
kubectl rollout history deployment <deployment-name>
```

---

## 5. Kubernetes Manifests (`k8s/`)

These are the raw (non-Helm) manifests. They can be applied directly for quick testing without Helm.

### `k8s/deploy.yaml` — Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: python-test
  labels:
    app: python-test
spec:
  replicas: 3                    # Run 3 identical pods for availability
  selector:
    matchLabels:
      app: python-test           # This Deployment manages pods with this label
  template:
    metadata:
      labels:
        app: python-test
    spec:
      containers:
      - name: python-test
        image: asherif310/python-test:v1   # Image from Docker Hub
        ports:
        - containerPort: 5000              # Port Flask listens on inside container
```

**Key concepts:**
- `replicas: 3` — Kubernetes ensures 3 pods are always running. If one crashes, it restarts it.
- `selector.matchLabels` — ties the Deployment to its pods via labels.
- `containerPort` is informational — it doesn't open a firewall. Traffic routing is handled by the Service.

### `k8s/service.yaml` — Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: python-test
spec:
  selector:
    app: python-test        # Finds pods with this label
  ports:
    - protocol: TCP
      port: 8080            # Port the Service exposes inside the cluster
      targetPort: 5000      # Port on the pod to forward to
```

**Key concepts:**
- A Service is a stable virtual IP + DNS name for a set of pods. Pods come and go; the Service IP stays.
- `port: 8080` → internal cluster access is `python-test:8080`
- `targetPort: 5000` → traffic gets forwarded to port 5000 on each pod
- No `type` specified = defaults to `ClusterIP` (only reachable inside the cluster)

### `k8s/ingress.yaml` — Ingress

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: python-test
spec:
  ingressClassName: nginx      # Use the nginx Ingress controller
  rules:
  - http:
      paths:
      - path: /api/v1
        pathType: Prefix       # Any path starting with /api/v1 matches
        backend:
          service:
            name: python-test
            port:
              number: 8080
```

**Key concepts:**
- The Ingress is the entry door for external HTTP traffic.
- `ingressClassName: nginx` tells K8s which Ingress controller handles this rule.
- `pathType: Prefix` — `/api/v1`, `/api/v1/health`, `/api/v1/details` all match.
- Traffic flow: `Browser → nginx Ingress → Service (port 8080) → Pod (port 5000)`

---

## 6. Helm Chart

Helm is a package manager for Kubernetes. Instead of maintaining separate YAML files per environment, Helm uses **templates** + **values files** to produce the final manifests.

### Chart structure

```
charts/python-test/
├── Chart.yaml          # Chart metadata (name, version, appVersion)
├── values.yaml         # Default configuration values
├── argocd/
│   └── values-argo.yaml  # ArgoCD-specific Helm values for ArgoCD itself
└── templates/
    ├── _helpers.tpl    # Reusable template functions (named templates)
    ├── deployment.yaml # Templated Deployment
    ├── service.yaml    # Templated Service
    ├── ingress.yaml    # Templated Ingress
    ├── httproute.yaml  # Optional Gateway API HTTPRoute
    ├── serviceaccount.yaml
    └── NOTES.txt       # Displayed after helm install
```

### `Chart.yaml` explained

```yaml
apiVersion: v2
name: python-test
description: A Helm chart for Kubernetes
type: application
version: 0.1.0        # Chart version — bump when you change the chart templates
appVersion: "1.16.0"  # App version — informational, shown in labels
```

- `version` — the Helm chart version. Bump this when chart structure changes.
- `appVersion` — the version of the application the chart deploys. In this setup, the actual image tag is controlled by `values.yaml`, not this field.

### `values.yaml` explained (key fields)

```yaml
replicaCount: 1

image:
  repository: asherif310/python-test
  pullPolicy: IfNotPresent
  tag: "3cb9b6c"          # ← This is what the CI pipeline auto-updates
```
`tag` is patched by the `update-manifest` job every time a new image is built. ArgoCD then detects the change and redeploys.

```yaml
service:
  type: ClusterIP
  port: 5000              # Service port exposed inside the cluster
```

```yaml
ingress:
  enabled: true
  className: "nginx"
  annotations:
    nginx.ingress.kubernetes.io/backend-protocol: "HTTP"
  hosts:
    - host: python-test.example.com
      paths:
        - path: /api/v1
          pathType: Prefix
  tls:
    - secretName: python-test-tls
      hosts:
        - python-test.example.com
```
Enabling TLS here tells nginx Ingress to serve HTTPS and look for a TLS secret named `python-test-tls` in the namespace.

### `_helpers.tpl` — Template functions

This file defines reusable named templates called by the other templates:

| Template name | What it returns |
|---|---|
| `python-test.name` | Chart name (truncated to 63 chars) |
| `python-test.fullname` | Release name + chart name combined (e.g., `python-test`) |
| `python-test.chart` | `name-version` string used in labels |
| `python-test.labels` | Standard Helm labels block |
| `python-test.selectorLabels` | `app.kubernetes.io/name` and `instance` labels |
| `python-test.serviceAccountName` | Service account name or `default` |

### `argocd/values-argo.yaml` — ArgoCD Helm values

```yaml
server:
  replicas: 1
  ingress:
    enabled: true
    ingressClassName: nginx
    annotations:
      nginx.ingress.kubernetes.io/backend-protocol: "HTTPS"
    tls: true

global:
  domain: argocd.example.com
```
These are values passed to the **ArgoCD Helm chart** (not the app chart) when installing ArgoCD itself. It configures ArgoCD to be accessible at `argocd.example.com` behind the nginx Ingress.

### Helm commands

```powershell
# Install the chart (first time)
helm install python-test ./charts/python-test

# Install with custom values
helm install python-test ./charts/python-test -f ./charts/python-test/values.yaml

# Upgrade (update) a running release
helm upgrade python-test ./charts/python-test

# Upgrade, install if not exists
helm upgrade --install python-test ./charts/python-test

# Preview rendered manifests without installing
helm template python-test ./charts/python-test

# Preview with specific values
helm template python-test ./charts/python-test --set image.tag=abc1234

# List installed releases
helm list

# List releases across all namespaces
helm list -A

# Get values of a release
helm get values python-test

# Get the full rendered manifests of a release
helm get manifest python-test

# Uninstall a release
helm uninstall python-test

# Validate chart syntax
helm lint ./charts/python-test

# Package chart into .tgz archive
helm package ./charts/python-test

# Add a Helm repo
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update

# Install ArgoCD from Helm (using the argocd values)
helm install argocd argo/argo-cd -n argocd --create-namespace \
  -f charts/python-test/argocd/values-argo.yaml
```

---

## 7. ArgoCD

ArgoCD is a **GitOps** continuous delivery tool. It watches your Git repository and automatically syncs the Kubernetes cluster to match the desired state declared in Git.

**GitOps principle:** Git is the single source of truth. If `values.yaml` says `tag: abc1234`, the cluster should run that image. ArgoCD enforces this.

### Install ArgoCD

```powershell
# Create namespace
kubectl create namespace argocd

# Install using Helm with custom values
helm install argocd argo/argo-cd -n argocd -f charts/python-test/argocd/values-argo.yaml

# Or install using the official manifest (simpler, no Helm)
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
```

### Access ArgoCD UI

```powershell
# Port-forward to localhost (do this in a separate terminal and keep it running)
kubectl port-forward svc/argocd-server -n argocd 8080:443

# Then open in browser
# https://localhost:8080

# Get initial admin password
kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath="{.data.password}" | base64 -d
```

### ArgoCD CLI

```powershell
# Download CLI (Windows)
# Install via winget or download from https://github.com/argoproj/argo-cd/releases

# Log in to ArgoCD
argocd login localhost:8080 --insecure --username admin --password <password>

# List applications
argocd app list

# Get details of an app
argocd app get python-test

# Manually sync an app (pull latest from Git and apply)
argocd app sync python-test

# Sync with --wait (blocks until sync and health check complete)
argocd app sync python-test --wait --timeout 120

# Sync with grpc-web (needed if behind reverse proxy)
argocd app sync python-test --grpc-web --insecure \
  --auth-token <token> --server <host:port> --wait --timeout 120

# Create an app pointing to the Helm chart in the Git repo
argocd app create python-test \
  --repo https://github.com/ahmed-shereif/python-test \
  --path charts/python-test \
  --dest-server https://kubernetes.default.svc \
  --dest-namespace default \
  --sync-policy automated

# Delete an app
argocd app delete python-test

# Generate an auth token (for CI use in secrets)
argocd account generate-token --account admin

# List accounts
argocd account list

# Change admin password
argocd account update-password

# Check ArgoCD server version
argocd version
```

### Setting up the ArgoCD Application for this project

In the ArgoCD UI (`https://localhost:8080`):

1. Click **New App**
2. Fill in:
   - **Application Name:** `python-test`
   - **Project:** `default`
   - **Sync Policy:** `Automatic` (ArgoCD will sync on every git push)
   - **Repository URL:** `https://github.com/ahmed-shereif/python-test`
   - **Path:** `charts/python-test`
   - **Cluster:** `https://kubernetes.default.svc` (in-cluster)
   - **Namespace:** `default`
3. Click **Create**

After this, every time the `update-manifest` job pushes a new `values.yaml`, ArgoCD detects the change and redeploys.

---

## 8. Self-Hosted GitHub Actions Runner

The `cd` job needs to run **inside the cluster** so it can reach the ArgoCD server without exposing ArgoCD to the internet. This is done by deploying a GitHub Actions runner as a pod inside Kind.

### Install Actions Runner Controller (ARC)

```powershell
# Add Helm repo
helm repo add actions-runner-controller https://actions-runner-controller.github.io/actions-runner-controller
helm repo update

# Create namespace
kubectl create namespace actions-runner-system

# Create GitHub token secret (PAT with repo scope)
kubectl create secret generic controller-manager \
  -n actions-runner-system \
  --from-literal=github_token=<YOUR_GITHUB_PAT>

# Install the controller
helm install actions-runner-controller \
  actions-runner-controller/actions-runner-controller \
  -n actions-runner-system \
  --set syncPeriod=1m
```

### `runnerdeployment.yaml` explained

```yaml
apiVersion: actions.summerwind.dev/v1alpha1
kind: RunnerDeployment
metadata:
  name: self-hosted-runner
  namespace: actions-runner-system
spec:
  replicas: 1                                  # One runner pod
  template:
    spec:
      repository: ahmed-shereif/python-test    # GitHub repo to register runner for
```

This registers a self-hosted runner with your GitHub repository. It shows up in **Repo → Settings → Actions → Runners** as an available runner.

```powershell
# Apply the RunnerDeployment
kubectl apply -f runnerdeployment.yaml

# Check runner pod is running
kubectl get pods -n actions-runner-system

# Check it appears in GitHub
# Go to: GitHub repo → Settings → Actions → Runners
# Should show status: Idle
```

---

## 9. GitHub Actions CI/CD Pipeline

**File:** `.github/workflows/cidc.yaml`

### Trigger

```yaml
on:
  push:
    branches:
      - main
    paths:
      - 'src/**'
      - '.github/workflows/cidc.yaml'
```
The pipeline only runs on pushes to `main` that touch files inside `src/` **or** the workflow file itself. This prevents unnecessary runs on docs-only changes.

### Permissions

```yaml
permissions:
  contents: write
```
Gives the workflow write access to the repository contents. Required for the `update-manifest` job to commit back to `values.yaml`.

---

### Job 1: `ci` — Build and Push Docker Image

**Runs on:** `ubuntu-latest` (GitHub-hosted runner)

```yaml
- name: Checkout code
  uses: actions/checkout@v4
```
Clones the repository.

```yaml
- name: Login to Docker Hub
  uses: docker/login-action@v4
  with:
    username: ${{ secrets.DOCKERHUB_USERNAME }}
    password: ${{ secrets.DOCKERHUB_TOKEN }}
```
Authenticates to Docker Hub using secrets stored in the repo.

```yaml
- name: Set up Docker Buildx
  uses: docker/setup-buildx-action@v3
```
Enables BuildKit — Docker's modern build backend. Required for features like multi-platform builds and layer caching.

```yaml
- name: Compute short SHA
  id: vars
  run: echo "short_sha=${GITHUB_SHA::7}" >> $GITHUB_OUTPUT
```
Truncates the 40-char full commit SHA to 7 characters. This becomes the Docker image tag and is shared with downstream jobs via `outputs`.  
Example: `GITHUB_SHA=a1b2c3d4e5f6...` → `short_sha=a1b2c3d`

```yaml
- name: Build and push
  uses: docker/build-push-action@v7
  with:
    context: .
    push: true
    tags: asherif310/python-test:${{ steps.vars.outputs.short_sha }}
```
Builds the Docker image from the `Dockerfile` in the repo root and pushes it to Docker Hub tagged with the short SHA.

**Output:** `short_sha` — passed to the next job.

---

### Job 2: `update-manifest` — Patch `values.yaml`

**Runs on:** `ubuntu-latest`  
**Depends on:** `ci` job completing successfully

```yaml
- name: Update Image Tag in Helm values
  run: |
    sed -i 's/tag: .*/tag: "${{ needs.ci.outputs.short_sha }}"/' charts/python-test/values.yaml
```
Uses `sed` to find the line containing `tag:` in `values.yaml` and replace it with the new SHA tag.  
Before: `tag: "3cb9b6c"`  
After: `tag: "a1b2c3d"`

```yaml
- name: Commit and push changes
  run: |
    git config --global user.name "github-actions[bot]"
    git config --global user.email "github-actions[bot]@users.noreply.github.com"
    git add charts/python-test/values.yaml
    git commit -m "chore: update image tag to ${{ needs.ci.outputs.short_sha }} [skip ci]"
    git push
```
Commits the updated `values.yaml` back to the repo. The `[skip ci]` suffix in the commit message **prevents this commit from triggering another pipeline run** (otherwise you'd get an infinite loop).

---

### Job 3: `cd` — Trigger ArgoCD Sync

**Runs on:** `self-hosted` (the runner pod inside your Kind cluster)  
**Depends on:** `update-manifest` job completing successfully

```yaml
- name: Install ArgoCD CLI
  run: |
    mkdir -p "$HOME/.local/bin"
    curl -sSL -o "$HOME/.local/bin/argocd" \
      https://github.com/argoproj/argo-cd/releases/latest/download/argocd-linux-amd64
    chmod +x "$HOME/.local/bin/argocd"
    echo "$HOME/.local/bin" >> $GITHUB_PATH
```
Downloads the ArgoCD CLI binary into the runner pod. Since the runner is ephemeral, the CLI is installed fresh each run.

```yaml
- name: argocd app sync
  run: |
    argocd app sync python-test \
      --grpc-web \
      --insecure \
      --auth-token ${{ secrets.ARGOCD_AUTH_TOKEN }} \
      --server ${{ secrets.ARGOCD_SERVER }} \
      --wait \
      --timeout 120
```

| Flag | Explanation |
|---|---|
| `--grpc-web` | Uses HTTP/1.1 fallback — needed when behind an nginx reverse proxy that doesn't support HTTP/2 gRPC |
| `--insecure` | Skips TLS certificate verification (common in local/dev clusters with self-signed certs) |
| `--auth-token` | API token instead of username/password (non-interactive, secure for CI) |
| `--server` | ArgoCD server address (e.g., `argocd-server.argocd.svc.cluster.local:443`) |
| `--wait` | Blocks until sync is complete and app is healthy |
| `--timeout 120` | Fails after 120 seconds if not healthy |

---

### Full pipeline flow

```
git push to main (src/** changed)
         │
         ├─ Job: ci (ubuntu-latest)
         │    Build image → push asherif310/python-test:<sha>
         │    Output: short_sha
         │
         ├─ Job: update-manifest (ubuntu-latest)
         │    Patch values.yaml tag: "<sha>"
         │    git commit [skip ci] → push
         │
         └─ Job: cd (self-hosted runner in Kind)
              argocd app sync python-test --wait
              ArgoCD reads updated values.yaml → Helm renders new Deployment
              New pod with new image starts → old pod terminates
```

---

## 10. Secrets Reference

Configure these in **GitHub → Repo → Settings → Secrets and Variables → Actions**:

| Secret | Value | Used by |
|---|---|---|
| `DOCKERHUB_USERNAME` | `asherif310` | `ci` job login |
| `DOCKERHUB_TOKEN` | Docker Hub access token (not password) | `ci` job login |
| `GITHUB_TOKEN` | Auto-provided by GitHub | `update-manifest` git push |
| `ARGOCD_AUTH_TOKEN` | Token from `argocd account generate-token` | `cd` job |
| `ARGOCD_SERVER` | ArgoCD server address reachable from runner pod (e.g., `argocd-server.argocd.svc.cluster.local:443`) | `cd` job |

### Generate ArgoCD token

```powershell
# While port-forward is active (kubectl port-forward svc/argocd-server -n argocd 8080:443)
argocd login localhost:8080 --insecure --username admin --password <password>
argocd account generate-token --account admin
# Copy the output → paste as ARGOCD_AUTH_TOKEN secret in GitHub
```

---

## 11. Day-to-Day Command Reference

### Check everything is healthy

```powershell
# Cluster node
kubectl get nodes

# App pods
kubectl get pods

# ArgoCD pods
kubectl get pods -n argocd

# Self-hosted runner
kubectl get pods -n actions-runner-system

# ArgoCD app status
argocd app get python-test

# Current image tag deployed
kubectl get deployment python-test -o jsonpath="{.spec.template.spec.containers[0].image}"

# Currently deployed tag in values.yaml
Select-String "tag:" .\charts\python-test\values.yaml
```

### Test the app

```powershell
# Via port-forward (always works regardless of ingress)
kubectl port-forward svc/python-test 5000:5000
# Then in another terminal:
curl http://localhost:5000/api/v1/health
curl http://localhost:5000/api/v1/details

# Via Ingress (add hosts entry first)
# As Administrator:
Add-Content -Path "C:\Windows\System32\drivers\etc\hosts" -Value "127.0.0.1  python-test.example.com"
curl http://python-test.example.com/api/v1/health
```

### Trigger the pipeline manually

```powershell
# Make any change in src/ and push
git add src/app.py
git commit -m "test: trigger pipeline"
git push origin main

# Watch the pod rollout
kubectl get pods -w
```

### Force ArgoCD sync manually (without CI)

```powershell
kubectl port-forward svc/argocd-server -n argocd 8080:443
argocd login localhost:8080 --insecure --username admin --password <password>
argocd app sync python-test --wait
```

### Roll back to previous version

```powershell
# Option 1: kubectl rollout undo
kubectl rollout undo deployment python-test

# Option 2: ArgoCD rollback (to previous sync)
argocd app rollback python-test

# Option 3: git revert the values.yaml commit, push → ArgoCD will sync old tag
git log --oneline -5
git revert <sha-of-values-update-commit>
git push origin main
```

### Restart all pods without changing the image

```powershell
kubectl rollout restart deployment python-test
```

### Scale the deployment

```powershell
kubectl scale deployment python-test --replicas=5
# Or edit values.yaml and run: helm upgrade python-test ./charts/python-test
```

### Get events (useful for debugging crashloops)

```powershell
kubectl get events --sort-by='.lastTimestamp'
kubectl get events -n argocd --sort-by='.lastTimestamp'
```

### nginx Ingress

```powershell
# Install nginx Ingress controller (if not installed)
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml

# Wait for it to be ready
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=90s

# List ingress rules
kubectl get ingress

# Describe ingress (shows backend and rules)
kubectl describe ingress python-test
```

### Git workflow

```powershell
# View recent commits
git log --oneline -10

# Check current tag in values.yaml matches what's running
Select-String "tag:" .\charts\python-test\values.yaml
kubectl get deployment python-test -o jsonpath="{.spec.template.spec.containers[0].image}"

# See what files changed in last commit
git show --name-only HEAD

# Undo uncommitted changes to a file
git checkout -- src/app.py

# Create a feature branch, do work, merge to main via PR to trigger pipeline
git checkout -b feature/my-change
# ... make changes ...
git add .
git commit -m "feat: my change"
git push origin feature/my-change
# Create PR on GitHub → merge to main → pipeline runs
```

---

## 12. End-to-End Flow Walkthrough

This is exactly what happens step by step when you push code:

```
1. You edit src/app.py (e.g., change the message field)

2. git add src/app.py
   git commit -m "feat: update message"
   git push origin main

3. GitHub Actions detects push to main, src/** matches the path filter
   → Starts workflow run "ci-cd"

4. Job: ci  (runs on ubuntu-latest)
   a. Checks out the repo
   b. Logs into Docker Hub with DOCKERHUB_USERNAME / DOCKERHUB_TOKEN secrets
   c. Sets up Docker Buildx
   d. Computes short SHA: e.g., "d4e5f6g"
   e. Runs: docker build -t asherif310/python-test:d4e5f6g .
      - FROM python:3.13-slim
      - pip install flask (cached if requirements.txt unchanged)
      - COPY src/ → runs app.py on CMD
   f. docker push asherif310/python-test:d4e5f6g → image lands on Docker Hub
   g. Outputs short_sha="d4e5f6g"

5. Job: update-manifest  (runs on ubuntu-latest, after ci)
   a. Checks out repo
   b. sed patches values.yaml:   tag: "3cb9b6c"  →  tag: "d4e5f6g"
   c. git commit -m "chore: update image tag to d4e5f6g [skip ci]"
   d. git push  (token = GITHUB_TOKEN, auto-provided)

6. Job: cd  (runs on self-hosted runner pod inside Kind cluster)
   a. Downloads argocd CLI binary
   b. Runs: argocd app sync python-test --grpc-web --insecure
            --auth-token <ARGOCD_AUTH_TOKEN> --server <ARGOCD_SERVER>
            --wait --timeout 120

7. ArgoCD (running in-cluster) detects the updated values.yaml in Git
   a. Runs `helm template` with new values → produces Deployment YAML
      with image: asherif310/python-test:d4e5f6g
   b. Compares to running Deployment → image tag differs → out of sync
   c. Applies new Deployment → Kubernetes starts rolling update:
      - New pod created: pulls asherif310/python-test:d4e5f6g
      - New pod passes readiness → old pod terminated
   d. Sync complete, health = Healthy

8. New pod is running with updated app code
   kubectl get pods  → python-test-<new-hash>   1/1   Running

9. curl http://python-test.example.com/api/v1/details
   → returns new message value, hostname = new pod name
```

---

## 13. Troubleshooting

### Pod stuck in `ImagePullBackOff`

```powershell
kubectl describe pod <pod-name>
# Look at Events section — usually "Failed to pull image" with the reason
```

Common causes:
- Wrong image tag in `values.yaml` (tag doesn't exist on Docker Hub)
- Docker Hub rate limit (unauthenticated pulls are limited)
- Corporate proxy blocking Docker Hub

Fix: Load image directly into Kind (bypasses registry pull):
```powershell
docker pull asherif310/python-test:<tag>
kind load docker-image asherif310/python-test:<tag> --name <cluster-name>
```

---

### ArgoCD shows `OutOfSync` but won't sync

```powershell
argocd app get python-test    # Check sync status detail
argocd app sync python-test --force
```

If ArgoCD shows the app but won't sync due to drift:
```powershell
argocd app set python-test --sync-policy automated
```

---

### `cd` job fails: `argocd: command not found`

The binary download step failed. Check the runner pod has internet access or curl the binary URL manually inside the pod:
```powershell
kubectl exec -it <runner-pod-name> -n actions-runner-system -- curl -I https://github.com
```

---

### Self-hosted runner shows `Offline` in GitHub

```powershell
kubectl get pods -n actions-runner-system
kubectl logs <runner-pod-name> -n actions-runner-system
```
Usually means the GitHub PAT expired or the runner pod crashed. Reapply:
```powershell
kubectl delete -f runnerdeployment.yaml
kubectl apply -f runnerdeployment.yaml
```

---

### Port-forward disconnects

Port-forward is not persistent — it dies if the pod restarts. Re-run:
```powershell
kubectl port-forward svc/argocd-server -n argocd 8080:443
```

For a more persistent setup, use the Ingress (requires the hosts file entry).

---

### Check what image is actually running

```powershell
kubectl get deployment python-test -o jsonpath="{.spec.template.spec.containers[0].image}"
# Should match the tag in values.yaml
Select-String "tag:" .\charts\python-test\values.yaml
```

---

### Pipeline runs but `[skip ci]` commit triggers another run

The `[skip ci]` token only works if **all** paths in the trigger match AND the commit message contains `[skip ci]`. Verify the commit message is exactly right:
```powershell
git log --oneline -3
```
Should show: `chore: update image tag to <sha> [skip ci]`

---

### Kind cluster lost after Docker Desktop restart

Kind clusters don't survive Docker restarts by default. Recreate:
```powershell
kind create cluster --config kind-config.yaml --name <cluster-name>
# Then reinstall ArgoCD, runner, nginx ingress, and re-apply ArgoCD app
```

---

*Last updated: July 2026*
