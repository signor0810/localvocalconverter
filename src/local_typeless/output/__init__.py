"""Output adapters package."""
from local_typeless.output.clipboard_out import ClipboardOutput, copy_with_retry
from local_typeless.output.obsidian_out import ObsidianOutput, append_to_file
from local_typeless.output.rich_paste import RichPaster, set_clipboard_image

__all__ = [
    "ClipboardOutput",
    "ObsidianOutput",
    "RichPaster",
    "append_to_file",
    "copy_with_retry",
    "set_clipboard_image",
]
