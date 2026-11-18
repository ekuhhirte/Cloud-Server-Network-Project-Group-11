# Protocol

This is how the client and server talk to each other over TCP. TCP is just a
stream of bytes, so the protocol has to say where one message ends and the
next begins.

## Message format

Every message is one line of JSON that ends with a newline:

```
{"type": "Download Request", "file_path": "notes.txt"}\n
```

If the message is a file transfer, the raw file bytes come right after the
header. The header says how many (`file_size`), so the receiver just reads
that many bytes and then goes back to reading headers. File bytes are never
searched for a newline, so a file can contain anything.

A few things to know when writing code for this:

- JSON escapes newlines inside strings, so the first real newline always
  means the header is over.
- Headers can be at most 64 KiB. Anything longer is treated as a broken or
  malicious client.
- Read the header and the file bytes from the same buffered reader (for
  example `sock.makefile("rb")`). If you read the header with `readline()` and
  then call `sock.recv()` for the file, you can lose bytes, because the
  buffered reader may already have grabbed the start of the file.
- Paths use `/` and are relative to the server's storage folder.

The names `Download Request`, `Download Response` and `error` come from the
current `backend.py`. The other names are our guess at the same style, so
check them with whoever writes the client. They are all defined in one place,
`server_network/wire.py`.

## Requests (client to server)

| type | fields | notes |
|---|---|---|
| `Hello Request` | `version` | optional, must come before login |
| `Auth Request` | `username`, `credential` | |
| `Upload Request` | `file_path`, `file_size` | file bytes follow after the server says `Upload Ready` |
| `Overwrite Reply` | `confirm` (true/false) | only sent after an `Overwrite Prompt` |
| `Download Request` | `file_path` | |
| `Delete Request` | `file_path` | |
| `Dir Request` | `dir_path` (optional, empty means the root) | |
| `Subfolder Request` | `action` (`create` or `delete`), `dir_path` | |
| `Goodbye Request` | | |

## Replies (server to client)

| type | fields | notes |
|---|---|---|
| `Hello Response` | `version`, `server`, `auth_required` | |
| `Auth Response` | `username` | |
| `Download Response` | `filename`, `file_size`, `file_path` | `file_size` raw bytes follow |
| `Upload Ready` | `file_path`, `file_size` | client can start sending the file |
| `Upload Complete` | `file_path`, `file_size` | |
| `Upload Cancelled` | | client answered no to the overwrite prompt |
| `Overwrite Prompt` | `file_path`, `message` | file already exists |
| `Delete Response` | `file_path` | |
| `Dir Response` | `dir_path`, `entries`, `truncated` | each entry is `{name, type, size}` |
| `Subfolder Response` | `action`, `dir_path` | |
| `Goodbye Response` | | server closes after this |
| `error` | `error`, `code`, `fatal` | see below |

A directory listing has to fit in one header, so a very big folder gets cut
off and `truncated` is set to true.

## Rules

- Before logging in, the server only accepts `Hello Request`, `Auth Request`
  and `Goodbye Request`. Anything else gets `NOT_AUTHENTICATED`.
- After an `Overwrite Prompt`, the only things accepted are `Overwrite Reply`
  and `Goodbye Request`.
- Five failed logins in a row and the server closes the connection.

## Upload

```
client: Upload Request {file_path, file_size}
server: error                         (nothing was uploaded, connection stays open)
   or   Overwrite Prompt {file_path}  (client replies with Overwrite Reply)
          confirm false -> server sends Upload Cancelled, done
          confirm true  -> keep going
server: Upload Ready
client: <file_size raw bytes>
server: Upload Complete   (or an error)
```

If something goes wrong after the client has started sending file bytes (disk
fills up, the client stalls, a server bug), the server can't know where the
next message starts anymore. It sends a fatal `error` if it can and closes the
connection. The half-written file is thrown away.

## Download

```
client: Download Request {file_path}
server: error                         (connection stays open)
   or   Download Response {filename, file_path, file_size}
        <file_size raw bytes>
```

If the server fails partway through the file it just closes the connection.
The client sees fewer bytes than `file_size` and knows the download is
incomplete.

## Bad input

| What the client sent | What the server does |
|---|---|
| A full line that isn't valid JSON, or isn't a JSON object | `BAD_REQUEST` ("Malformed Header"), connection stays open |
| Valid JSON with an unknown `type` | `UNKNOWN_COMMAND`, connection stays open |
| A missing field, wrong field type, or a bad path | `BAD_REQUEST` or `BAD_PATH`, connection stays open |
| A line over 64 KiB, or no newline in the first 64 KiB | `PROTOCOL_ERROR`, connection closed |

The last row is the only one that closes the connection. Once a line is too
long there's no safe way to tell where the next message starts, so the server
gives up instead of guessing.

## Error codes

Every `error` reply has a `code` and a `fatal` flag. If `fatal` is true the
server closes the connection right after sending it.

| code | meaning | fatal |
|---|---|---|
| `BAD_REQUEST` | missing or wrong field | no |
| `UNKNOWN_COMMAND` | `type` isn't one we know | no |
| `NOT_AUTHENTICATED` | log in first | no |
| `AUTH_FAILED` | bad username or password (same message for both) | no |
| `BAD_STATE` | not allowed at this point, for example `Overwrite Reply` with no prompt | no |
| `BAD_PATH` | path tries to leave the storage folder or is invalid | no |
| `NOT_FOUND` | file or folder doesn't exist | no |
| `FILE_EXISTS` | already exists | no |
| `FILE_BUSY` | another client is using it | no |
| `NOT_EMPTY` | folder still has files in it | no |
| `DISK_FULL` | out of space | no, except in the middle of an upload |
| `TOO_LARGE` | bigger than the server's upload limit | no |
| `PROTOCOL_ERROR` | message framing broke | yes |
| `VERSION_MISMATCH` | client and server versions don't match | yes |
| `TOO_MANY_FAILURES` | too many failed logins | yes |
| `SERVER_BUSY` | too many clients connected | yes |
| `INTERNAL_ERROR` | bug on the server (details go in the server log only) | no, except in the middle of an upload |

## Still to decide

- The message names that aren't in `backend.py` yet.
- What exactly goes in `credential`.
- The maximum upload size.
