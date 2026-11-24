# eap-iam-devloopback

Identity plug-in (`eap.iam.identity`, name `devloopback`): the forgeable dev bearer `Authorization: Bearer
dev:<tenant>:<user>`, accepted **only when the TCP peer address is loopback** (`kubectl port-forward`, an SSH tunnel, a
local uvicorn). Never `X-Forwarded-For`: a request that carries `Cf-Access-Jwt-Assertion`, `X-Forwarded-For`,
`CF-Connecting-IP`, `Forwarded` or `X-Real-IP` is not loopback even when its peer is `127.0.0.1`. It also needs
`EAP_AUTH_MODE=dev` at request time, as the bearer always did.

`EAP_IAM_DEVLOOPBACK_EXTRA_PEERS` (comma list, dev mode only) names peer strings that count as loopback; the API tests set
`testclient`, the peer Starlette's TestClient reports. A real ASGI server always reports an IP address.
