# Server network layer

`network_layer.py` handles the socket side of the server. It reads a request,
checks it, asks the backend to do the work and sends back the reply. It doesn't
touch files, locks or threads, those belong to the backend. Message formats are
in `PROTOCOL.md`.

## How it plugs in

```
client --TCP--> backend.py (accept loop, a thread per client)
                    |
                    v
              network_layer.serve_client(...)
                    |
                    v
              backend object (files, locks)
```

In each client thread:

```python
import network_layer

def handle_client(client_socket, client_address):
    network_layer.serve_client(client_socket, client_address, backend, record_transfer)
```

`serve_client` never raises and always closes the socket. `record_transfer` is
optional (see the metrics section).

## What the backend needs

An object with these methods. No base class.

| method | does |
|---|---|
| `check_login(username, credential)` | returns True or False |
| `exists(path)` | True or False |
| `list_dir(path)` | list of `{"name", "type", "size"}`, `""` means the top folder |
| `delete(path)` | delete a file |
| `create_dir(path)`, `delete_dir(path)` | make or remove a folder |
| `download(path)` | `with` block that gives `(file, size)`, holds a read lock |
| `upload(path)` | `with` block that gives something with `.write()`, holds a write lock |

Errors are normal Python ones: `FileNotFoundError`, `FileExistsError`,
`OSError` (like disk full), and `network_layer.FileBusyError` if someone else
has the file. The network layer turns each into the right message.

`download` and `upload` are easy with `contextlib.contextmanager`:

```python
@contextmanager
def download(self, path):
    lock = self.lock_manager.buildOrAcquireLock(real_path)
    lock.acquire_read()
    try:
        with open(real_path, "rb") as f:
            yield f, os.path.getsize(real_path)
    finally:
        lock.release_read()

@contextmanager
def upload(self, path):
    lock = self.lock_manager.buildOrAcquireLock(real_path)
    lock.acquire_write()
    try:
        with open(temp_path, "wb") as f:
            yield f
        os.replace(temp_path, real_path)   # skipped if the with block failed
    except BaseException:
        # delete temp_path if it exists
        raise
    finally:
        lock.release_write()
```

If the block ends in an error (client left, disk full), the half written file
has to go, which is why the upload writes to a temp file first.

`check_path` already rejects `..` and absolute paths, but the backend should
still join the path onto the storage folder and make sure the result is inside
it (`os.path.realpath` plus `startswith`).

## What's in the file

| piece | job |
|---|---|
| `read_header` | reads one line (max 64 KiB) and parses the JSON |
| `check_path`, `get` | check paths and field types |
| `do_auth`, `do_dir`, ... | one function per request type |
| `handle_request` | picks the function based on `type` |
| `serve_client` | the loop: read a header, handle it, repeat, clean up |
| `hang_up` | closes without losing our last message |

Two ideas explain most of the code. First, headers and file bytes come from the
same reader, so no bytes get lost. Second, once file bytes are moving an error
message would look like file data, so a failure mid transfer closes the
connection (an upload sends one last error first, since the client is
listening).

## Metrics

If you pass `record_transfer`, it gets called after every finished upload or
download with a dict:

| key | meaning |
|---|---|
| `operation` | `"upload"` or `"download"` |
| `file`, `size_bytes` | path and byte count |
| `seconds` | time spent moving the file bytes only |
| `mb_per_s` | data rate (1 MB = 1,000,000 bytes) |
| `response_time_s` | time until the server was ready (checks, lock, first reply) |
| `clients` | rough count of busy clients (running threads) |
| `user`, `client`, `time` | who, from where, when |

The timer starts just before the first file byte and stops after the last, so a
slow lock doesn't look like slow network. The callback runs after the timer
stops and the lock is released, and if it raises, the error is logged and the
transfer is fine. Failed or refused transfers aren't recorded.

## Assignment checklist

| requirement | where |
|---|---|
| Upload, prompt if the file exists | `do_upload`, `ask_overwrite` |
| Download, error if missing | `do_download`, `error_text` |
| Delete, error if busy or missing | `do_delete`, `FileBusyError` |
| Dir and Subfolder create/delete | `do_dir`, `do_subfolder` |
| Clear error messages | `error_text`, table in `PROTOCOL.md` |
| Rate, transfer time, third metric | `report_transfer` |
| Many clients at once | one `serve_client` per thread, nothing shared between clients |
| Security | paths checked, header and file sizes capped, login tries capped |
| No clear text password | **not done here**, see below |

## Not covered here

- **Passwords.** `credential` goes to `check_login` as is. The group needs to
  pick a way to protect it, like wrapping the socket with Python's `ssl` on
  both sides, or having the client send a hash.
- **File naming (TS001), load balancer, locks, storage folder.** Backend.

## Running the tests

```
pip install -r requirements-dev.txt
python -m pytest -q
```

`tests/fake_storage.py` is a pretend backend that keeps files in memory.
`tests/test_real_tcp.py` runs the same code over real localhost sockets with
three clients at once.
