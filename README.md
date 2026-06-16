# zerolang — Todo BE (Zero read backend + writer sidecar)

A Todo REST API whose **reads** are served by a [Zerolang](https://zerolang.ai)
(`zero` 0.3.4) `std.http` server and whose **writes/persistence** are owned by a thin
external writer sidecar. One public entrypoint, full CRUD, real on-disk persistence.

## Why two processes

On this machine (macOS/arm64) Zero 0.3.4 has a hard, *proven* limitation: the host
direct backend can **read** files inside an HTTP handler (`std.fs.readBytes`) but cannot
**write** them (every fs-write IR value fails `BLD004`), it has no mutable globals
(top-level `var` → `PAR100`), and the HTTP runtime exists **only** on the host target
(every Linux target is `httpRuntime: unsupported`). So no single Zero target can both
serve HTTP and persist writes. The honest split:

```
  client ──> writer.py  (:8080, owns writes + persistence to data/store.json)
                │  GET /health, GET /todos        (proxied)
                └─> zero run  (:3000+, reads data/store.json via std.fs.readBytes)
```

Zero genuinely serves reads — kill `zero run` and `GET /todos` returns `502` while writes
keep working. The writer is the persistence layer Zero can't yet be on this host.

## ⚠️ Portability note

`src/main.0` hardcodes the absolute store path
`/Users/osa/projects/zerolang/data/store.json` (Zero `std.fs.readBytes` needs a string
literal, and an in-handler path can't yet be derived from env on the host backend). If
you clone elsewhere, edit that literal in `src/main.0` to match your clone's
`data/store.json`, then re-run.

## Run

```sh
sh run.sh                       # starts Zero backend + writer, prints both URLs
# -> zero read-backend: http://127.0.0.1:3000
# -> writer: http://127.0.0.1:8080  store=.../data/store.json  backend=127.0.0.1:3000
```

`run.sh` invokes `zero` by **full path** on purpose: `std.http.listen` re-execs
`argv[0]`, so a bare PATH-resolved `zero run` fails with
`zero listen: No such file or directory`.

## Endpoints (public port 8080)

| Method | Path        | Served by | Result                                              |
|--------|-------------|-----------|------------------------------------------------------|
| GET    | /health     | Zero      | `200 {"status":"ok"}`                               |
| GET    | /todos      | Zero      | `200` list, re-read from `data/store.json` each call |
| POST   | /todos      | writer    | `201` new todo (auto-increment id); `400` no title  |
| PATCH  | /todos/:id  | writer    | `200` toggles `done`; `404` if unknown id           |
| DELETE | /todos/:id  | writer    | `204`; `404` if unknown id                          |
| *      | *           | —         | `404 {"error":"not_found"}`                         |

```sh
B=http://127.0.0.1:8080
curl $B/todos
curl -X POST $B/todos -H 'content-type: application/json' -d '{"title":"牛乳を買う"}'
curl -X PATCH  $B/todos/1
curl -X DELETE $B/todos/1
```

## Files

- `src/main.0`   — Zero HTTP handler: `/health`, read-backed `/todos`, plus body-parsing
  echo routes (real `std.fs.readBytes` + `std.json` parsing).
- `writer.py`    — external writer sidecar: owns `data/store.json`, proxies reads to Zero.
- `run.sh`       — launches both, discovers Zero's auto-assigned port, wires them.
- `data/store.json` — the JSON array store (authoritative writer = `writer.py`).

## Verified (live, with curl)

Create → list → toggle → delete with correct status codes, UTF-8 round-trip, auto-increment
ids, `400`/`404` error paths, reads proxied through Zero (`502` when Zero is down), and data
surviving a full restart (on-disk persistence).
