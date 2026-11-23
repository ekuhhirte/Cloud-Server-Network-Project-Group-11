# Protocol

How the client and server talk over TCP.

TCP gives you a stream of bytes with no message boundaries, so we add our own.
A message is one line of JSON ending in a newline:

```
{"type": "Download Request", "file_path": "notes.txt"}\n
```

For uploads and downloads the raw file bytes come right after the header line.
The header has `file_size`, so the reader takes exactly that many bytes and
then goes back to reading header lines. Nobody searches the file for newlines,
so files can hold anything.

A few things to watch when writing code for this:

- Read headers and file bytes from the same reader, like `sock.makefile("rb")`.
  If you call `readline()` for the header and then `sock.recv()` for the file,
  the reader may already have grabbed the start of the file and you lose it.
- Headers can be at most 64 KiB.
- Paths use `/` and are relative to the storage folder. No `..`, no leading
  `/`, no backslashes.

## Requests

| type | fields |
|---|---|
| `Auth Request` | `username`, `credential` |
| `Dir Request` | `dir_path` (leave it out for the top folder) |
| `Delete Request` | `file_path` |
| `Subfolder Request` | `action` (`create` or `delete`), `dir_path` |
| `Download Request` | `file_path` |
| `Upload Request` | `file_path`, `file_size` |
| `Overwrite Reply` | `confirm` (true or false), only after an `Overwrite Prompt` |
| `Goodbye Request` | |

Everything except `Auth Request` and `Goodbye Request` needs a login first.

## Replies

| type | fields | notes |
|---|---|---|
| `Auth Response` | `username` | |
| `Dir Response` | `dir_path`, `entries`, `truncated` | entries look like `{name, type, size}`, type is `file` or `dir`. Max 500, then `truncated` is true |
| `Delete Response` | `file_path` | |
| `Subfolder Response` | `action`, `dir_path` | |
| `Download Response` | `filename`, `file_path`, `file_size` | `file_size` raw bytes come next |
| `Upload Ready` | `file_path`, `file_size` | go ahead and send the bytes |
| `Overwrite Prompt` | `file_path`, `message` | file exists, answer with `Overwrite Reply` |
| `Upload Cancelled` | `file_path` | they said no to the prompt |
| `Upload Complete` | `file_path`, `file_size` | saved |
| `Goodbye Response` | | server closes after this |
| `error` | `error` | text is listed at the bottom |

## Upload

```
client: Upload Request {file_path, file_size}
server: error                       nothing started, nothing to send
   or   Overwrite Prompt            the file exists
client:   Overwrite Reply {confirm}
server:   Upload Cancelled          if confirm was false, done
server: Upload Ready
client: <file_size raw bytes>
server: Upload Complete             or error
```

Wait for `Upload Ready` before sending file bytes. If the file exists the
server sends the prompt first, and bytes sent at that point get read as the
answer.

If something breaks after the bytes started (disk full, client too slow, a bug)
the server can't tell where the next message would begin. It sends one `error`
if it can, then closes the connection and throws away the partial file.

## Download

```
client: Download Request {file_path}
server: error                       connection stays open
   or   Download Response {filename, file_path, file_size}
        <file_size raw bytes>
```

If the server dies half way through the file it just closes the connection. The
client sees fewer bytes than `file_size`, so it knows the file is incomplete.

## Errors

An error is `{"type": "error", "error": "<text>"}`. Unless it says otherwise
below, the connection stays open and the client can try again.

| text | when |
|---|---|
| `Malformed Header` | not JSON, not an object, or a field is missing or the wrong type |
| `Unknown Command` | we don't know that `type` |
| `Not Authenticated` | not logged in yet |
| `Invalid Login` | wrong username or password (same text for both) |
| `Too Many Failed Logins` | 5 bad logins, then the connection closes |
| `Bad Path` | path is not allowed |
| `File Not Found` | file or folder doesn't exist |
| `Already Exists` | folder already exists |
| `File Is Busy` | another client is using the file |
| `Is A Folder`, `Not A Folder` | a folder used as a file or the other way round |
| `Folder Not Empty` | deleting a folder that still has things in it |
| `File Too Large` | `file_size` over the 4 GiB limit |
| `Disk Full` | no space left (closes the connection if it happens mid upload) |
| `Header Too Long` | header over 64 KiB, closes |
| `Expected Overwrite Reply` | something else came after an `Overwrite Prompt`, closes |
| `Transfer Timed Out` | client went quiet mid upload, closes |
| `Internal Server Error` | server bug, details only in the server log (closes if mid upload) |

## Still to decide

What goes in `credential`. The assignment says the password can't be sent as
clear text, so the group has to pick something.
