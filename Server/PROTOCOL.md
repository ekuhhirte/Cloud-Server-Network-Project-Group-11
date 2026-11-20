# Application-Layer Protocol (draft v1)

Transport: **TCP**. TCP is a byte stream, so every message is framed.

## Wire layout

Every message, in both directions, is:

```
[ 4 bytes: header length N, unsigned, big-endian ][ N bytes: UTF-8 JSON object ][ optional raw bytes ]
```

- The 4-byte prefix is `struct.pack("!I", N)`. `N` is the length of the
  **encoded bytes** of the JSON (never the character count).
- `N` must satisfy `1 <= N <= MAX_HEADER_LEN` (65536). Anything else is a
  fatal protocol error; the receiver must not try to read that many bytes.
- The JSON must decode to an **object** (dict). Lists, numbers and strings
  are protocol errors.
- If the JSON has a `"size"` field (integer, >= 0) and the message type
  carries a body, exactly `size` raw bytes follow immediately. The reader
  must never read past `size`; the next byte belongs to the next message.
- Paths always use `/` as the separator, relative to the storage root.

## Requests (client -> server)

| cmd | required fields | raw bytes follow? |
|---|---|---|
| `hello` | `version` (int) | no |
| `auth` | `username` (str), `credential` (str) | no |
| `upload` | `name` (str), `size` (int) | after server says `ready` |
| `overwrite` | `confirm` (bool) | no (answer to a prompt) |
| `download` | `name` (str) | no |
| `delete` | `name` (str) | no |
| `dir` | `path` (str, optional, default `""`) | no |
| `subfolder` | `action` (`"create"`/`"delete"`), `path` (str) | no |
| `goodbye` | none | no |

## Responses (server -> client)

Every response has `"status"`: `"ok"`, `"error"` or `"prompt"`.

| status | fields | raw bytes follow? |
|---|---|---|
| `ok` | optional extra fields (`size`, `entries`, `name`, ...) | download reply only (`size` bytes) |
| `error` | `code` (str), `message` (str), `fatal` (bool) | no |
| `prompt` | `code` (`"FILE_EXISTS"`), `message` | no |

`dir` replies `ok` with `path`, `entries` (list of `{name, type, size}`) and
`truncated` (true if the listing was cut to fit one header frame).

Session rules: before `auth` succeeds only `hello`, `auth`, `goodbye` are
accepted (others get `NOT_AUTHENTICATED`). While a `FILE_EXISTS` prompt is
outstanding only `overwrite` and `goodbye` are accepted. `hello` is
optional but, if sent, must come before `auth`.

For `upload`, after validation the server replies either:
`ok` with `"ready": true` (client now sends `size` raw bytes),
`prompt` / `FILE_EXISTS` (client must answer with an `overwrite` request), or
`error`.

## Error codes

| code | meaning | fatal? |
|---|---|---|
| `BAD_REQUEST` | malformed field/value | no |
| `UNKNOWN_COMMAND` | `cmd` not recognised | no |
| `NOT_AUTHENTICATED` | command needs login first | no |
| `AUTH_FAILED` | wrong credentials | no (fatal after too many) |
| `BAD_STATE` | valid command, not allowed right now | no |
| `BAD_PATH` | path escapes storage root or is illegal | no |
| `NOT_FOUND` | file/folder does not exist | no |
| `FILE_EXISTS` | already exists | no |
| `FILE_BUSY` | in use by another client | no |
| `NOT_EMPTY` | folder still has contents | no |
| `DISK_FULL` | no space | no |
| `TOO_LARGE` | size above server limit | no |
| `PROTOCOL_ERROR` | framing/JSON broken, stream untrusted | **yes** |
| `VERSION_MISMATCH` | incompatible `hello` version | **yes** |
| `TOO_MANY_FAILURES` | too many failed logins | **yes** |
| `INTERNAL_ERROR` | server bug (details only in server log) | no |

Recoverable errors keep the connection open. Fatal errors are sent (if
possible) and then the server closes the connection.

## Upload sequence

```
C: upload {name, size}
S: error                       -> end (connection stays open)
S: prompt FILE_EXISTS          -> C sends overwrite {confirm}
     confirm=false -> S: ok {cancelled:true}   -> end
     confirm=true  -> continue
S: ok {ready:true}
C: <size raw bytes>
S: ok {name, size}             (or error; see aborted-upload rule)
```

**Aborted-upload rule:** if the server aborts after the client has already
begun sending raw bytes, the stream position is unknown, so the server sends
the error (if it can) and **closes the connection**.

## Download sequence

```
C: download {name}
S: error                       -> end
S: ok {size}  followed by <size raw bytes>
```

## Open items (confirm with teammates)

- Credential format for `auth` (owned by the security person).
- Whether the shared message helpers live in one module used by both sides.
- Upload size limit and `Dir` listing size cap.
