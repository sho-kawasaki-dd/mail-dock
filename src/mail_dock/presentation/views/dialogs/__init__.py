"""Modal dialogs used by the presentation layer."""

from .confirmation_dialog import (
    ConfirmationDialog,
    confirm_external_link,
    confirm_overwrite,
    confirm_save_executable,
)
from .error_dialog import ErrorDialog, show_error
from .integrity_dialog import IntegrityDialog
from .progress_dialog import ProgressDialog

__all__ = [
    "ConfirmationDialog",
    "ErrorDialog",
    "IntegrityDialog",
    "ProgressDialog",
    "confirm_external_link",
    "confirm_overwrite",
    "confirm_save_executable",
    "show_error",
]
