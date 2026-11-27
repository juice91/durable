# Expose Studio and the API

To try the platform on one machine, without Kubernetes, use the [Docker Compose quick start](quickstart-compose.md); it
publishes Studio on the loopback address only. The rest of this page is about a Kubernetes install.

The Kubernetes manifests (`deploy/k8s`) create ClusterIP Services only, and the `pydanticos` and `iam` namespaces deny all
traffic that no NetworkPolicy allows. To let people reach the platform, put an ingress controller or any other reverse
proxy in front of it. The example below is a Kubernetes Ingress with TLS; a proxy outside the cluster works the same way.

## What to route

| Public path | Service | Port |
|---|---|---|
| `studio.example.com/api` | `api` (namespace `pydanticos`) | 8000 |
| `studio.example.com/` | `studio` (namespace `pydanticos`) | 3000 |
| `auth.example.com/` | `zitadel` (namespace `iam`), HTTP/2 cleartext (h2c) to the backend | 8080 |

Serve Studio and the API on one origin: the Studio image calls `/api/v1/...` on whatever origin served it, so no CORS
setting is needed. OpenFGA, Postgres and the metrics stack are never published.

## Before you publish anything

- **Sign-in.** Choose a mode and set it up first ([the three Studio sign-in modes](admin-account.md)). With the built-in
  admin account (`local`), create the account before you publish anything, and read "Sender addresses behind a proxy"
  below: its sign-in is throttled per address. For ZITADEL (authorization code with PKCE; Studio sends the access token
  to the API) follow `plugins/iam-zitadel/README.md`, set `EAP_ZITADEL_ISSUER` in `deploy/k8s/configmap.yaml` and
  `ZITADEL_EXTERNALDOMAIN` in `deploy/k8s/iam/zitadel.yaml` to your own host (`auth.example.com` here). Run the bootstrap
  with `--issuer https://auth.example.com --public-url https://studio.example.com`.
- **Authentication mode.** The ConfigMap ships `EAP_AUTH_MODE: dev` and the `openaccess` policy, under which every
  signed-in user is admin. On a reachable host use `EAP_AUTH_MODE: production`, remove `devloopback` from
  `EAP_IAM_IDENTITY`, and switch to OpenFGA ([IAM](iam.md), "Switching to OpenFGA").
- **TLS only.** Terminate TLS at the ingress and redirect plain HTTP.

## Ingress example

The annotations are for ingress-nginx; other controllers have equivalents. Run events are server-sent events, so the proxy
must not buffer responses and must allow long reads. Attachments need a request body limit of at least
`EAP_CHAT_ATTACHMENT_MAX_BYTES` (10 MiB by default). Create the TLS Secrets yourself or with cert-manager.

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: studio
  namespace: pydanticos
  annotations:
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-body-size: 12m
spec:
  ingressClassName: nginx
  tls:
    - hosts: [studio.example.com]
      secretName: studio-tls
  rules:
    - host: studio.example.com
      http:
        paths:
          - {path: /api, pathType: Prefix, backend: {service: {name: api, port: {number: 8000}}}}
          - {path: /, pathType: Prefix, backend: {service: {name: studio, port: {number: 3000}}}}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: zitadel
  namespace: iam
  annotations:
    nginx.ingress.kubernetes.io/backend-protocol: GRPC # h2c to the backend: ZITADEL's APIs need HTTP/2
spec:
  ingressClassName: nginx
  tls:
    - hosts: [auth.example.com]
      secretName: auth-tls
  rules:
    - host: auth.example.com
      http:
        paths:
          - {path: /, pathType: Prefix, backend: {service: {name: zitadel, port: {number: 8080}}}}
```

ZITADEL must receive its public host name as the `Host` header. With ingress-nginx, `GRPC` carries every request to that
backend over h2c; check that the sign-in pages and the APIs both answer with your controller.

## Admit the controller

Add one policy per published Service. Replace `ingress-nginx` with the namespace your controller runs in.

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: studio-ingress-controller, namespace: pydanticos}
spec:
  podSelector: {matchLabels: {app.kubernetes.io/name: studio}}
  policyTypes: [Ingress]
  ingress:
    - from: [{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: ingress-nginx}}}]
      ports: [{protocol: TCP, port: 3000}]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: api-ingress-controller, namespace: pydanticos}
spec:
  podSelector: {matchLabels: {app.kubernetes.io/name: api}}
  policyTypes: [Ingress]
  ingress:
    - from: [{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: ingress-nginx}}}]
      ports: [{protocol: TCP, port: 8000}]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: zitadel-ingress-controller, namespace: iam}
spec:
  podSelector: {matchLabels: {app.kubernetes.io/name: zitadel}}
  policyTypes: [Ingress]
  ingress:
    - from: [{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: ingress-nginx}}}]
      ports: [{protocol: TCP, port: 8080}]
```

## Paths that outside systems call

Two path prefixes authenticate by themselves and are off by default. If you put a sign-in gate in front of the whole host
(an authenticating proxy, for example), let only these prefixes through it, and add a rate limit for them at the proxy.

| Prefix | What authenticates it | Switch |
|---|---|---|
| `/api/v1/hooks/` | the HMAC signature of an [inbound webhook](webhooks.md) | `EAP_WEBHOOKS_ENABLED` |
| `/api/v1/external/` | a `run` API key ([external access](external-access.md)) | `EAP_EXTERNAL_ACCESS_ENABLED` |

For webhooks set, in `deploy/k8s/configmap.yaml` or on the `api` Deployment:

| Variable | Meaning |
|---|---|
| `EAP_WEBHOOKS_ENABLED` | `true` once the hooks path is reachable through your Ingress or proxy. Otherwise every hook URL answers 404. |
| `EAP_WEBHOOK_BASE_URL` | The public https origin senders use (`https://studio.example.com`). A trigger cannot be enabled without it. |
| `EAP_WEBHOOK_PUBLIC` | `true` states that the path is internet-facing; the API then refuses to start under `EAP_AUTH_MODE=dev`. |
| `EAP_WEBHOOK_IP_RATE_PER_MINUTE` | Per-address limit of the hook route, per API process (default 120; `0` turns it off). |

## Sender addresses behind a proxy

Behind a proxy every request comes from the proxy's address, so the per-address limits would share one bucket. The API
reads the sender's address from the `CF-Connecting-IP` header, and only from a peer listed in `EAP_TRUSTED_PROXY_CIDRS`
(empty by default: nobody). Configure your proxy to **overwrite** that header with the client's address on every request,
and only then list the proxy's CIDR. A proxy that passes a client's own value through would let senders choose their
address. The identity chain never reads forwarded headers.
