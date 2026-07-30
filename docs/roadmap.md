# Engineering Roadmap

Non-test backlog items. Test work lives in [test-roadmap.md](test-roadmap.md).

---

## 1. Generate the OpenAPI spec instead of hand-authoring it

Status: **Deferred** — decided against for the initial Swagger work, revisit if the spec starts drifting in practice.

### Background

`src/bt_audio_manager/web/static/openapi.yaml` is written by hand. It has to be, because nothing
can derive it from the code: every handler in `web/api.py` has the same signature —

```python
async def connect(request: web.Request) -> web.Response:
```

`web.Request` describes the HTTP plumbing, not the payload. The real contract only appears as
executed statements inside the body (`body.get("address")`, `web.json_response({...})`), so an
introspection tool sees nothing that distinguishes `connect` from `forget`. It goes all the way
down: `manager.pair_device()` is annotated `-> dict`, which says nothing about the keys.

This is the difference from the Swashbuckle setup in the
[Multi-SendSpin-Player-Container](https://github.com/scyto/Multi-SendSpin-Player-Container)
add-on, where typed C# minimal-API endpoints let the framework generate the spec for free.

### The option

[`aiohttp-pydantic`](https://pypi.org/project/aiohttp-pydantic/) 3.0.1 (May 2026) is the closest
Python equivalent. Requires Python >= 3.12 and aiohttp >= 3.10 — both already satisfied here
(3.12 and 3.14.1). Its `aiohttp_pydantic.oas` sub-app generates the spec from annotations, and
function-based handlers are supported via `@inject_params`, so no class-based view rewrite.

Handlers would become:

```python
async def connect(body: ConnectRequest) -> ConnectResponse:
```

and the spec would generate itself — drift becomes structurally impossible.

### Why it was deferred

It is a rewrite of the web layer, not a documentation change:

- all 26 handlers re-signed, with request/response models defined for each
- `pydantic` added to `docker/requirements.txt` — Rust wheels, built across 4 architectures
- **the real risk:** validation moves from the hand-rolled checks in `api.py` to pydantic, which
  changes error response shapes. The frontend reads `data?.error` (`web/static/app.js`), and the
  friendly `_BLUEZ_ERROR_MAP` messages plus the numeric range checks on
  `PUT /api/devices/{address}/settings` would all need remapping to keep the UI's error toasts
  working.

Bundling that with a docs feature was not worth the regression risk.

### If picked up

- Supersedes the "keep the spec current" discipline — generation replaces it.
- Makes `tests/test_openapi_spec.py` (the drift test) redundant.
- The hand-written spec is not wasted work: it is the reference to diff generated output against.
- Do it behind the existing test suite, and verify every error path the UI displays still returns
  a body with an `error` key.
