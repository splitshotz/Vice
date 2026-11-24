"""Uploading clips to a remote media server (Zipline v4 or S3-compatible)."""

from .base import Provider, RemoteRef, UploadError, make_provider

__all__ = ["Provider", "RemoteRef", "UploadError", "make_provider"]
