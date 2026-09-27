"""Use-case functions prewired with their production infrastructure adapters."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from mail_dock.infrastructure.parsing.eml_parser import EmlParser
from mail_dock.infrastructure.storage.pst_manifest import PstManifestReader
from mail_dock.usecases.reindex import ReindexResult
from mail_dock.usecases.reindex import reindex as _reindex
from mail_dock.usecases.reparse import ReparseResult
from mail_dock.usecases.reparse import reparse_messages as _reparse_messages
from mail_dock.usecases.sync_mail import (
    SyncResult,
)
from mail_dock.usecases.sync_mail import (
    force_fetch_message as _force_fetch_message,
)
from mail_dock.usecases.sync_mail import (
    sync_account as _sync_account,
)
from mail_dock.usecases.verify import ManifestVerifyResult
from mail_dock.usecases.verify import verify_manifest as _verify_manifest

_PARSER = EmlParser()

sync_account: Callable[..., SyncResult] = partial(_sync_account, parser=_PARSER)
force_fetch_message: Callable[..., SyncResult] = partial(
    _force_fetch_message,
    parser=_PARSER,
)
reparse_messages: Callable[..., ReparseResult] = partial(_reparse_messages, parser=_PARSER)
reindex: Callable[..., ReindexResult] = partial(_reindex, parser=_PARSER)
verify_manifest: Callable[..., ManifestVerifyResult] = partial(
    _verify_manifest,
    pst_manifest_reader_factory=PstManifestReader,
)
