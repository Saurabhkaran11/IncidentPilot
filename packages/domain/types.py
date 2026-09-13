"""Reusable constrained field types shared by every domain model."""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
#: Prefixed digest form used on the wire for plan digests, e.g.
#: ``sha256:abc...`` -- the API contract shows this prefixed shape, and the
#: prefix is what lets a future algorithm change stay distinguishable.
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[a-f0-9]{64}$")]
OpaqueId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
