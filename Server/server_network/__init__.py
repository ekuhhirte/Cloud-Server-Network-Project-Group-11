"""Server-side network layer for the CNT3004 file-sharing project.

This package is the "waiter" between the network and the server backend:
it reads framed messages, validates them, dispatches them to backend
operations, and builds replies. It never touches the file system directly;
that is the backend's job (see backend_api.py for the contract).
"""
