"""Server-side network layer for the file sharing project.

Reads client messages, validates them, calls the backend and sends replies.
Doesn't touch files itself, the backend does that.
"""
